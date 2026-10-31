import tempfile
import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.upgrade_audio import improves, install, save
import yt_dlp


class AudioQualityTests(unittest.TestCase):
    def test_scheduled_and_repair_selectors_reject_muxed_low_audio_and_choose_best_source(self):
        from scripts.upgrade_audio import FORMAT
        source = Path(__file__).resolve().parents[1] / "sync.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "download_cmd" for t in n.targets))
        values = assignment.value.elts
        index = next(i for i, n in enumerate(values) if isinstance(n, ast.Constant) and n.value == "-f")
        selectors = [FORMAT, ast.literal_eval(values[index + 1])]
        low = {"format_id": "18", "url": "https://example.invalid/low", "ext": "mp4",
               "vcodec": "avc1", "acodec": "mp4a", "abr": 48}
        aac = {"format_id": "140", "url": "https://example.invalid/aac", "ext": "m4a",
               "vcodec": "none", "acodec": "mp4a", "abr": 128}
        opus = {"format_id": "251", "url": "https://example.invalid/opus", "ext": "webm",
                "vcodec": "none", "acodec": "opus", "abr": 108}
        with yt_dlp.YoutubeDL({"quiet": True}) as downloader:
            for expression in selectors:
                select = downloader.build_format_selector(expression)
                self.assertEqual(list(select({"formats": [low], "incomplete_formats": False})), [])
                chosen = list(select({"formats": [low, aac, opus], "incomplete_formats": False}))
                self.assertEqual(chosen[0]["format_id"], "140")
                full_opus = dict(opus, abr=140)
                chosen = list(select({"formats": [low, aac, full_opus], "incomplete_formats": False}))
                self.assertEqual(chosen[0]["format_id"], "251")
                broken_opus = dict(opus, abr=4)
                chosen = list(select({"formats": [low, broken_opus, aac], "incomplete_formats": False}))
                self.assertEqual(chosen[0]["format_id"], "140")

    def test_no_upscale_or_replacement_with_equivalent_aac(self):
        self.assertFalse(improves(128000, 128000, "140", ".m4a"))
        self.assertFalse(improves(48000, 48000, "18", ".m4a"))
        self.assertFalse(improves(48000, 70000, "250", ".opus"))
        self.assertFalse(improves(128000, 100000, "251", ".opus"))
        self.assertTrue(improves(48000, 128000, "140", ".m4a"))
        self.assertTrue(improves(128000, 132000, "251", ".opus"))

    def prepare(self, tmp):
        root = Path(tmp) / "music"; root.mkdir()
        config = Path(tmp) / "config"; config.mkdir()
        old = root / "track.m4a"; old.write_bytes(b"original")
        staged = config / "better.opus"; staged.write_bytes(b"better-source")
        (config / "idmap.tsv").write_text("id\ttrack.m4a\n")
        (root / "YTM.m3u").write_text("other.opus\ntrack.m4a\nlast.opus\n")
        return root, config, old, staged

    def test_replacement_keeps_backup_and_playlist_position_without_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config, old, staged = self.prepare(tmp)
            backup = config / "originals/id.m4a"
            result = install(old, staged, root, config, "id", backup)
            self.assertEqual(result, "track.opus")
            self.assertEqual(backup.read_bytes(), b"original")
            self.assertFalse(old.exists())
            self.assertEqual((root / "track.opus").read_bytes(), b"better-source")
            self.assertEqual((config / "idmap.tsv").read_text(), "id\ttrack.opus\n")
            self.assertEqual((root / "YTM.m3u").read_text(), "other.opus\ntrack.opus\nlast.opus\n")

    def test_write_failure_restores_mapping_playlist_and_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config, old, staged = self.prepare(tmp)
            calls = 0
            def fail_once(path, text):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated playlist write failure")
                save(path, text)
            with patch("scripts.upgrade_audio.save", side_effect=fail_once):
                with self.assertRaises(OSError):
                    install(old, staged, root, config, "id", config / "originals/id.m4a")
            self.assertEqual(old.read_bytes(), b"original")
            self.assertFalse((root / "track.opus").exists())
            self.assertEqual((config / "idmap.tsv").read_text(), "id\ttrack.m4a\n")
            self.assertEqual((root / "YTM.m3u").read_text(), "other.opus\ntrack.m4a\nlast.opus\n")
