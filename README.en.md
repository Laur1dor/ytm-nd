# ytm-sync

[🇷🇺 Русский](README.md) · 🇬🇧 English

Automatically mirror a **public YouTube Music playlist** into a **Navidrome** music
library. New tracks you add to the playlist are downloaded on a schedule (original
`opus`/`m4a`, with cover art and tags), grouped into a single album so they don't
flood your library, and exposed to Navidrome both as files and as an ordered `.m3u`
playlist.

Runs as a single Docker Compose service next to your Navidrome container.

---

## Why it works the way it does

Getting a large YouTube Music playlist reliably is trickier than it looks. This
project uses a deliberate split that survived a lot of trial and error:

| Concern | Approach | Reason |
|---|---|---|
| **Get the full track list** | **YouTube Data API v3** (API key) | Anonymous access caps big playlists at ~100 items. Cookies work but **expire within hours** when the account is used elsewhere. An API key never rotates. |
| **Download audio** | `yt-dlp` with the `android_vr` client, **no cookies** | This client returns direct audio URLs without the JS "n-signature" challenge. With cookies, web/TV clients fail with *"Only images available"*. |
| **Age‑restricted tracks** | `yt-dlp[default]` (bundles `yt-dlp-ejs`) + `deno` + cookies | Solves the JS challenge so age‑gated videos resolve. |
| **Library structure** | All tracks tagged `ALBUM=YTM`, `ALBUMARTIST=…` | Otherwise every single becomes its own album and floods Navidrome's *Albums* view. |
| **Playlist order** | Generated `.m3u` rebuilt every run | Navidrome auto‑imports it as a playlist in the exact playlist order. |
| **Don't trash local disk** | Optional network‑share guard | If the destination is a network share that dropped, the run aborts instead of silently writing to the local disk. |

## Features

- ⏱️ Periodic sync (default every 4h) + immediate run on container start.
- 🎵 Original audio (no transcode), embedded metadata + cover art.
- 📃 Ordered `.m3u` playlist, auto‑imported by Navidrome.
- 🗂️ Single‑album grouping so the library stays clean.
- ➕ Add‑only: removing a track from the playlist never deletes the local file.
- ♻️ Self‑healing: the playlist is rebuilt from the files actually on disk
  (track IDs are read back from the embedded `purl` tag).
- 🛟 Backfill tool: recovers age‑restricted tracks and finds replacements for
  deleted/blocked ones.
- 💾 Switchable storage: network share / local disk, with optional `rclone`
  mirroring to S3 or any remote.
- 🔁 Auto‑recovery: if the network share isn't mounted yet (e.g. after a reboot),
  the container waits and picks it up without a restart.

---

## Requirements

- Docker + Docker Compose.
- A Navidrome instance pointing at a music folder you can also write to.
- A **YouTube Data API v3** key (free) — see below.

## Quick start

```bash
git clone <this-repo> ytm-sync
cd ytm-sync
cp .env.example .env
# edit .env — set PLAYLIST_URL and YT_API_KEY
docker compose up -d --build
docker compose logs -f
```

### Getting a YouTube Data API key (≈3 minutes, free)

