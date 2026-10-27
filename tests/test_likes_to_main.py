import tempfile
import unittest
import os
from unittest.mock import patch, Mock
from pathlib import Path

from likes_to_main import (has_new_likes, scan_new_likes, sync, main, save_checkpoints,
                           load_checkpoints, YouTubeClient, QuotaExceeded)


class FakeYouTube:
    def __init__(self, liked, main, fail_on=None, youtube_liked=None, music_ids=None):
        self.liked = liked
        self.main = main
        self.fail_on = fail_on
        self.added = []
        self.youtube_liked = liked if youtube_liked is None else youtube_liked
        self.music_ids = set(liked if music_ids is None else music_ids)

    def owns_playlist(self, playlist_id):
        return playlist_id == "MainId"

    def get_playlist(self, playlist_id, limit=None):
        assert playlist_id == "MainId"
        ids = self.main[:limit] if limit is not None else self.main
        return {"tracks": [{"videoId": vid} for vid in ids]}

    def get_liked_songs(self, limit=100):
        return {"tracks": [{"videoId": vid} for vid in self.liked[:limit]]}

    def get_youtube_liked_songs(self, limit=100):
        return {"tracks": [{"videoId": vid} for vid in self.youtube_liked[:limit]]}

    def is_music_video(self, vid):
        return vid in self.music_ids

    def insert_playlist_item(self, playlist_id, vid):
        assert playlist_id == "MainId"
        if vid == self.fail_on:
            raise RuntimeError("API failure")
        self.added.append(vid)
        self.main.insert(0, vid)
        return {"snippet": {"resourceId": {"videoId": vid}}}


A = "AAAAAAAAAAA"
B = "BBBBBBBBBBB"
C = "CCCCCCCCCCC"
D = "DDDDDDDDDDD"
E = "EEEEEEEEEEE"


