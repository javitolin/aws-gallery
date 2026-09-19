#!/usr/bin/env python3
"""Prepare recovered media for the web, into a parallel tree.

Three cases:
  MPEG-2 and friends   full transcode to H.264/AAC (hardware encoder)
  moov at end of file  lossless container rewrite so playback starts instantly
  everything else      hardlink, so no disk is wasted

Output mirrors the source layout, because the first folder becomes the gallery
category. Sources are never modified.
"""
import argparse
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

SOURCE = Path(os.environ["GALLERY_SOURCE"])
OUTPUT = Path(os.environ["GALLERY_WEB"])

TRANSCODE_EXT = {".mpg", ".mpeg", ".avi", ".wmv"}
REMUXABLE_EXT = {".mp4", ".mov", ".m4v"}
SKIP_EXT = {".sfk", ".ds_store", ".mp3", ".thm", ".sec"}
# Codecs every current browser can decode. HEVC plays in Safari but not
# reliably in Chrome on Windows or Linux, so it is re-encoded.
WEB_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}

VIDEO_BITRATE = "2500k"
AUDIO_BITRATE = "160k"


def first_atom(path: Path) -> str:
    """Return whichever of moov/mdat appears first; moov first means faststart."""
    try:
        with path.open("rb") as fh:
            offset = 0
            while True:
                header = fh.read(8)
                if len(header) < 8:
                    return "?"
                size = struct.unpack(">I", header[:4])[0]
                kind = header[4:8].decode("latin1")
                if kind in ("moov", "mdat"):
                    return kind
                if size == 1:
                    size = struct.unpack(">Q", fh.read(8))[0]
                elif size == 0:
                    return "?"
                offset += size
                fh.seek(offset)
    except Exception:
        return "?"


def video_codec(path: Path) -> str:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, timeout=60)
    return result.stdout.decode().strip().rstrip(",")


def run(cmd: list[str]) -> bool:
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr.decode()[-500:] + "\n")
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--keep-stale", action="store_true",
                        help="leave outputs whose source is gone")
    args = parser.parse_args()

    plans = []
    for path in sorted(p for p in args.source.rglob("*") if p.is_file()):
        ext = path.suffix.lower()
        if ext in SKIP_EXT or path.name == ".DS_Store" or path.stat().st_size == 0:
            continue
        rel = path.relative_to(args.source)

        if ext in TRANSCODE_EXT:
            plans.append(("transcode", path, args.output / rel.with_suffix(".mp4")))
        elif ext in REMUXABLE_EXT:
            codec = video_codec(path)
            if codec and codec not in WEB_VIDEO_CODECS:
                plans.append(("transcode", path, args.output / rel.with_suffix(".mp4")))
            elif first_atom(path) != "moov":
                plans.append(("remux", path, args.output / rel))
            else:
                plans.append(("link", path, args.output / rel))
        else:
            plans.append(("link", path, args.output / rel))

    # The output tree is entirely generated. Anything in it without a source is
    # left over from a rename and would publish under a category that no longer
    # exists, so it goes. Sources and S3 are never touched.
    expected = {dest for _, _, dest in plans}
    stale = [
        p for p in args.output.rglob("*")
        if p.is_file() and p not in expected and p.name != ".upload-checkpoint.json"
    ] if args.output.exists() and not args.keep_stale else []

    for action in ("transcode", "remux", "link"):
        group = [p for p in plans if p[0] == action]
        size = sum(p[1].stat().st_size for p in group) / 2**30
        print(f"  {action:10s} {len(group):3d} files  {size:6.2f} GiB")

    if stale:
        size = sum(p.stat().st_size for p in stale) / 2**30
        print(f"  {'stale':10s} {len(stale):3d} files  {size:6.2f} GiB  (generated, source gone)")

    if not args.apply:
        print("\ndry run — re-run with --apply")
        return 0

    for p in stale:
        p.unlink()
    for directory in sorted((d for d in args.output.rglob("*") if d.is_dir()),
                            key=lambda d: -len(d.parts)):
        if not any(directory.iterdir()):
            directory.rmdir()
    if stale:
        print(f"removed {len(stale)} stale generated files\n")

    failures = 0
    todo = [p for p in plans if not (p[2].exists() and p[2].stat().st_size > 0)]
    print(f"\n{len(plans) - len(todo)} already done, {len(todo)} to process\n")

    for i, (action, src, dest) in enumerate(todo, 1):
        dest.parent.mkdir(parents=True, exist_ok=True)
        started = time.time()

        if action == "link":
            try:
                os.link(src, dest)
            except OSError:
                dest.write_bytes(src.read_bytes())
            print(f"  [{i}/{len(todo)}] link      {dest.name}")
            continue

        if action == "remux":
            # Container rewrite only; the streams are copied bit for bit.
            ok = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                      "-c", "copy", "-movflags", "+faststart", str(dest)])
        else:
            ok = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                      "-c:v", "h264_videotoolbox", "-b:v", VIDEO_BITRATE,
                      # Only touches frames actually flagged interlaced.
                      "-vf", "yadif=deint=interlaced",
                      "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                      "-movflags", "+faststart", str(dest)])

        if not ok:
            failures += 1
            dest.unlink(missing_ok=True)
            print(f"  [{i}/{len(todo)}] FAILED    {src.name}")
            continue

        elapsed = time.time() - started
        before = src.stat().st_size / 2**20
        after = dest.stat().st_size / 2**20
        print(f"  [{i}/{len(todo)}] {action:9s} {dest.name}  "
              f"{before:.0f} -> {after:.0f} MiB in {elapsed:.0f}s")

    print(f"\n{len(todo) - failures} done, {failures} failed")
    print(f"output: {args.output}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
