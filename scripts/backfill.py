#!/usr/bin/env python3
"""
Одноразовый бэкфилл недостающих треков плейлиста.

Для каждой позиции плейлиста, которой нет на диске:
  1) пробуем СКАЧАТЬ ОРИГИНАЛ (cookies + EJS, дефолтный клиент) — лечит возрастные;
  2) если оригинал мёртв (удалён/заблокирован/premium) — ИЩЕМ ЗАМЕНУ на YouTube
     по «название + канал» (ytsearch) и качаем её (android_vr), записывая
     replacements.tsv: origId -> путь файла-замены (для .m3u);
  3) что не вышло вообще — пишем в unavailable.txt, чтобы основной sync не долбился.

Запуск (в контейнере образа ytm-sync):
  docker run --rm -v /home/ytm-sync/data:/config -v /mnt/Music:/music \
    -v /home/ytm-sync/.env:/env:ro -v /home/ytm-sync/scripts/backfill.py:/backfill.py:ro \
    --entrypoint python3 ytm-sync:latest /backfill.py
"""
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.request
from pathlib import Path

# Точечный ретрай: python backfill.py <id1> <id2> ... — обработать только эти id
# (игнорируя фильтры have/unavailable), полезно добрать не нашедшиеся с прошлого раза.
ONLY_IDS = set(a.strip() for a in sys.argv[1:] if a.strip())


def clean_query(title: str, chan: str) -> str:
    """Нормализует «фигурный» юникод (𝐀𝐁𝐂 -> ABC), чистит мусор, берёт первые слова."""
    t = unicodedata.normalize("NFKC", title)
    t = re.sub(r'[|/\\]+', ' ', t)
    t = re.sub(r'[^\w\s\-()&]', ' ', t, flags=re.UNICODE)
    t = re.sub(r'\s+', ' ', t).strip()
    t = " ".join(t.split()[:8])
    return f"ytsearch1:{t} {chan}".strip()

ENV = dict(l.strip().split("=", 1) for l in open("/env")
           if "=" in l and not l.strip().startswith("#"))
KEY = ENV["YT_API_KEY"]
PID = re.search(r"list=([\w-]+)", ENV["PLAYLIST_URL"]).group(1)
SUBDIR = ENV.get("MUSIC_SUBDIR", "YTM").strip()

MUSIC = Path("/music")
DEST = MUSIC / SUBDIR
CONFIG = Path("/config")
ARCHIVE = CONFIG / "archive.txt"
IDMAP = CONFIG / "idmap.tsv"
REPL = CONFIG / "replacements.tsv"
UNAVAIL = CONFIG / "unavailable.txt"
COOKIES = CONFIG / "cookies.txt"
TMP = CONFIG / "_bf_path.txt"
OUT_TMPL = str(DEST / "%(artist,uploader)s - %(track,title)s.%(ext)s")

BASE = ["yt-dlp", "--no-progress", "--no-overwrites", "--ignore-errors",
        "--download-archive", str(ARCHIVE), "-f", "bestaudio/best", "--extract-audio",
        "--embed-metadata", "--embed-thumbnail", "--no-warnings",
        "--retries", "3", "-o", OUT_TMPL,
        "--print-to-file", "after_move:%(filepath)s", str(TMP)]


def fetch_items():
    items, tok = [], ""
    while True:
        u = ("https://www.googleapis.com/youtube/v3/playlistItems"
             f"?part=snippet,contentDetails,status&maxResults=50&playlistId={PID}&key={KEY}")
        if tok:
            u += f"&pageToken={tok}"
        d = json.load(urllib.request.urlopen(u, timeout=30))
        for it in d["items"]:
            sn = it["snippet"]
            items.append((it["contentDetails"]["videoId"], sn["title"],
                          sn.get("videoOwnerChannelTitle", "")))
        tok = d.get("nextPageToken")
        if not tok:
            break
    return items


def have_ids():
    s = set()
    if IDMAP.exists():
        for l in IDMAP.read_text(encoding="utf-8").splitlines():
            if "\t" in l:
                s.add(l.split("\t", 1)[0])
    return s


def run_dl(target, with_cookies, client):
    if TMP.exists():
        TMP.unlink()
    cmd = list(BASE) + ["--extractor-args", f"youtube:player_client={client}"]
    if with_cookies and COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    cmd.append(target)
    subprocess.run(cmd, capture_output=True, text=True)
    if TMP.exists():
        p = TMP.read_text(encoding="utf-8").strip().splitlines()
        if p and Path(p[-1]).exists():
            return p[-1]
    return None


def main():
    have = have_ids()
    items = fetch_items()
    if ONLY_IDS:
        missing = [(v, t, c) for v, t, c in items if v in ONLY_IDS]
        print(f"точечный ретрай {len(missing)} id: {sorted(ONLY_IDS)}")
    else:
        missing = [(v, t, c) for v, t, c in items if v not in have]
        print(f"всего {len(items)}, на диске {len(have)}, не хватает {len(missing)}")

    repl = {}
    if REPL.exists():
        for l in REPL.read_text(encoding="utf-8").splitlines():
            if "\t" in l:
                k, val = l.split("\t", 1)
                repl[k] = val
    unavail = set()
    if UNAVAIL.exists():
        unavail = {l.strip() for l in UNAVAIL.read_text(encoding="utf-8").splitlines() if l.strip()}

    report = []
    for vid, title, chan in missing:
        if title in ("Private video", "Deleted video") or not title.strip():
            unavail.add(vid); report.append((vid, "ПРИВАТ/УДАЛЁН — пропуск", title)); continue

        # 1) оригинал (cookies+EJS лечит возрастные)
        path = run_dl(f"https://www.youtube.com/watch?v={vid}", True, "default")
        if path:
            report.append((vid, "ОРИГИНАЛ", os.path.basename(path))); continue

        # 2) замена через поиск
        path = run_dl(clean_query(title, chan), False, "android_vr")
        if path:
            rel = os.path.relpath(path, str(MUSIC)).replace(os.sep, "/")
            repl[vid] = rel
            unavail.add(vid)  # оригинал мёртв
            report.append((vid, "ЗАМЕНА", f"{title}  ->  {os.path.basename(path)}")); continue

        # 3) совсем не вышло
        unavail.add(vid)
        report.append((vid, "НЕ НАЙДЕНО", title))

    # сохранить состояние
    REPL.write_text("".join(f"{k}\t{v}\n" for k, v in repl.items()), encoding="utf-8")
    UNAVAIL.write_text("".join(f"{v}\n" for v in sorted(unavail)), encoding="utf-8")

    print("\n=== ИТОГ БЭКФИЛЛА ===")
    for vid, kind, info in report:
        print(f"{kind:18} {vid}  {info}")
    kinds = {}
    for _, k, _ in report:
        kinds[k.split()[0]] = kinds.get(k.split()[0], 0) + 1
    print("\nсводка:", kinds)


if __name__ == "__main__":
    main()
