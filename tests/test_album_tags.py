"""Regression tests for the tag pass, without starting a download."""
import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeMP4(dict):
    saves = 0

    @property
    def tags(self):
        return self

    def save(self):
        self.saves += 1


def tag_pass(root):
    # sync.py is an executable script; load only its tag pass for this test.
    source = Path(__file__).resolve().parents[1] / "sync.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "tag_files")
    namespace = {"MUSIC_ROOT": root, "ALBUM_NAME": "YTM",
                 "ALBUM_ARTIST": "YouTube Music", "log": lambda *args: None}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["tag_files"]


class AlbumTagsTests(unittest.TestCase):
    def test_opus_uses_same_disc_as_mp4_so_track_numbers_define_the_order(self):
        tags = FakeMP4({"album": ["YTM"], "albumartist": ["YouTube Music"],
                        "tracknumber": ["1"], "date": ["2011"]})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "song.opus").touch()
            with patch("mutagen.oggopus.OggOpus", return_value=tags):
                tag_pass(root)(["song.opus"])
                self.assertEqual(tags["discnumber"], ["1"])
                self.assertEqual(tags["date"], ["2011"])

    def test_mp4_release_date_is_removed_even_when_album_and_number_match(self):
        tags = FakeMP4({"\xa9alb": ["YTM"], "aART": ["YouTube Music"],
                        "trkn": [(1, 754)], "\xa9day": ["20220210"],
                        "\xa9ART": ["Strawberry Guy"]})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "song.m4a").touch()
            run = tag_pass(root)
            with patch("mutagen.mp4.MP4", return_value=tags):
                run(["song.m4a"])
                self.assertNotIn("\xa9day", tags)
                self.assertEqual(tags["----:com.apple.iTunes:YTM_SOURCE_DATE"], [b"20220210"])
                self.assertEqual(tags["\xa9ART"], ["Strawberry Guy"])
                self.assertEqual(tags["disk"], [(1, 1)])
                self.assertEqual(tags["trkn"][0][0], 1)
                self.assertEqual(tags.saves, 1)
                run(["song.m4a"])
                self.assertEqual(tags.saves, 1)

    def test_failed_tag_write_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "song.m4a").touch()
            with patch("mutagen.mp4.MP4", side_effect=OSError("write failed")):
                with self.assertRaisesRegex(RuntimeError, "1"):
                    tag_pass(root)(["song.m4a"])


if __name__ == "__main__":
    unittest.main()
