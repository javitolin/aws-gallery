import json
import os
from datetime import datetime, timezone

import boto3

from media_kinds import kind_for

s3 = boto3.client("s3")

# Formats a browser can play directly; everything else in archive/ is download-only.
BROWSER_PLAYABLE = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".mp4", ".webm", ".mov"}


def _paginate(bucket: str, prefix: str):
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        yield from page.get("Contents", [])


def _items(bucket: str, meta_prefix: str) -> tuple[list[dict], list[dict]]:
    """Split sidecars into the timeline and the archive section."""
    live, archived = [], []
    for obj in _paginate(bucket, meta_prefix):
        if not obj["Key"].endswith(".json"):
            continue
        body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
        record = json.loads(body)
        (archived if record.get("archived") else live).append(record)

    live.sort(key=lambda i: (i.get("taken_at") or i.get("modified_at") or ""), reverse=True)
    archived.sort(key=lambda i: i.get("name") or "")
    return live, archived


def _archive(bucket: str, archive_prefix: str) -> list[dict]:
    entries = []
    for obj in _paginate(bucket, archive_prefix):
        if obj["Key"].endswith("/"):
            continue
        ext = os.path.splitext(obj["Key"])[1].lower()
        entries.append(
            {
                "key": obj["Key"],
                "name": os.path.basename(obj["Key"]),
                "size": obj["Size"],
                "storage_class": obj.get("StorageClass", "STANDARD"),
                "kind": kind_for(obj["Key"]),
                "modified_at": obj["LastModified"].isoformat(),
                "playable": ext in BROWSER_PLAYABLE,
            }
        )
    entries.sort(key=lambda e: e["name"])
    return entries


def rebuild(bucket: str, meta_prefix: str, archive_prefix: str, manifest_key: str) -> dict:
    items, archived = _items(bucket, meta_prefix)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        # Objects under archive/, plus anything archived in place from the gallery.
        # Archived items keep their media/ key and stay viewable: Glacier IR
        # answers a normal GET. Only the storage class and the placement change.
        "archive": _archive(bucket, archive_prefix) + [
            {
                "key": a["key"],
                "name": a["name"],
                "size": a.get("size", 0),
                "storage_class": "archived",
                "kind": a.get("kind", "other"),
                "category": a.get("category"),
                "thumb": a.get("thumb"),
                "taken_at": a.get("taken_at"),
                "archived_by": a.get("archived_by"),
                "archived_at": a.get("archived_at"),
                "modified_at": a.get("modified_at"),
                "duration": a.get("duration"),
                "playable": a.get("renderable", False),
            }
            for a in archived
        ],
    }
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache",
    )
    return manifest