1. Open <https://console.cloud.google.com/> and create (or pick) a project.
2. **APIs & Services → Library** → enable **“YouTube Data API v3”**
   (direct link: <https://console.cloud.google.com/apis/library/youtube.googleapis.com>).
3. **APIs & Services → Credentials → Create credentials → API key**.
4. Copy the key (`AIza…`) into `YT_API_KEY` in your `.env`.

Quota is tiny: listing a 650‑track playlist costs ~13 units against a 10,000/day limit.

> No API key? It falls back to `yt-dlp` + an optional `data/cookies.txt`. Without
> cookies, large playlists are truncated to ~100 tracks — so the API key is recommended.

---

## Configuration

All configuration is via `.env` (see [`.env.example`](.env.example)):

| Variable | Default | Description |
|---|---|---|
| `PLAYLIST_URL` | — | Public YouTube Music playlist URL (required). |
| `YT_API_KEY` | — | YouTube Data API v3 key (recommended). |
| `INTERVAL_SECONDS` | `14400` | How often to check the playlist (4h). |
| `MUSIC_SUBDIR` | `YTM` | Subfolder inside the music root for downloads. |
| `ALBUM_NAME` | `YTM` | Album tag applied to every track (grouping). |
| `ALBUM_ARTIST` | `YouTube Music` | Album‑artist tag applied to every track. |
| `MUSIC_HOST_PATH` | `/mnt/Music` | Host path mounted into the container at `/music`. |
| `REQUIRE_SHARE` | `true` | Guard: only write if `/music` is a live network share. Set `false` for local disk. |
| `RCLONE_REMOTE` | — | Optional rclone remote to mirror into, e.g. `s3:bucket/music`. |
| `TZ` | `Europe/Moscow` | Timezone. |

### Storage modes

- **Network share (default):** mount your CIFS/NFS share at `MUSIC_HOST_PATH`,
  keep `REQUIRE_SHARE=true`. The run aborts (without writing locally) if the share
  is gone, and the container auto‑waits for it to come back.
- **Local disk:** set `MUSIC_HOST_PATH` to a local folder and `REQUIRE_SHARE=false`.
- **S3 / remote (optional mirror):** put an `rclone.conf` in `./data/` and set
  `RCLONE_REMOTE=s3:your-bucket/music`. After each sync, new files are `rclone copy`‑ed
  there. (Serving that bucket to Navidrome is a separate concern, e.g. an `rclone mount`.)

---

## How a sync run works

1. Fetch the ordered list of video IDs (YouTube Data API, or yt‑dlp fallback).
2. Download anything new with `yt-dlp` (`android_vr`, archive prevents re‑downloads),
   skipping IDs known to be permanently unavailable.
3. Apply the single‑album tags to any untagged files.
4. Rebuild the `videoId → file` map from disk (read from embedded `purl` tags).
5. Optionally mirror to a remote via `rclone`.
6. Rewrite the `.m3u` in current playlist order (using replacements where needed).

## Recovering missing tracks — `scripts/backfill.py`

Some playlist entries fail on a normal run: age‑restricted, deleted, region‑blocked,
or Premium‑only. The backfill tool tries, for each missing entry:

1. download the **original** (cookies + EJS solver — fixes age‑restricted);
2. otherwise **search YouTube** for the same title and download a replacement,
   recording it in `data/replacements.tsv` so the `.m3u` still points at it;
3. otherwise mark it in `data/unavailable.txt` so future runs skip it.

```bash
# all missing tracks
docker run --rm \
  -v "$PWD/data:/config" -v /mnt/Music:/music \
  -v "$PWD/.env:/env:ro" -v "$PWD/scripts/backfill.py:/backfill.py:ro" \
  --entrypoint python3 ytm-sync:latest /backfill.py

# only specific video IDs (force retry)
... /backfill.py VIDEOID1 VIDEOID2
```

Age‑restricted downloads need a valid `data/cookies.txt` (Netscape format). Export it
from a browser where you're signed in — Firefox is the most reliable source
(`yt-dlp --cookies-from-browser firefox --cookies cookies.txt <any-url>`).

---

## State files (`./data`, mounted at `/config`)

| File | Purpose |
|---|---|
| `archive.txt` | yt‑dlp download archive (already‑downloaded video IDs). |
| `idmap.tsv` | `videoId → relative file path`, rebuilt from disk. |
| `replacements.tsv` | `originalId → replacement file path` (for the `.m3u`). |
| `unavailable.txt` | IDs to stop retrying (deleted/blocked/private). |
| `cookies.txt` | Optional. Fallback for listing + age‑restricted downloads. |
| `rclone.conf` | Optional. Used when `RCLONE_REMOTE` is set. |

## Troubleshooting

- **Playlist truncated to ~100 tracks** → set a `YT_API_KEY`, or refresh `cookies.txt`.
- **`Only images available` / `Requested format is not available`** → you're hitting
  the nsig challenge; ensure the image was built with `yt-dlp[default]` and `deno`.
- **Nothing downloads, run aborts** → with `REQUIRE_SHARE=true`, the share isn't
  mounted; check the host mount (and `data/.ytm_share_ok` marker on the share).
- **yt‑dlp suddenly breaks** → it auto‑updates on container start; `docker compose restart`.

## License

MIT.
