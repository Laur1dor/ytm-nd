#!/usr/bin/env python3
"""
Синхронизация публичного плейлиста YouTube Music в библиотеку Navidrome.

Архитектура:
  * СПИСОК и ПОРЯДОК треков — через YouTube Data API (ключ не протухает).
    Резерв — yt-dlp+cookies (анонимно большой плейлист обрезается до ~100).
  * СКАЧИВАНИЕ — yt-dlp клиентом android_vr БЕЗ cookies (отдаёт opus/m4a без
    nsig-челленджа). Возрастные/недоступные обрабатываются отдельно (бэкфилл).
  * Все треки тегируются как один альбом (ALBUM/ALBUMARTIST) — чтобы в Navidrome
    они не разваливались на сотни одиночных альбомов.
  * Порядок плейлиста сохраняется в .m3u (Navidrome импортирует как плейлист).

Файлы состояния в /config:
  archive.txt        — что уже скачано (yt-dlp download-archive)
  idmap.tsv          — videoId -> относительный путь файла (строится из тегов на диске)
  unavailable.txt    — id, которые качать бессмысленно (удалены/заблокированы) — пропускаем
  replacements.tsv   — origId -> относительный путь файла-замены (для .m3u)
  cookies.txt        — опционально, резерв для листинга
  rclone.conf        — опционально, для зеркалирования в S3

Режим только добавления: убранные из плейлиста треки с диска не удаляются.
"""
import os
import re
import sys
import subprocess
from pathlib import Path
from datetime import datetime

AUDIO_EXTS = {".opus", ".m4a", ".mp3", ".ogg", ".webm", ".flac", ".aac", ".oga"}
ALBUM_NAME = os.environ.get("ALBUM_NAME", "YTM").strip()
ALBUM_ARTIST = os.environ.get("ALBUM_ARTIST", "YouTube Music").strip()


def log(*args):
    print(f"[sync {datetime.now().isoformat(timespec='seconds')}]", *args, flush=True)


def truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "").strip()
MUSIC_ROOT = Path(os.environ.get("MUSIC_ROOT", "/music"))
SUBDIR = os.environ.get("MUSIC_SUBDIR", "YouTube Music").strip()
CONFIG = Path(os.environ.get("CONFIG_DIR", "/config"))
PLAYER_CLIENT = os.environ.get("YT_PLAYER_CLIENT", "android_vr").strip()
YT_API_KEY = os.environ.get("YT_API_KEY", "").strip()
REQUIRE_SHARE = truthy(os.environ.get("REQUIRE_SHARE", "true"))
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "").strip()

DEST = MUSIC_ROOT / SUBDIR
ARCHIVE = CONFIG / "archive.txt"
IDMAP = CONFIG / "idmap.tsv"
BATCH = CONFIG / "_batch.txt"
UNAVAIL = CONFIG / "unavailable.txt"
REPL = CONFIG / "replacements.tsv"
COOKIES = CONFIG / "cookies.txt"
RCLONE_CONF = CONFIG / "rclone.conf"
M3U = MUSIC_ROOT / f"{SUBDIR}.m3u"

if not PLAYLIST_URL:
    log("ОШИБКА: не задан PLAYLIST_URL. Укажи его в .env и перезапусти контейнер.")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Защита хранилища
# --------------------------------------------------------------------------- #
def assert_share_mounted():
    """Если REQUIRE_SHARE — гарантируем, что /music это живая сетевая шара,
    а не пустая локальная папка (защита от записи на локальный диск)."""
    if not REQUIRE_SHARE:
        log("REQUIRE_SHARE=false — пишу в DEST как есть (локально/иное хранилище)")
        return
    NET_FS = {"smb2", "smb3", "cifs", "smbfs", "nfs", "nfs4"}
    try:
        fstype = subprocess.run(["stat", "-f", "-c", "%T", str(MUSIC_ROOT)],
                                capture_output=True, text=True).stdout.strip()
    except Exception as e:
        fstype = f"err:{e}"
    marker_ok = (MUSIC_ROOT / ".ytm_share_ok").exists()
    if fstype not in NET_FS or not marker_ok:
        log(f"ОТМЕНА: шара {MUSIC_ROOT} недоступна "
            f"(fstype={fstype!r}, marker={marker_ok}). Пропускаю прогон, локально не пишу.")
        sys.exit(2)
    log(f"шара примонтирована ок (fstype={fstype}, marker=да)")


# --------------------------------------------------------------------------- #
# Карты состояния
# --------------------------------------------------------------------------- #
def load_tsv(path: Path) -> dict:
    m = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "\t" in line:
                k, v = line.split("\t", 1)
                m[k] = v
    return m


def save_tsv(path: Path, m: dict):
    path.write_text("".join(f"{k}\t{v}\n" for k, v in m.items()), encoding="utf-8")


