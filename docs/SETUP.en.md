# Full setup & operation guide for ytm-sync

[🇷🇺 Русский](SETUP.md) · 🇬🇧 English

A detailed, zero-to-running walkthrough for mirroring a YouTube Music playlist into
Navidrome, plus how it works internally and how to maintain it. For a short
overview see the [README](../README.en.md).

---

## 0. What & why

`ytm-sync` takes a **public YouTube Music playlist**, downloads its tracks into your
Navidrome music folder and keeps it in sync: add a track to the playlist and a few
hours later it shows up in Navidrome (and any client like Amperfy/DSub).

You get:
- original audio (`opus`/`m4a`, no transcode) with cover art and tags;
- one tidy album instead of hundreds of single-track albums;
- a Navidrome playlist in the same order as YouTube Music;
- automation: scheduled checks, run-on-start, yt-dlp self-update.

---

## 1. Prerequisites

| Component | Why | Check |
|---|---|---|
| Docker + Docker Compose | runs the service | `docker --version && docker compose version` |
| Navidrome | music server | already deployed, pointing at a music folder |
| A **writable** music folder | downloads land here | same one Navidrome serves, mounted writable |
| YouTube Data API v3 key | full playlist listing | free, see step 3 |

> Navidrome usually mounts the music folder read-only (`:ro`) — fine. `ytm-sync`
> writes to the same folder mounted writable separately. Navidrome then scans it.

---

## 2. Get the project

```bash
git clone https://github.com/Laur1dor/ytm-nd.git ytm-sync
cd ytm-sync
```

```
ytm-sync/
├─ compose.yaml        # docker service definition
├─ Dockerfile          # image: python + ffmpeg + yt-dlp[default] + deno + rclone
├─ entrypoint.sh       # loop: update yt-dlp, wait for share, sync, sleep
├─ sync.py             # all the sync logic
├─ scripts/backfill.py # recover missing tracks
├─ .env.example        # config template
└─ docs/               # this documentation
```

---

## 3. YouTube Data API v3 key (≈3 min, free)

Why a key and not cookies: anonymously YouTube caps big playlists at ~100 tracks,
and account cookies **expire within hours**. An API key never rotates and returns
the whole list.

1. Open <https://console.cloud.google.com/> and create a project (top bar →
   *New Project*), or use an existing one.
2. Enable the API: **APIs & Services → Library**, find **"YouTube Data API v3"**,
   click **Enable**. Direct link:
   <https://console.cloud.google.com/apis/library/youtube.googleapis.com>.
   > If the "API restrictions" dropdown is empty when creating the key, you skipped
   > this — Enable first, then create the key.
3. Create the key: **APIs & Services → Credentials → Create credentials → API key**.
4. Copy the `AIza...` key. Optionally restrict it to this API.

Quota: listing a 650-track playlist ≈ 13 units against a 10,000/day limit.

---

## 4. Configure `.env`

```bash
cp .env.example .env
nano .env
```

At minimum set `PLAYLIST_URL` and `YT_API_KEY`. All variables:

| Variable | Default | Meaning |
|---|---|---|
| `PLAYLIST_URL` | — | Public YTM playlist URL. |
| `YT_API_KEY` | — | Key from step 3. |
| `INTERVAL_SECONDS` | `14400` | Check period (s). 14400 = 4h. |
| `MUSIC_SUBDIR` | `YTM` | Subfolder for downloads. |
| `ALBUM_NAME` | `YTM` | Wrapper album name for all tracks. |
| `ALBUM_ARTIST` | `YouTube Music` | Album-artist for all tracks. |
| `MUSIC_HOST_PATH` | `/mnt/Music` | Host music path, mounted to `/music`. |
| `REQUIRE_SHARE` | `true` | Guard: only write to a live network share. |
| `RCLONE_REMOTE` | — | Optional S3/remote mirror. |
| `TZ` | `Europe/Moscow` | Timezone. |

Get `PLAYLIST_URL`: in YouTube Music open the playlist → Share → Copy link. The
playlist must be public (or unlisted/link).

---

## 5. Choose a storage mode

**A — network share (default, recommended for NAS).** Music on a CIFS/NFS share
mounted on the host (e.g. `/mnt/Music`). Keep `REQUIRE_SHARE=true`. Before each
write the service verifies `/music` is a live network FS with a `.ytm_share_ok`
marker; if the share dropped, the run **aborts and nothing is written locally**.
Create the marker once:

```bash
touch /mnt/Music/.ytm_share_ok
```

**B — local disk.** Set `MUSIC_HOST_PATH=/path/to/local/folder` and
`REQUIRE_SHARE=false`.

**C — S3/remote mirror (on top of A or B).** Put an rclone config in
`./data/rclone.conf` and set `RCLONE_REMOTE=s3:bucket/music`. New files are
`rclone copy`-ed there after each sync. Serving that bucket to Navidrome is
separate (e.g. `rclone mount`).

---

## 6. Build & run

```bash
docker compose up -d --build
docker compose logs -f
```

The first run downloads the whole playlist (≈1.5–2h for 600+ tracks, in the
background). After that only new tracks are fetched every `INTERVAL_SECONDS`.

```bash
docker compose logs -f ytm-sync   # watch logs
docker compose restart            # force a run now (sync runs on start)
docker compose up -d              # apply .env changes
docker compose down               # stop
```

---

## 7. Verify in Navidrome

Navidrome scans on its own schedule (`ND_SCANSCHEDULE`). To see it now, restart the
Navidrome container (`docker restart navidrome`) — it scans on start.

You'll get:
- an **album** named `ALBUM_NAME` ("YTM"), all tracks in one album, ordered by
  playlist position (newest first);
