#!/usr/bin/env python3
"""
Импорт интернет-радиостанций из radio-browser.info в Navidrome, СГРУППИРОВАННЫХ
по категориям, с приведением к HTTPS.

Почему HTTPS: Navidrome обычно открыт по https, а браузер (mixed-content) и iOS
(ATS) блокируют http-потоки. Поэтому:
  * если у станции уже есть https — берём его;
  * если только http — пробуем https-вариант (тот же адрес со схемой https) и
    РЕАЛЬНО проверяем, что поток отвечает (параллельный тест). Работает — берём,
    нет — выбрасываем.

Категория зашивается в начало имени ("🎧 Электроника · Dance Wave"): при сортировке
по имени станции группируются по категориям. Перед заливкой таблица radio чистится.

Запуск (Navidrome остановлен):
  docker stop navidrome
  cp /home/navidrome/navidrome.db /home/navidrome/navidrome.db.bak_$(date +%s)
  docker run --rm -v /home/navidrome:/nd \
    -v /home/ytm-sync/scripts/import_radio.py:/import_radio.py:ro \
    --entrypoint python3 ytm-sync:latest /import_radio.py
  docker start navidrome
"""
import json
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

DB = "/nd/navidrome.db"
UA = "ytm-sync-radio-import/3.0 (+https://github.com/Laur1dor/ytm-nd)"
HOSTS = [
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
]

PER_TAG = 70
RU_LIMIT = 400
TOP_LIMIT = 400
TEST_WORKERS = 40   # параллельных проверок https

GENRE_CATEGORIES = [
    ("\U0001F3A7 Электроника", ["electronic", "house", "techno", "trance", "drum and bass", "dance", "edm"]),
    ("\U0001F45F Фонк",        ["phonk"]),
    ("\U0001F3A4 Хип-хоп",     ["hip hop", "rap", "trap"]),
    ("\U0001F3B8 Рок",         ["rock", "metal", "indie", "punk", "alternative"]),
    ("\U0001F3B9 Поп",         ["pop", "80s", "top 40", "hits"]),
    ("\U0001F3B7 Джаз и соул", ["jazz", "funk", "soul", "blues"]),
    ("\U0001F319 Чилл и лаунж", ["lofi", "chillout", "ambient", "lounge", "relax"]),
    ("\U0001F3BB Классика",    ["classical"]),
    ("\U0001F334 Регги",       ["reggae"]),
]


def fetch(path):
    """Запрос к radio-browser с ретраями (его DNS периодически моргает)."""
    last = None
    for attempt in range(4):
        for h in HOSTS:
            try:
                req = urllib.request.Request(h + path, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except Exception as e:
                last = e
        time.sleep(3)  # бэкофф перед новым кругом по хостам
    print(f"[radio] не удалось получить {path}: {last}")
    return []


def tag_stations(tag, limit):
    return fetch(f"/json/stations/bytagexact/{urllib.parse.quote(tag)}"
                 f"?limit={limit}&order=votes&reverse=true&hidebroken=true")


def candidate(s):
    """(name, url, needs_test, home) или None.
    url — https напрямую (needs_test=False) либо https-вариант http-ссылки
    (needs_test=True, потом проверим, что отвечает)."""
    name = " ".join((s.get("name") or "").split()).strip()
    if not name or s.get("lastcheckok") not in (1, "1", True):
        return None
    res = (s.get("url_resolved") or "").strip()
    orig = (s.get("url") or "").strip()
    home = (s.get("homepage") or "").strip()[:300]
    for u in (res, orig):                       # уже https
        if u.lower().startswith("https://"):
            return name[:160], u, False, home
    for u in (res, orig):                       # только http -> кандидат https
        if u.lower().startswith("http://"):
            return name[:160], "https://" + u[7:], True, home
    return None


def https_works(url):
    """Поток реально отвечает по https (с проверкой TLS, как в браузере)."""
    try:
        out = subprocess.run(
            ["curl", "-sL", "--max-time", "6", "-A", "Mozilla/5.0",
             "-o", "/dev/null", "-w", "%{size_download}|%{content_type}", url],
            capture_output=True, text=True, timeout=10).stdout.strip()
        size_s, _, ctype = out.partition("|")
        size = int(size_s or 0)
        return size >= 8000 and "html" not in ctype.lower()
    except Exception:
        return False


def main():
    seen_url = set()
    items = []  # (category, name, url, needs_test, home)

    def add(category, station_list):
        for s in station_list:
            c = candidate(s)
            if not c:
                continue
            name, url, needs_test, home = c
            if url in seen_url:
                continue
            seen_url.add(url)
            items.append((category, name, url, needs_test, home))

    for category, tags in GENRE_CATEGORIES:
        for t in tags:
            add(category, tag_stations(t, PER_TAG))
    add("\U0001F1F7\U0001F1FA Россия",
        fetch(f"/json/stations/bycountrycodeexact/RU?limit={RU_LIMIT}&order=votes&reverse=true&hidebroken=true"))
    add("\U0001F525 Популярное", fetch(f"/json/stations/topvote/{TOP_LIMIT}"))

    to_test = [it for it in items if it[3]]
    print(f"[radio] кандидатов всего: {len(items)} (из них http→https на проверку: {len(to_test)})")

    # параллельно проверяем https-варианты http-станций
    if to_test:
        with ThreadPoolExecutor(max_workers=TEST_WORKERS) as ex:
            ok = list(ex.map(lambda it: https_works(it[2]), to_test))
        good = {it[2] for it, passed in zip(to_test, ok) if passed}
        print(f"[radio] из http→https прошли проверку: {len(good)}/{len(to_test)}")
    else:
        good = set()

    final = [it for it in items if not it[3] or it[2] in good]
    cnt = Counter(c for c, *_ in final)
    print(f"[radio] итог станций (все https): {len(final)}")
    for c, n in cnt.most_common():
        print(f"    {c}: {n}")

    con = sqlite3.connect(DB)
    con.execute("delete from radio")
    now = datetime.now(timezone.utc).isoformat(sep=" ")
    used = set()
    for category, name, url, _needs, home in final:
        full = f"{category} · {name}"[:240]
        base, k = full, 2
        while full.lower() in used:
            full = f"{base} ({k})"
            k += 1
        used.add(full.lower())
        con.execute(
            "insert into radio (id, name, stream_url, home_page_url, created_at, updated_at) "
            "values (?,?,?,?,?,?)",
            (uuid.uuid4().hex[:22], full, url, home, now, now),
        )
    con.commit()
    print(f"[radio] залито: {con.execute('select count(*) from radio').fetchone()[0]}")


if __name__ == "__main__":
    main()