def load_set(path: Path) -> set:
    if not path.exists():
        return set()
    return {l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def rel_of(p) -> str:
    return os.path.relpath(str(p), str(MUSIC_ROOT)).replace(os.sep, "/")


def probe_video_id(path: Path):
    """videoId из вшитого тега purl/comment (ffprobe)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-show_entries", "stream_tags=purl,comment:format_tags=purl,comment",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    m = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', out)
    return m.group(1) if m else None


def reconcile_idmap_from_disk(idmap: dict) -> dict:
    """Сверяет карту id->путь с реальными файлами. Новые опознаёт по purl-тегу.
    Плейлист строится из того, что реально на диске."""
    known = set(idmap.values())
    probed = 0
    for p in sorted(DEST.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
            continue
        rel = rel_of(p)
        if rel in known:
            continue
        vid = probe_video_id(p)
        probed += 1
        if vid:
            idmap[vid] = rel
            known.add(rel)
    if probed:
        log(f"опознано новых файлов с диска: {probed}")
    return idmap


def tag_files(ordered_rels):
    """Один проход по файлам в порядке плейлиста:
      * единый ALBUM/ALBUMARTIST — чтобы в Navidrome был один альбом, а не сотни;
      * TRACKNUMBER = позиция в плейлисте — чтобы и АЛЬБОМ показывался в порядке
        плейлиста (свежее сверху), а не по алфавиту исполнителей.
    Пишет файл только если теги реально меняются."""
    try:
        from mutagen.oggopus import OggOpus
        from mutagen.oggvorbis import OggVorbis
        from mutagen.mp4 import MP4
        from mutagen.easyid3 import EasyID3
        from mutagen.flac import FLAC
    except Exception as e:
        log(f"mutagen недоступен, пропускаю теги: {e}")
        return
    total = len(ordered_rels)
    changed = 0
    for i, rel in enumerate(ordered_rels, start=1):
        p = MUSIC_ROOT / rel
        if not p.exists():
            continue
        ext = p.suffix.lower()
        num = str(i)
        try:
            if ext == ".opus":
                a = OggOpus(p)
                if (a.get("album", [""])[0] == ALBUM_NAME and a.get("albumartist", [""])[0] == ALBUM_ARTIST
                        and a.get("tracknumber", [""])[0] == num):
                    continue
                a["album"] = [ALBUM_NAME]; a["albumartist"] = [ALBUM_ARTIST]; a["tracknumber"] = [num]; a.save()
            elif ext in (".ogg", ".oga"):
                a = OggVorbis(p)
                if (a.get("album", [""])[0] == ALBUM_NAME and a.get("albumartist", [""])[0] == ALBUM_ARTIST
                        and a.get("tracknumber", [""])[0] == num):
                    continue
                a["album"] = [ALBUM_NAME]; a["albumartist"] = [ALBUM_ARTIST]; a["tracknumber"] = [num]; a.save()
            elif ext in (".m4a", ".aac"):
                a = MP4(p)
                t = a.tags or {}
                cur = t.get("trkn", [(0, 0)])[0][0]
                if (t.get("\xa9alb", [""])[:1] == [ALBUM_NAME] and t.get("aART", [""])[:1] == [ALBUM_ARTIST]
                        and cur == i):
                    continue
                a["\xa9alb"] = [ALBUM_NAME]; a["aART"] = [ALBUM_ARTIST]; a["trkn"] = [(i, total)]; a.save()
            elif ext == ".mp3":
                try:
                    a = EasyID3(p)
                except Exception:
                    from mutagen.mp3 import MP3
                    mp3 = MP3(p); mp3.add_tags(); mp3.save(); a = EasyID3(p)
                if (a.get("album", [""])[:1] == [ALBUM_NAME] and a.get("albumartist", [""])[:1] == [ALBUM_ARTIST]
                        and a.get("tracknumber", [""])[:1] == [num]):
                    continue
                a["album"] = ALBUM_NAME; a["albumartist"] = ALBUM_ARTIST; a["tracknumber"] = num; a.save()
            elif ext == ".flac":
                a = FLAC(p)
                if (a.get("album", [""])[0] == ALBUM_NAME and a.get("albumartist", [""])[0] == ALBUM_ARTIST
                        and a.get("tracknumber", [""])[0] == num):
                    continue
                a["album"] = [ALBUM_NAME]; a["albumartist"] = [ALBUM_ARTIST]; a["tracknumber"] = [num]; a.save()
            else:
                continue
            changed += 1
        except Exception as e:
            log(f"не удалось проставить теги {p.name}: {e}")
    if changed:
        log(f"обновлены теги (альбом + номер по порядку плейлиста): {changed}")


# --------------------------------------------------------------------------- #
# Список плейлиста
# --------------------------------------------------------------------------- #
def playlist_id_from_url(url: str) -> str:
    m = re.search(r'[?&]list=([A-Za-z0-9_-]+)', url)
    return m.group(1) if m else url


def ordered_ids_via_api(pid: str, key: str) -> list:
    import json
    import urllib.request
    ids, token = [], ""
    while True:
        url = ("https://www.googleapis.com/youtube/v3/playlistItems"
               f"?part=contentDetails&maxResults=50&playlistId={pid}&key={key}")
        if token:
            url += f"&pageToken={token}"
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)
        for it in data.get("items", []):
            vid = it.get("contentDetails", {}).get("videoId")
            if vid:
                ids.append(vid)
        token = data.get("nextPageToken")
        if not token:
            break
    return ids


def ordered_ids_via_ytdlp() -> list:
    cmd = ["yt-dlp", "--flat-playlist", "--print", "%(id)s"]
    if COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    cmd.append(PLAYLIST_URL)
    res = subprocess.run(cmd, capture_output=True, text=True)
    return [l.strip() for l in res.stdout.splitlines() if l.strip()]


# --------------------------------------------------------------------------- #
# Прогон
# --------------------------------------------------------------------------- #
assert_share_mounted()
DEST.mkdir(parents=True, exist_ok=True)
CONFIG.mkdir(parents=True, exist_ok=True)
if BATCH.exists():
    BATCH.unlink()

# 1) список
ordered_ids = []
if YT_API_KEY:
    try:
        ordered_ids = ordered_ids_via_api(playlist_id_from_url(PLAYLIST_URL), YT_API_KEY)
        log(f"список через YouTube Data API: {len(ordered_ids)} позиций")
    except Exception as e:
        log(f"YouTube Data API не сработал ({e}); пробую yt-dlp")
if not ordered_ids:
    ordered_ids = ordered_ids_via_ytdlp()
    note = "" if COOKIES.exists() else " (без cookies — может быть обрезан до ~100)"
    log(f"список через yt-dlp{note}: {len(ordered_ids)} позиций")
if not ordered_ids:
    log("ОШИБКА: не удалось получить список плейлиста. Прерываю прогон.")
    sys.exit(1)

# 2) скачивание новых (минус заведомо недоступные)
unavailable = load_set(UNAVAIL)
to_get = [v for v in ordered_ids if v not in unavailable]
BATCH.write_text("".join(f"https://www.youtube.com/watch?v={v}\n" for v in to_get), encoding="utf-8")
out_template = str(DEST / "%(artist,uploader)s - %(track,title)s.%(ext)s")
download_cmd = [
    "yt-dlp",
    "--ignore-errors", "--no-overwrites", "--no-progress",
    "--download-archive", str(ARCHIVE),
    "--extractor-args", f"youtube:player_client={PLAYER_CLIENT}",
    "-f", "bestaudio/best", "--extract-audio",
    "--embed-metadata", "--embed-thumbnail",
    "--sleep-requests", "1", "--sleep-interval", "2", "--max-sleep-interval", "6",
    "--retries", "5", "--fragment-retries", "10",
    "-o", out_template, "-a", str(BATCH),
]
log(f"скачиваю (client={PLAYER_CLIENT}, без cookies, пропуск недоступных: {len(unavailable)})...")
rc = subprocess.run(download_cmd).returncode
log(f"yt-dlp скачивание завершено, rc={rc}")

# 3) сверка карты по диску (теги проставим ниже, по финальному порядку)
idmap = reconcile_idmap_from_disk(load_tsv(IDMAP))
save_tsv(IDMAP, idmap)
log(f"всего в карте id->файл: {len(idmap)}")

# 4) опциональное зеркало в S3/удалённое хранилище
if RCLONE_REMOTE:
    cmd = ["rclone", "copy", str(DEST), f"{RCLONE_REMOTE.rstrip('/')}/{SUBDIR}", "--transfers", "4"]
    if RCLONE_CONF.exists():
        cmd += ["--config", str(RCLONE_CONF)]
    log(f"rclone -> {RCLONE_REMOTE}/{SUBDIR}")
    subprocess.run(cmd)

# 5) .m3u в порядке плейлиста (с учётом замен) + «лишние» файлы в конец
replacements = load_tsv(REPL)
lines, pruned = [], False
for vid in ordered_ids:
    rel = idmap.get(vid) or replacements.get(vid)
    if not rel:
        continue
    if not (MUSIC_ROOT / rel).exists():
        if vid in idmap:
            idmap.pop(vid, None); pruned = True
        continue
    lines.append(rel)

# файлы, лежащие в папке, но не входящие в плейлист (предсуществующие/ручные) — в конец
in_playlist = set(lines)
extras = sorted(rel_of(p) for p in DEST.rglob("*")
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS and rel_of(p) not in in_playlist)
if extras:
    log(f"добавляю в конец плейлиста файлов вне плейлиста: {len(extras)}")
lines += extras

M3U.write_text("".join(f"{l}\n" for l in lines), encoding="utf-8")
if pruned:
    save_tsv(IDMAP, idmap)

# единый альбом + номера треков по финальному порядку (свежее сверху)
tag_files(lines)
log(f"записан плейлист {M3U} — {len(lines)} треков")