- a **playlist** named after the subfolder ("YTM"), auto-imported from the
  `<MUSIC_SUBDIR>.m3u` file in playlist order.

---

## 8. How it works internally

Each run (`sync.py`):

1. **List.** Ordered video IDs via YouTube Data API (fallback: `yt-dlp
   --flat-playlist` + cookies). Order = playlist order.
2. **Download.** `yt-dlp` with the `android_vr` client, **no cookies**, downloads
   new tracks. `--download-archive` prevents re-downloads. Known-dead IDs
   (`data/unavailable.txt`) are skipped.
3. **Reconcile.** For each file on disk the embedded `purl` tag (video link) is
   read → builds a `videoId → file` map. The playlist is rebuilt from what's
   actually on disk (self-healing).
4. **Tags.** Every file gets the single album and a **track number = playlist
   position** (so the album also opens in playlist order). Only changed files are
   written.
5. **Mirror.** If `RCLONE_REMOTE` is set — `rclone copy` to the remote.
6. **Playlist.** Rewrites `<MUSIC_SUBDIR>.m3u` in playlist order; files in the
   folder that aren't in the playlist are appended at the end.

### Why these choices

| Choice | Reason |
|---|---|
| List via API, not cookies | anonymous caps at ~100; cookies expire within hours |
| Download via `android_vr`, no cookies | direct audio URLs, no JS "n-signature"; cookies break web/tv clients ("Only images available") |
| Age-restricted via `yt-dlp[default]` + `deno` + cookies | the `yt-dlp-ejs` package solves the JS challenge |
| Single album + track numbers | otherwise hundreds of singles; numbers give playlist order in the album |
| Network-share guard | never write to local disk if the share dropped |

---

## 9. Recover missing tracks — `scripts/backfill.py`

Some entries fail on a normal run: **age-restricted**, **deleted**, **region-
blocked**, **Premium-only**. For each missing entry the backfill tool:

1. tries the **original** (cookies + EJS solver — fixes age-restricted);
2. otherwise **searches YouTube** for the same title (`ytsearch`) and downloads a
   replacement, recording it in `data/replacements.tsv`;
3. otherwise marks it in `data/unavailable.txt` so future runs skip it.

```bash
# all missing
docker run --rm \
  -v "$PWD/data:/config" -v /mnt/Music:/music \
  -v "$PWD/.env:/env:ro" -v "$PWD/scripts/backfill.py:/backfill.py:ro" \
  --entrypoint python3 ytm-sync:latest /backfill.py

# specific IDs only (force retry)
... /backfill.py VIDEOID1 VIDEOID2
```

Age-restricted needs a valid `data/cookies.txt`.

---

## 10. Cookies (when & why)

Cookies are needed **only** for (a) no API key (fallback listing) or (b) backfilling
age-restricted tracks. Normal operation doesn't need them.

Export (Firefox, signed in to YouTube, is most reliable):

```bash
yt-dlp --cookies-from-browser firefox --cookies cookies.txt "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
cp cookies.txt ./data/cookies.txt
```

> YouTube cookies rotate and expire fast, especially if the account is actively
> used in a browser. That's why listing uses an API key and cookies are only a
> one-off for backfill.

---

## 11. Maintenance

- **yt-dlp** self-updates on container start and once a day. If YouTube breaks
  something — `docker compose restart`.
- **Change playlist/interval/folder** — edit `.env`, then `docker compose up -d`.
- `sync.py`/`entrypoint.sh` are mounted as volumes — apply edits with a restart,
  no rebuild. `Dockerfile` changes need `docker compose build`.

---

## 12. Troubleshooting (FAQ)

| Symptom | Cause / fix |
|---|---|
| Only ~100 tracks in the playlist | `YT_API_KEY` missing/invalid |
| `Only images available` / `Requested format is not available` | nsig challenge; rebuild image — needs `yt-dlp[default]` and `deno` |
| Run aborts immediately, nothing downloads | `REQUIRE_SHARE=true` but share not mounted; check host mount and `.ytm_share_ok` |
| Age-restricted track won't download | need fresh `data/cookies.txt`, then `backfill.py <id>` |
| Album sorted alphabetically, not by playlist | old files lack track numbers; wait for a run or `docker compose restart` |
| Two identical playlists in Navidrome | duplicate from frequent rescans; delete the extra in DB/UI |
| New tracks don't appear | check logs that the full list arrives and downloading happens |

---

## 13. State files (`./data` → `/config`)

| File | Purpose |
|---|---|
| `archive.txt` | yt-dlp download archive |
| `idmap.tsv` | `videoId → relative file path` (built from disk) |
| `replacements.tsv` | `origId → replacement file path` (for `.m3u`) |
| `unavailable.txt` | IDs to stop retrying |
| `cookies.txt` | optional; fallback listing + age-restricted |
| `rclone.conf` | optional; used when `RCLONE_REMOTE` is set |

---

## 14. Bonus tools (`scripts/`)

- **`import_radio.py`** — bulk-imports a big set of internet radio stations from
  radio-browser.info into Navidrome, grouped by category (the category is embedded
  in the name, "🎧 Electronic · …", so they group when sorted — Navidrome has no
  folders for radio). HTTPS-only (http is blocked by mixed-content in an HTTPS web
  UI and on iOS; http→https is actually verified). Run with Navidrome stopped and a
  DB backup (see the file header). Limits/genres are editable at the top of the file.

- **`nas-share-watch.sh`** — for setups where music lives on a network share that
  comes up late after a reboot. A systemd service on the Docker host that waits for
  the share (via a marker file) on boot and restarts the containers that need it
  (e.g. `navidrome`), so you don't have to do it by hand.
