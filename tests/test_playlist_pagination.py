"""Regression for YouTube's continuation after the final unavailable item."""
import ast
import io
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from likes_to_main import YouTubeClient


def main_pages():
    # Reproduces the live response: 778 advertised slots, 777 visible IDs,
    # and a nextPageToken even on the final short page.
    pages = []
    for index in range(16):
        count = 50 if index < 15 else 27
        pages.append({"items": [{"contentDetails": {"videoId": "AAAAAAAAAAA"}}] * count,
                      "nextPageToken": str(index + 1),
                      "pageInfo": {"totalResults": 778, "resultsPerPage": 50}})
    return pages


def download_reader():
    path = Path(__file__).resolve().parents[1] / "sync.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ordered_ids_via_api")
    namespace = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[fn.name]


class PlaylistPaginationTests(unittest.TestCase):
    def test_oauth_stops_at_advertised_slots_without_fetching_phantom_page(self):
        client = object.__new__(YouTubeClient)
        client._request = Mock(side_effect=main_pages())
        result = client.get_playlist("Main")
        self.assertEqual(len(result["tracks"]), 777)
        self.assertFalse(result["hasMore"])
        self.assertEqual(client._request.call_count, 16)

    def test_download_reader_stops_at_same_boundary(self):
        responses = [io.BytesIO(json.dumps(p).encode()) for p in main_pages()]
        with patch("urllib.request.urlopen", side_effect=responses) as request:
            self.assertEqual(len(download_reader()("Main", "test-key")), 777)
        self.assertEqual(request.call_count, 16)

    def test_download_reader_still_rejects_repeated_pages_before_end(self):
        page = {"items": [{"contentDetails": {"videoId": "AAAAAAAAAAA"}}],
                "nextPageToken": "same", "pageInfo": {"totalResults": 500}}
        responses = [io.BytesIO(json.dumps(page).encode()) for _ in range(2)]
        with patch("urllib.request.urlopen", side_effect=responses) as request:
            with self.assertRaisesRegex(RuntimeError, "защиты квоты"):
                download_reader()("Main", "test-key")
        self.assertEqual(request.call_count, 2)
