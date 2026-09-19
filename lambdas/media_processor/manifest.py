import os
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from datetime import datetime, timezone
from typing import Iterator

import boto3

from media_kinds import kind_for
from models import ArchiveEntry, Manifest, MediaRecord

s3 = boto3.client("s3")

# Formats a browser can play directly; everything else in archive/ is download-only.
BROWSER_PLAYABLE = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".mp4", ".webm", ".mov"}


def _paginate(bucket: str, prefix: str) -> Iterator[dict]:
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        yield from page.get("Contents", [])


def _by_recency(record: MediaRecord) -> str:
    return record.sort_key()


def _by_name(entry: MediaRecord | ArchiveEntry) -> str:
    return entry.name or ""


# Sidecars are fetched one GetObject each. Serially that is a minute or more at
# a few thousand items, which timed out the write API; the work is pure network
# wait, so it parallelises almost linearly.
FETCH_WORKERS = 32


def _read_record(bucket: str, key: str) -> MediaRecord | None:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return MediaRecord.model_validate_json(body)
    except Exception:
        # A single unreadable sidecar must not lose the whole manifest.
        return None


def _records(bucket: str, meta_prefix: str) -> tuple[list[MediaRecord], list[MediaRecord]]:
    """Split sidecars into the timeline and the archive section."""
    keys = [
        obj["Key"] for obj in _paginate(bucket, meta_prefix) if obj["Key"].endswith(".json")
    ]
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        records = pool.map(partial(_read_record, bucket), keys)

    live: list[MediaRecord] = []
    archived: list[MediaRecord] = []
    for record in records:
        if record is None:
            continue
        (archived if record.archived else live).append(record)

    live.sort(key=_by_recency, reverse=True)
    archived.sort(key=_by_name)
    return live, archived


def _archive_objects(bucket: str, archive_prefix: str) -> list[ArchiveEntry]:
    entries = [
        ArchiveEntry(
            key=obj["Key"],
            name=os.path.basename(obj["Key"]),
            size=obj["Size"],
            storage_class=obj.get("StorageClass", "STANDARD"),
            kind=kind_for(obj["Key"]),
            modified_at=obj["LastModified"].isoformat(),
            playable=os.path.splitext(obj["Key"])[1].lower() in BROWSER_PLAYABLE,
        )
        for obj in _paginate(bucket, archive_prefix)
        if not obj["Key"].endswith("/")
    ]
    entries.sort(key=_by_name)
    return entries


def _as_archive_entry(record: MediaRecord) -> ArchiveEntry:
    """Archived items keep their media/ key and stay viewable: Glacier IR
    answers a normal GET. Only the storage class and the placement change."""
    return ArchiveEntry(
        key=record.key,
        name=record.name,
        size=record.size,
        storage_class="archived",
        kind=record.kind,
        category=record.category,
        thumb=record.thumb,
        taken_at=record.taken_at,
        modified_at=record.modified_at,
        duration=record.duration,
        playable=record.renderable,
    )


def rebuild(bucket: str, meta_prefix: str, archive_prefix: str, manifest_key: str) -> Manifest:
    items, archived = _records(bucket, meta_prefix)
    manifest = Manifest(
        generated_at=datetime.now(timezone.utc).isoformat(),
        items=items,
        archive=_archive_objects(bucket, archive_prefix) + [
            _as_archive_entry(record) for record in archived
        ],
    )
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=manifest.to_json(),
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache",
    )
    return manifest
