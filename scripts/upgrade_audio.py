#!/usr/bin/env python3
"""One-shot upgrade of mapped AAC fallbacks. Run with the sync worker stopped."""
import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus

FORMAT = 'bestaudio[abr>=110]/bestaudio[format_id="251"][abr>=100]/140'


def save(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def audio(path):
    return OggOpus(path) if path.suffix == ".opus" else MP4(path)


def improves(old_bitrate, new_bitrate, source_format, extension, old_extension=".m4a"):
    # Known full-quality Opus is an actual different source, not an AAC upscale.
    if extension == ".opus":
        minimum = max(100000, old_bitrate * (1.15 if old_extension == ".opus" else .95))
        return source_format in {"251", "774"} and new_bitrate >= minimum
    return new_bitrate >= max(110000, old_bitrate * 1.15)


def preserve_album(path, original):
    new = audio(path)
    if isinstance(new, OggOpus):
        if isinstance(original, OggOpus):
            for key in ["title", "artist", "album", "albumartist", "tracknumber", "tracktotal", "discnumber"]:
                if original.get(key):
                    new[key] = original[key]
        else:
            for source, target in [("\xa9nam", "title"), ("\xa9ART", "artist"),
                                   ("\xa9alb", "album"), ("aART", "albumartist")]:
                if original.get(source):
                    new[target] = original[source]
            number, total = original.get("trkn", [(0, 0)])[0]
            new["tracknumber"] = [str(number)]
            new["tracktotal"] = [str(total)]
            new["discnumber"] = [str(original.get("disk", [(1, 1)])[0][0])]
        for key in ["date", "year", "originaldate", "originalyear", "releasedate"]:
            if key in new:
                new["ytm_source_" + key] = new[key]
                del new[key]
    else:
        if isinstance(original, OggOpus):
            for source, target in [("title", "\xa9nam"), ("artist", "\xa9ART"),
                                   ("album", "\xa9alb"), ("albumartist", "aART")]:
                if original.get(source):
                    new[target] = original[source]
            number = int(original.get("tracknumber", ["0"])[0].split("/")[0])
            total = int(original.get("tracktotal", ["0"])[0])
            new["trkn"] = [(number, total)]
            new["disk"] = [(int(original.get("discnumber", ["1"])[0].split("/")[0]), 1)]
        else:
            for key in ["\xa9nam", "\xa9ART", "\xa9alb", "aART", "trkn", "disk"]:
                if original.get(key):
                    new[key] = original[key]
        if "\xa9day" in new:
            new["----:com.apple.iTunes:YTM_SOURCE_DATE"] = [s.encode() for s in new["\xa9day"]]
            del new["\xa9day"]
    new.save()


def install(original, staged, root, config, video_id, backup):
    mapping_file = config / "idmap.tsv"
    old_mapping = mapping_file.read_text(encoding="utf-8")
    mapping = dict(line.split("\t", 1) for line in old_mapping.splitlines() if "\t" in line)
    old_rel = str(original.relative_to(root))
    if mapping.get(video_id) != old_rel:
        raise RuntimeError("The mapping changed; stop before replacing a file")
    target = original.with_suffix(staged.suffix)
    if target != original and target.exists():
        raise RuntimeError("Target path already exists; keep both files untouched")
    new_rel = str(target.relative_to(root))
    playlist = root / (os.environ.get("MUSIC_SUBDIR", "YTM") + ".m3u")
    old_playlist = playlist.read_text(encoding="utf-8")
    lines = old_playlist.splitlines()
    if lines.count(old_rel) != 1:
        raise RuntimeError("Expected exactly one playlist entry")
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(original, backup)
    temp = target.with_name(".ytm-quality-" + video_id + ".part")
    shutil.copyfile(staged, temp)
    try:
        temp.replace(target)
        mapping[video_id] = new_rel
        save(mapping_file, "".join(k + "\t" + v + "\n" for k, v in mapping.items()))
        save(playlist, "".join((new_rel if line == old_rel else line) + "\n" for line in lines))
        if target != original:
            original.unlink()
    except Exception:
        save(mapping_file, old_mapping)
        save(playlist, old_playlist)
        if target == original:
            shutil.copy2(backup, original)
        elif target.exists():
            target.unlink()
        raise
    return new_rel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="*", help="Upgrade only these mapped IDs")
    args = parser.parse_args()
    config = Path(os.environ.get("CONFIG_DIR", "/config"))
    root = Path(os.environ.get("MUSIC_ROOT", "/music"))
    work = config / "backups/audio-quality-20261031"
    work.mkdir(parents=True, exist_ok=True)
    work.chmod(0o700)
    manifest = work / "results.json"
    results = json.loads(manifest.read_text()) if manifest.exists() else {}
    for name in ["idmap.tsv", "archive.txt"]:
        if not (work / name).exists():
            shutil.copy2(config / name, work / name)
    playlist = root / (os.environ.get("MUSIC_SUBDIR", "YTM") + ".m3u")
    if not (work / "playlist.m3u").exists():
        shutil.copy2(playlist, work / "playlist.m3u")
    mapping = dict(line.split("\t", 1) for line in (config / "idmap.tsv").read_text(encoding="utf-8").splitlines() if "\t" in line)
    selected = [(v, root / p) for v, p in mapping.items()
                if (Path(p).suffix == ".m4a" or (args.ids is not None and Path(p).suffix == ".opus"))
                and (args.ids is None or v in args.ids)]
    print(f"Audio candidates: {len(selected)}", flush=True)
    for index, (vid, old) in enumerate(selected, 1):
        if results.get(vid, {}).get("status") == "upgraded":
            continue
        if not old.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("File outside music root")
        old_audio = audio(old)
        directory = work / vid
        directory.mkdir(exist_ok=True)
        command = ["yt-dlp", "--no-playlist", "--no-progress", "--retries", "2",
                   "--fragment-retries", "3", "--socket-timeout", "15", "--write-info-json",
                   "--extractor-args", "youtube:player_client=" + os.environ.get("YT_PLAYER_CLIENT", "tv_simply,mweb"),
                   "--extractor-args", "youtubepot-bgutilhttp:base_url=" + os.environ.get("POT_BASE_URL", "http://bgutil-provider:4416"),
                   "-f", FORMAT, "--extract-audio", "--embed-metadata", "--embed-thumbnail",
                   "-o", str(directory / (vid + ".%(ext)s")), "https://www.youtube.com/watch?v=" + vid]
        entry = {"old_path": str(old.relative_to(root)), "old_bitrate": old_audio.info.bitrate,
                 "checked_at": datetime.now(timezone.utc).isoformat()}
        try:
            with (directory / "download.log").open("w") as log:
                downloaded = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=240)
            if downloaded.returncode:
                raise RuntimeError("Source download failed; see private download.log")
            info = json.loads((directory / (vid + ".info.json")).read_text())
            if info["id"] != vid:
                raise RuntimeError("Source ID changed")
            staged = next(p for p in directory.iterdir() if p.suffix in {".opus", ".m4a"})
            new_audio = audio(staged)
            if abs(new_audio.info.length - old_audio.info.length) > max(3, old_audio.info.length * .02):
                raise RuntimeError("Duration mismatch")
            fmt = str(info.get("format_id"))
            entry.update(new_bitrate=new_audio.info.bitrate, source_format=fmt)
            if not improves(old_audio.info.bitrate, new_audio.info.bitrate, fmt, staged.suffix, old.suffix):
                entry["status"] = "no_better_source"
            else:
                preserve_album(staged, old_audio)
                new_rel = install(old, staged, root, config, vid, work / "originals" / (vid + old.suffix))
                entry.update(status="upgraded", new_path=new_rel)
        except Exception as exc:
            entry.update(status="failed", error=str(exc))
        results[vid] = entry
        save(manifest, json.dumps(results, ensure_ascii=False, indent=2))
        print(f"{index}/{len(selected)} {vid}: {entry['status']} "
              f"{entry['old_bitrate']//1000} -> {entry.get('new_bitrate', 0)//1000} kbps", flush=True)
    return int(any(r["status"] == "failed" for r in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
