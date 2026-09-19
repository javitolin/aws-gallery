#!/usr/bin/env python3
"""Publish media to the gallery: prepare anything unplayable, then upload.

Keys are content-addressed — media/<sha256[:16]><ext> — because 677 of the
source files share a name with a different file, so a flat layout keyed on
filename would silently overwrite them. Hashing also means identical copies
upload once.

The folder a file sits in becomes its category, carried in a tag and in the
metadata sidecar rather than in the key, so items can be recategorised later
without moving a byte.

Safe to interrupt and safe to re-run: hashes are cached, the checkpoint is
written after each file, and S3 is consulted as the source of truth. Nothing is
ever deleted.
"""
import argparse
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

BUCKET = os.environ["GALLERY_BUCKET"]
# Drop new files here; the web-ready tree is generated from it.
RAW_SOURCE = Path(os.environ["GALLERY_SOURCE"])
SOURCE = Path(os.environ["GALLERY_WEB"])
CHECKPOINT = SOURCE / ".upload-checkpoint.json"

MEDIA_PREFIX = "media/"
ARCHIVE_PREFIX = "archive/"
META_PREFIX = "meta/"

# .sfk/.thm/.sec are camera and editor sidecars that regenerate or carry nothing.
SKIP_EXT = {".sfk", ".ds_store", ".mp3", ".thm", ".sec"}
NEEDS_TRANSCODE = {".mpg", ".mpeg", ".avi", ".wmv"}
# What the frontend can render. Anything else is kept, but cheaply and out of
# the timeline.
RENDERABLE_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
    ".mp4", ".mov", ".m4v", ".webm", ".m4a", ".wav", ".ogg",
}

CONTENT_TYPES = {".mov": "video/quicktime", ".m4v": "video/x-m4v", ".heic": "image/heic"}

SETTLE_SECONDS = 120
HASH_CHUNK = 8 << 20


