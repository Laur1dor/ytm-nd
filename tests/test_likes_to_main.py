import tempfile
import unittest
from pathlib import Path

from likes_to_main import has_new_likes, scan_new_likes, sync


class FakeYouTube:
    def __init__(self, liked, main, fail_on=None):
        self.liked = liked
        self.main = main
        self.fail_on = fail_on
        self.added = []

    def owns_playlist(self, playlist_id):
        return playlist_id == "MainId"

    def get_playlist(self, playlist_id, limit=None):
        assert playlist_id == "MainId"
        ids = self.main[:limit] if limit is not None else self.main
        return {"tracks": [{"videoId": vid} for vid in ids]}

    def get_liked_songs(self, limit=100):
        return {"tracks": [{"videoId": vid} for vid in self.liked[:limit]]}

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
