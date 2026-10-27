#!/usr/bin/env python3
"""Copy only new liked tracks to the top of Main; never change Liked Songs."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests


VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHECKPOINT_COUNT = 50
MAX_SCAN = 5000
API_ROOT = "https://www.googleapis.com/youtube/v3/"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class QuotaExceeded(RuntimeError):
    pass


def next_quota_reset():
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    return (now + timedelta(days=1)).replace(hour=0, minute=2, second=0, microsecond=0).timestamp()


def playlist_id(url):
    values = parse_qs(urlsplit(url).query).get("list", [])
    if len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", values[0]):
        raise ValueError("PLAYLIST_URL должен содержать один корректный параметр list")
    return values[0]


def ids_from_tracks(playlist):
    tracks = playlist.get("tracks")
    if not isinstance(tracks, list):
        raise ValueError("YouTube не вернул список треков")
    return [item["videoId"] for item in tracks
            if isinstance(item, dict) and VIDEO_ID.fullmatch(item.get("videoId") or "")]


def track_entries(playlist):
    return [item for item in playlist["tracks"]
            if isinstance(item, dict) and VIDEO_ID.fullmatch(item.get("videoId") or "")]


class YouTubeClient:
    """Adapter around the official YouTube Data API and device OAuth token."""

    def __init__(self, config_dir):
        self.quota_file = config_dir / "youtube_quota.json"
        self.token_file = config_dir / "oauth.json"
        if not self.token_file.is_file():
            raise FileNotFoundError(f"Нет OAuth-файла {self.token_file}")
        self.client_id = os.environ.get("YTM_OAUTH_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("YTM_OAUTH_CLIENT_SECRET", "").strip()
        if not self.client_id or not self.client_secret:
            client_file = config_dir / "oauth_client.json"
            if client_file.is_file():
                credentials = json.loads(client_file.read_text(encoding="utf-8"))
                self.client_id = self.client_id or credentials.get("client_id", "").strip()
                self.client_secret = self.client_secret or credentials.get("client_secret", "").strip()
        if not self.client_id or not self.client_secret:
            raise ValueError("Нужны OAuth client ID и secret в .env или /config/oauth_client.json")
        self.token = json.loads(self.token_file.read_text(encoding="utf-8"))
        self.session = requests.Session()

    def _access_token(self, force_refresh=False):
        if not force_refresh and self.token.get("expires_at", 0) > time.time() + 60:
            return self.token["access_token"]
        response = self.session.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.token["refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=30)
        if not response.ok:
            raise RuntimeError(f"Не удалось обновить OAuth-токен: HTTP {response.status_code}")
        fresh = response.json()
        self.token["access_token"] = fresh["access_token"]
        self.token["expires_at"] = int(time.time()) + fresh["expires_in"]
        temporary = self.token_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.token) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.token_file)
        return self.token["access_token"]

    def _request(self, method, resource, params=None, body=None):
        if self.quota_file.exists():
            resume_at = json.loads(self.quota_file.read_text())["resume_at"]
            if time.time() < resume_at:
                raise QuotaExceeded("Квота YouTube исчерпана; автоматический повтор после "
                                    + datetime.fromtimestamp(resume_at).isoformat(timespec="minutes"))
        for attempt in range(2):
            response = self.session.request(
                method, API_ROOT + resource, params=params, json=body,
                headers={"Authorization": "Bearer " + self._access_token(attempt > 0)},
                timeout=30,
            )
            if response.status_code == 401 and attempt == 0:
                continue
            if not response.ok:
                error = response.json().get("error", {})
                if any(item.get("reason") in {"quotaExceeded", "dailyLimitExceeded"}
                       for item in error.get("errors", [])):
                    temporary = self.quota_file.with_suffix(".tmp")
                    temporary.write_text(json.dumps({"resume_at": next_quota_reset()}) + "\n")
                    os.chmod(temporary, 0o600)
                    temporary.replace(self.quota_file)
                    raise QuotaExceeded("Квота YouTube исчерпана; позиция лайков сохранена, "
                                        "перенос продолжится после сброса квоты")
                raise RuntimeError(f"YouTube API {resource}: HTTP {response.status_code}; "
                                   f"{error.get('message', 'ошибка запроса')}")
            return response.json()
        raise RuntimeError("YouTube API: не удалось обновить авторизацию")

    def owns_playlist(self, target_id):
        channels = self._request("GET", "channels", {"part": "id", "mine": "true"})
        channel_ids = {item["id"] for item in channels.get("items", [])}
        playlists = self._request("GET", "playlists", {"part": "snippet", "id": target_id})
        return any(item.get("snippet", {}).get("channelId") in channel_ids
                   for item in playlists.get("items", []))

    def _playlist_items(self, playlist_id, limit=None):
        tracks = []
        page_token = None
        seen_tokens = set()
        scanned = 0
        while limit is None or len(tracks) < limit:
            params = {"part": "contentDetails", "playlistId": playlist_id,
                      "maxResults": min(50, (limit - scanned)) if limit else 50}
            if page_token:
                params["pageToken"] = page_token
            page = self._request("GET", "playlistItems", params)
            items = page.get("items", [])
            scanned += len(items)
            for item in items:
                video_id = item.get("contentDetails", {}).get("videoId")
                if video_id and VIDEO_ID.fullmatch(video_id):
                    tracks.append({"videoId": video_id})
            page_token = page.get("nextPageToken")
            if not page_token:
                break
            if not items or page_token in seen_tokens or len(seen_tokens) >= 199:
                raise RuntimeError("YouTube вернул пустую/повторяющуюся страницу или слишком большой список; "
                                   "чтение остановлено для защиты квоты")
            seen_tokens.add(page_token)
            # Count raw playlist slots: deleted/private entries may have no videoId.
            # Never walk the entire liked history just to fill one valid result.
            if limit is not None and scanned >= limit:
                break
        return {"tracks": tracks[:limit] if limit is not None else tracks,
                "hasMore": bool(page_token)}

    def get_playlist(self, target_id, limit=None):
        return self._playlist_items(target_id, limit)

    def get_liked_songs(self, limit=50):
        # LM is music likes; LL also includes regular YouTube likes.
        return self._playlist_items("LM", limit)

    def get_youtube_liked_songs(self, limit=50):
        return self._playlist_items("LL", limit)

    def is_music_video(self, video_id):
        # YouTube categories are not reliable: ordinary videos can be marked
        # Music, while actual songs may use Entertainment. Ask YT Music itself.
        from ytmusicapi import YTMusic
        if not hasattr(self, "music_client"):
            self.music_client = YTMusic()
        details = self.music_client.get_song(video_id).get("videoDetails")
        if not isinstance(details, dict) or not details.get("videoId"):
            raise RuntimeError(f"YouTube Music не вернул тип записи {video_id}; повторим позже")
        # Keep the originally liked ID and version, even if YT Music maps it
        # to a different official recording in its metadata response.
        return details.get("musicVideoType") in {
            "MUSIC_VIDEO_TYPE_ATV", "MUSIC_VIDEO_TYPE_OMV", "MUSIC_VIDEO_TYPE_UGC"}

    def insert_playlist_item(self, target_id, video_id):
        body = {"snippet": {"playlistId": target_id,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        item = self._request("POST", "playlistItems", {"part": "snippet"}, body)
        if item.get("snippet", {}).get("resourceId", {}).get("videoId") != video_id:
            raise RuntimeError(f"YouTube API не подтвердил добавление {video_id}")
        return item


def load_checkpoints(path):
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = data.get("recent_liked_ids")
    if not isinstance(ids, list) or not ids or not all(
            isinstance(vid, str) and VIDEO_ID.fullmatch(vid) for vid in ids):
        raise ValueError(f"Некорректный файл контрольных позиций: {path}")
    return set(ids)


def save_checkpoints(path, liked_ids):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"recent_liked_ids": liked_ids[:CHECKPOINT_COUNT]},
                                    ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def has_new_likes(client, checkpoint_file, youtube=False):
    """Read at most 50 raw slots, even if some liked videos are unavailable."""
    checkpoints = load_checkpoints(checkpoint_file)
    if checkpoints is None:
        return True
    getter = client.get_youtube_liked_songs if youtube else client.get_liked_songs
    latest = ids_from_tracks(getter(limit=50))
    return bool(latest and latest[0] not in checkpoints)


def scan_new_likes(client, known_main_ids, checkpoints, youtube=False):
    """Read only the newest prefix, stopping at a previous liked track."""
    limit = 50
    while True:
        getter = client.get_youtube_liked_songs if youtube else client.get_liked_songs
        liked = getter(limit=limit)
        ids = ids_from_tracks(liked)
        anchors = checkpoints if checkpoints is not None else known_main_ids
        anchor = next((i for i, vid in enumerate(ids) if vid in anchors), None)
        if anchor is not None:
            return ids, anchor
        if not liked.get("hasMore", len(ids) >= limit) or limit >= MAX_SCAN:
            raise RuntimeError("Не найдена контрольная позиция в «Понравившихся»; "
                               "перенос остановлен, чтобы не копировать всю историю")
        limit = min(limit * 2, MAX_SCAN)


def sync(client, target_id, checkpoint_file, dry_run=False, youtube=False):
    checkpoints = load_checkpoints(checkpoint_file)
    if youtube and checkpoints is None:
        # Enabling the additional source must not import years of video likes.
        latest = ids_from_tracks(client.get_youtube_liked_songs(limit=50))
        if latest and not dry_run:
            save_checkpoints(checkpoint_file, latest)
        print("YouTube likes: начальная позиция; история не переносится", flush=True)
        return []
    scanned_likes = None
    if checkpoints is not None:
        scanned_likes = scan_new_likes(client, set(), checkpoints, youtube)
        if scanned_likes[1] == 0:
            if not dry_run:
                save_checkpoints(checkpoint_file, scanned_likes[0])
            print("Новых лайков нет", flush=True)
            return []
    if not client.owns_playlist(target_id):
        raise RuntimeError("Авторизованный аккаунт не владеет плейлистом Main")
    current = track_entries(client.get_playlist(target_id))
    known_main_ids = {item["videoId"] for item in current}
    liked_ids, anchor = scanned_likes or scan_new_likes(client, known_main_ids, checkpoints, youtube)
    # Если трек уже был добавлен в Main вручную, не дублируем его. Сохраняем
    # порядок остальных лайков перед найденной контрольной позицией.
    prefix = list(dict.fromkeys(liked_ids[:anchor]))
    missing = [vid for vid in prefix if vid not in known_main_ids]
    if youtube:
        missing = [vid for vid in missing if client.is_music_video(vid)]
        print(f"YouTube likes: новых записей {anchor}; новых музыкальных {len(missing)}", flush=True)
    print(f"Main: {len(known_main_ids)}; новых лайков до контрольной позиции: "
          f"{anchor}; к добавлению: {len(missing)}", flush=True)
    if dry_run:
        return missing

    if missing and checkpoints is None:
        # Keep the original boundary across a partial first run. New items in Main
        # must not become bootstrap anchors if a write fails midway through.
        save_checkpoints(checkpoint_file, [liked_ids[anchor]])

    for index, vid in enumerate(reversed(missing), 1):
        # Main uses YouTube's "newest first" sorting. An explicit position is
        # rejected unless the playlist uses manual sorting. Reads can lag writes.
        client.insert_playlist_item(target_id, vid)
        for check in range(5):
            actual_top = ids_from_tracks(client.get_playlist(target_id, limit=1))
            if actual_top == [vid]:
                break
            if check < 4:
                time.sleep(2)
        else:
            raise RuntimeError(f"Новый трек {vid} не оказался первым в Main; "
                               "дальнейший перенос остановлен")
        known_main_ids.add(vid)
        print(f"Добавлено в Main: {index}/{len(missing)} ({vid})", flush=True)

    save_checkpoints(checkpoint_file, liked_ids)
    return missing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="оценить без изменений")
    parser.add_argument("--probe", action="store_true", help="проверить верхнюю запись источников лайков")
    parser.add_argument("--signal-added", action="store_true", help="код 10, если треки добавлены в Main")
    args = parser.parse_args()
    added = []
    try:
        config = Path(os.environ.get("CONFIG_DIR", "/config"))
        client = YouTubeClient(config)
        checkpoint = config / "likes_checkpoint.json"
        youtube_checkpoint = config / "youtube_likes_checkpoint.json"
        youtube_enabled = os.environ.get("YOUTUBE_LIKES_TO_MAIN", "false").lower() == "true"
        if args.probe:
            changed = has_new_likes(client, checkpoint)
            if youtube_enabled and not changed:
                changed = has_new_likes(client, youtube_checkpoint, youtube=True)
            return 10 if changed else 0
        target_id = playlist_id(os.environ["PLAYLIST_URL"])
        youtube_failed = False
        if youtube_enabled:
            try:
                # LL includes music likes too. Handle its chronological prefix
                # first, so a mixed batch is inserted in the original order.
                added += sync(client, target_id, youtube_checkpoint, args.dry_run, youtube=True)
            except Exception as exc:
                print(f"[likes-to-main] Проверка YouTube likes не удалась: {exc}", file=sys.stderr, flush=True)
                youtube_failed = True
                if isinstance(exc, QuotaExceeded):
                    raise
        added += sync(client, target_id, checkpoint, args.dry_run)
        if args.signal_added and added and not args.dry_run:
            return 10
        if youtube_failed:
            return 1
    except Exception as exc:
        print(f"[likes-to-main] Ошибка: {exc}", file=sys.stderr, flush=True)
        if args.signal_added and added and not args.dry_run:
            return 10
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