def sha16(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def classify(rel: str, path: Path) -> tuple[str | None, str | None]:
    ext = path.suffix.lower()
    # Dotfiles are bookkeeping (the checkpoint, .DS_Store), never media.
    if ext in SKIP_EXT or path.name.startswith("."):
        return None, "regenerable sidecar"
    if ext in NEEDS_TRANSCODE:
        return None, "needs transcoding first — run scripts/transcode.py"
    return (MEDIA_PREFIX if ext in RENDERABLE_EXT else ARCHIVE_PREFIX), None


def category_of(rel: str) -> str | None:
    """Folder path under the source root, nesting preserved."""
    parent = str(Path(rel).parent)
    return None if parent == "." else parent


def load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        return json.loads(CHECKPOINT.read_text())
    return {"uploaded": {}, "hashes": {}, "started": None}


def save_checkpoint(state: dict) -> None:
    state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = CHECKPOINT.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(CHECKPOINT)


def cached_hash(state: dict, rel: str, path: Path) -> str:
    """Re-hashing 95GB on every run would make this unusable."""
    stat = path.stat()
    fingerprint = f"{stat.st_size}:{int(stat.st_mtime)}"
    entry = state["hashes"].get(rel)
    if entry and entry.get("fingerprint") == fingerprint:
        return entry["sha"]
    sha = sha16(path)
    state["hashes"][rel] = {"fingerprint": fingerprint, "sha": sha}
    return sha


def already_in_s3(s3: object, key: str, size: int) -> bool:
    try:
        return s3.head_object(Bucket=BUCKET, Key=key)["ContentLength"] == size
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def prepare(raw: Path, web: Path, apply: bool) -> bool:
    cmd = [sys.executable, str(Path(__file__).parent / "transcode.py"),
           "--source", str(raw), "--output", str(web)]
    if apply:
        cmd.append("--apply")
    print("preparing media…")
    return subprocess.run(cmd).returncode == 0


def write_sidecar(s3, key: str, rel: str, path: Path, category: str | None) -> None:
    """Written before the object, so the S3-triggered processor merges onto it
    rather than racing it.

    Re-run on every publish so reorganising the source folders re-categorises
    what is already uploaded — except where the category was changed from the
    gallery, which wins over the folder layout.
    """
    meta_key = f"{META_PREFIX}{key[len(MEDIA_PREFIX):]}.json"
    record = {}
    try:
        record = json.loads(s3.get_object(Bucket=BUCKET, Key=meta_key)["Body"].read())
    except Exception:
        pass

    manual = record.get("category_by")
    record.update({
        "key": key,
        "name": path.name,
        "source_path": rel,
        "size": path.stat().st_size,
    })
    if not manual:
        record["category"] = category
    s3.put_object(
        Bucket=BUCKET,
        Key=meta_key,
        Body=json.dumps(record, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually upload")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--raw", type=Path, default=RAW_SOURCE)
    parser.add_argument("--no-prepare", action="store_true")
    args = parser.parse_args()

    if not args.no_prepare:
        if not prepare(args.raw, args.source, args.apply):
            print("!! preparation failed; nothing uploaded", file=sys.stderr)
            return 1
        print()

    s3 = boto3.client("s3")
    state = load_checkpoint()
    state.setdefault("started", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    done = state["uploaded"]

    plan, skipped, pending, empty = [], [], [], []
    seen_keys = {}
    duplicates = 0
    now = time.time()

    print("hashing…", end="", flush=True)
    for path in sorted(p for p in args.source.rglob("*") if p.is_file()):
        rel = str(path.relative_to(args.source))
        prefix, reason = classify(rel, path)
        if prefix is None:
            skipped.append((rel, reason))
            continue

        size = path.stat().st_size
        if size == 0:
            empty.append(rel)
            continue
        if now - path.stat().st_mtime < SETTLE_SECONDS:
            pending.append(rel)
            continue

        sha = cached_hash(state, rel, path)
        key = f"{prefix}{sha}{path.suffix.lower()}"
        if key in seen_keys:
            # Identical content under two names: one object serves both.
            duplicates += 1
            continue
        seen_keys[key] = rel

        record = done.get(rel)
        if record and record.get("key") == key:
            continue
        plan.append((rel, key, size, prefix, category_of(rel)))
    print(" done\n")
    save_checkpoint(state)

    gallery = [p for p in plan if p[3] == MEDIA_PREFIX]
    archive = [p for p in plan if p[3] == ARCHIVE_PREFIX]
    categories = sorted({p[4] for p in plan if p[4]})

    print(f"already uploaded : {len(done)}")
    print(f"to gallery       : {len(gallery)}  ({sum(p[2] for p in gallery)/2**30:.1f} GiB)")
    print(f"to archive       : {len(archive)}  ({sum(p[2] for p in archive)/2**30:.1f} GiB)")
    print(f"deduplicated     : {duplicates} identical copies collapsed")
    print(f"not recovered yet: {len(empty)} zero-byte")
    print(f"still writing    : {len(pending)}")
    print(f"ignored          : {len(skipped)}")
    print(f"\ncategories ({len(categories)}):")
    for name in categories:
        count = sum(1 for p in plan if p[4] == name)
        print(f"  {name}  ({count})")

    if not args.apply:
        print("\ndry run — re-run with --apply")
        return 0

    failures = 0
    for i, (rel, key, size, prefix, category) in enumerate(plan, 1):
        path = args.source / rel
        if already_in_s3(s3, key, size):
            # Bytes are already there, but the folder may have moved since.
            if prefix == MEDIA_PREFIX:
                write_sidecar(s3, key, rel, path, category)
            done[rel] = {"key": key, "size": size, "category": category,
                         "uploaded_at": "pre-existing"}
            save_checkpoint(state)
            continue

        extra = {
            "ContentType": CONTENT_TYPES.get(path.suffix.lower())
            or mimetypes.guess_type(rel)[0] or "application/octet-stream",
            # Immutable key, so it can be cached indefinitely.
            "CacheControl": "public, max-age=31536000, immutable",
        }
        if prefix == ARCHIVE_PREFIX:
            extra["StorageClass"] = "GLACIER_IR"
        if category:
            # Tagging on PutObject is a URL-encoded query string, not a literal.
            # Passing a Hebrew or spaced category raw yields InvalidTag.
            extra["Tagging"] = urllib.parse.urlencode({"category": category})

        if prefix == MEDIA_PREFIX:
            write_sidecar(s3, key, rel, path, category)

        started = time.time()
        try:
            s3.upload_file(str(path), BUCKET, key, ExtraArgs=extra)
        except Exception as exc:
            failures += 1
            print(f"  [{i}/{len(plan)}] FAILED   {rel}: {exc}")
            continue

        if path.stat().st_size != size:
            print(f"  [{i}/{len(plan)}] GREW     {rel}; not checkpointed, rerun to fix")
            continue

        rate = size / max(time.time() - started, 0.001) / 2**20
        print(f"  [{i}/{len(plan)}] ok       {rel}  ({size/2**20:.0f} MiB, {rate:.1f} MiB/s)")
        done[rel] = {
            "key": key,
            "size": size,
            "category": category,
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        save_checkpoint(state)

    save_checkpoint(state)
    print(f"\n{len(plan)-failures} uploaded, {failures} failed, {len(done)} total in checkpoint")

    if plan:
        # Per-object rebuilds race and the last one may not be the last upload.
        print("rebuilding manifest…")
        boto3.client("lambda").invoke(
            FunctionName="gallery-media-processor",
            InvocationType="Event",
            Payload=json.dumps({"Records": []}).encode(),
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