class LikesToMainTests(unittest.TestCase):
    def test_unavailable_head_does_not_walk_thousands_of_likes(self):
        client = object.__new__(YouTubeClient)
        client._request = Mock(return_value={"items": [{}] * 50, "nextPageToken": "older"})
        result = client.get_liked_songs(limit=50)
        self.assertEqual(result, {"tracks": [], "hasMore": True})
        self.assertEqual(client._request.call_count, 1)

    def test_probe_reads_one_page_with_unavailable_head_and_new_music(self):
        client = object.__new__(YouTubeClient)
        client._request = Mock(return_value={"items": [{}, {"contentDetails": {"videoId": A}}],
                                            "nextPageToken": "older"})
        # A short page with a continuation must also remain a single probe read.
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "likes.json"
            save_checkpoints(checkpoint, [C])
            self.assertTrue(has_new_likes(client, checkpoint))
        self.assertEqual(client._request.call_count, 1)

    def test_repeated_api_page_stops_before_quota_is_exhausted(self):
        client = object.__new__(YouTubeClient)
        client._request = Mock(return_value={"items": [{"contentDetails": {"videoId": A}}],
                                            "nextPageToken": "same"})
        with self.assertRaisesRegex(RuntimeError, "защиты квоты"):
            client.get_playlist("MainId")
        self.assertEqual(client._request.call_count, 2)

    def test_quota_failure_survives_process_restart_without_network_or_cursor_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            checkpoint = path / "likes.json"
            save_checkpoints(checkpoint, [C])
            original = checkpoint.read_bytes()
            client = object.__new__(YouTubeClient)
            client.quota_file = path / "youtube_quota.json"
            client._access_token = Mock(return_value="test")
            client.session = Mock()
            response = client.session.request.return_value
            response.status_code = 403
            response.ok = False
            response.json.return_value = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
            with patch("likes_to_main.next_quota_reset", return_value=9999999999):
                with self.assertRaises(QuotaExceeded):
                    sync(client, "MainId", checkpoint)
            self.assertEqual(checkpoint.read_bytes(), original)
            restarted = object.__new__(YouTubeClient)
            restarted.quota_file = client.quota_file
            restarted.session = Mock()
            with self.assertRaises(QuotaExceeded):
                restarted.get_liked_songs(50)
            restarted.session.request.assert_not_called()
            self.assertEqual(client.session.request.call_count, 1)
            with patch("likes_to_main.time.time", return_value=10000000000):
                restarted._access_token = Mock(return_value="test")
                restarted.session.request.return_value.ok = True
                restarted.session.request.return_value.json.return_value = {"items": []}
                self.assertEqual(restarted.get_liked_songs(50)["tracks"], [])
            self.assertEqual(restarted.session.request.call_count, 1)

    def test_unchanged_source_does_not_read_entire_main(self):
        client = FakeYouTube([C], [C])
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "likes.json"
            save_checkpoints(checkpoint, [C])
            with patch.object(client, "get_playlist", side_effect=AssertionError("unnecessary Main read")):
                self.assertEqual(sync(client, "MainId", checkpoint), [])

    def test_youtube_source_starts_now_without_importing_old_video_likes(self):
        client = FakeYouTube([C], [C], youtube_liked=[A, B, C], music_ids={A, B, C})
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "youtube.json"
            self.assertEqual(sync(client, "MainId", checkpoint, youtube=True, dry_run=True), [])
            self.assertFalse(checkpoint.exists())
            self.assertEqual(sync(client, "MainId", checkpoint, youtube=True), [])
            self.assertFalse(has_new_likes(client, checkpoint, youtube=True))
            self.assertEqual(client.main, [C])

    def test_youtube_music_filter_remembers_skipped_videos_and_preserves_order(self):
        client = FakeYouTube([C], [C], youtube_liked=[A, B, D, C], music_ids={A, D})
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "youtube.json"
            save_checkpoints(checkpoint, [C])
            self.assertTrue(has_new_likes(client, checkpoint, youtube=True))
            self.assertEqual(sync(client, "MainId", checkpoint, youtube=True), [A, D])
            self.assertEqual(client.main, [A, D, C])
            self.assertEqual(client.added, [D, A])
            self.assertIn(B, load_checkpoints(checkpoint))
            self.assertFalse(has_new_likes(client, checkpoint, youtube=True))
            self.assertEqual(sync(client, "MainId", checkpoint, youtube=True), [])

    def test_music_classification_failure_keeps_youtube_checkpoint_for_retry(self):
        client = FakeYouTube([C], [C], youtube_liked=[A, C])
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "youtube.json"
            save_checkpoints(checkpoint, [C])
            with patch.object(client, "is_music_video", side_effect=RuntimeError("network")):
                with self.assertRaises(RuntimeError):
                    sync(client, "MainId", checkpoint, youtube=True)
            self.assertEqual(load_checkpoints(checkpoint), {C})
            self.assertEqual(client.added, [])

    def test_cli_merges_sources_without_duplicates_in_like_order(self):
        client = FakeYouTube([A, C], [C], youtube_liked=[A, B, C], music_ids={A, B, C})
        with tempfile.TemporaryDirectory() as tmp:
            save_checkpoints(Path(tmp) / "youtube_likes_checkpoint.json", [C])
            save_checkpoints(Path(tmp) / "likes_checkpoint.json", [C])
            env = {"CONFIG_DIR": tmp, "PLAYLIST_URL": "https://music.youtube.com/playlist?list=MainId",
                   "YOUTUBE_LIKES_TO_MAIN": "true"}
            with patch.dict(os.environ, env), patch("likes_to_main.YouTubeClient", return_value=client), patch("sys.argv", ["likes_to_main.py", "--signal-added"]):
                self.assertEqual(main(), 10)
            self.assertEqual(client.main, [A, B, C])
            self.assertEqual(client.added, [B, A])

    def test_secondary_source_failure_does_not_block_music_likes(self):
        client = FakeYouTube([A, C], [C])
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CONFIG_DIR": tmp, "PLAYLIST_URL": "https://music.youtube.com/playlist?list=MainId",
                   "YOUTUBE_LIKES_TO_MAIN": "true"}
            with patch.dict(os.environ, env), patch("likes_to_main.YouTubeClient", return_value=client), patch.object(client, "get_youtube_liked_songs", side_effect=RuntimeError("network")), patch("sys.argv", ["likes_to_main.py", "--signal-added"]):
                self.assertEqual(main(), 10)
            self.assertEqual(client.main, [A, C])

    def test_probe_only_reads_latest_like_and_checkpoint(self):
        client = FakeYouTube([A, B, C], [B, C])
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            self.assertTrue(has_new_likes(client, checkpoint))
            sync(client, "MainId", checkpoint)
            self.assertFalse(has_new_likes(client, checkpoint))
            client.liked.insert(0, D)
            self.assertTrue(has_new_likes(client, checkpoint))

    def test_bootstrap_copies_only_prefix_and_preserves_newest_first(self):
        client = FakeYouTube([A, B, C, D], [C, D, E])
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            self.assertEqual(sync(client, "MainId", checkpoint), [A, B])
            self.assertEqual(client.main, [A, B, C, D, E])
            self.assertTrue(checkpoint.exists())
            self.assertEqual(sync(client, "MainId", checkpoint), [])
            self.assertEqual(client.added, [B, A])

    def test_removed_like_does_not_remove_main_or_lose_cursor(self):
        client = FakeYouTube([A, B, C], [A, B, C])
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            sync(client, "MainId", checkpoint)
            client.liked = [D, B, C]  # A was unliked; B remains as checkpoint
            self.assertEqual(sync(client, "MainId", checkpoint), [D])
            self.assertEqual(client.main, [D, A, B, C])

    def test_manually_added_newer_like_is_not_duplicated(self):
        client = FakeYouTube([C], [C])
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            sync(client, "MainId", checkpoint)
            client.liked = [A, B, C]
            client.main = [A, E, C]  # A was already added manually
            self.assertEqual(sync(client, "MainId", checkpoint), [B])
            self.assertEqual(client.main, [B, A, E, C])

    def test_failed_write_keeps_old_checkpoint_for_retry(self):
        client = FakeYouTube([A, B, C], [C], fail_on=A)
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "checkpoint.json"
            with self.assertRaises(RuntimeError):
                sync(client, "MainId", checkpoint)
            self.assertTrue(checkpoint.exists())
            self.assertEqual(client.main, [B, C])
            client.fail_on = None
            self.assertEqual(sync(client, "MainId", checkpoint), [A])
            self.assertEqual(client.main, [A, B, C])

    def test_no_anchor_stops_instead_of_copying_history(self):
        client = FakeYouTube([A, B], [C])
        with self.assertRaisesRegex(RuntimeError, "контрольная позиция"):
            scan_new_likes(client, {C}, None)


if __name__ == "__main__":
    unittest.main()
