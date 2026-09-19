import json
import os
import tempfile
import urllib.parse

import boto3

import manifest
import thumbnail
import filename_dates
from models import MediaRecord

BUCKET = os.environ["BUCKET"]
MEDIA_PREFIX = os.environ.get("MEDIA_PREFIX", "media/")
THUMB_PREFIX = os.environ.get("THUMB_PREFIX", "thumbs/")
META_PREFIX = os.environ.get("META_PREFIX", "meta/")
ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "archive/")
MANIFEST_KEY = os.environ.get("MANIFEST_KEY", "manifest.json")

s3 = boto3.client("s3")

EXTRACTORS = {
    "image": thumbnail.from_image,
    "video": thumbnail.from_video,
    "audio": thumbnail.from_audio,
}


def _sidecar(key: str) -> str:
    return f"{META_PREFIX}{key[len(MEDIA_PREFIX):]}.json"


def _thumb(key: str) -> str:
    return f"{THUMB_PREFIX}{key[len(MEDIA_PREFIX):]}.webp"


# Keys are content hashes, so the display name, source path and category come
# from the sidecar the uploader writes before the object lands. Choices made in
# the gallery must survive reprocessing too.
STICKY_FIELDS = ("name", "source_path", "category", "source_mtime",
                 "archived", "archived_by", "archived_at",
                 "category_by", "category_at")


def _previous(key: str) -> MediaRecord | None:
    try:
        body = s3.get_object(Bucket=BUCKET, Key=_sidecar(key))["Body"].read()
        return MediaRecord.model_validate_json(body)
    except Exception:
        return None


def _preserve_manual(record: MediaRecord) -> MediaRecord:
    try:
        body = s3.get_object(Bucket=BUCKET, Key=_sidecar(record.key))["Body"].read()
        previous = MediaRecord.model_validate_json(body)
    except Exception:
        return record
    return record.model_copy(
        update={field: getattr(previous, field) for field in STICKY_FIELDS}
    )


def _write_meta(record: MediaRecord) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=_sidecar(record.key),
        Body=_preserve_manual(record).to_json(),
        ContentType="application/json; charset=utf-8",
    )


def _process(key: str) -> None:
    head = s3.head_object(Bucket=BUCKET, Key=key)
    kind = thumbnail.kind_for(key)  # extension survives in the hashed key
    record = MediaRecord(
        key=key,
        # Overridden by the uploader's sidecar; the key is only a hash.
        name=os.path.basename(key),
        kind=kind,
        size=head["ContentLength"],
        modified_at=head["LastModified"].isoformat(),
    )

    extractor = EXTRACTORS.get(kind)
    if extractor is None:
        record.reason = f"unsupported format: {os.path.splitext(key)[1] or 'none'}"
        return _write_meta(record)
    if head["ContentLength"] > thumbnail.MAX_TRANSCODE_BYTES:
        record.reason = "file too large to thumbnail"
        return _write_meta(record)

    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(key)[1]) as local:
        s3.download_fileobj(BUCKET, key, local)
        local.flush()
        try:
            thumb, meta = extractor(local.name)
        except Exception as exc:
            record.reason = f"{type(exc).__name__}: {exc}"
            return _write_meta(record)

    for field, value in meta.items():
        if value is not None:
            setattr(record, field, value)

    # The uploader's sidecar holds the real filename and source mtime; the key
    # is only a hash, so read them back before dating the item.
    previous = _previous(key)
    if not record.taken_at and previous:
        record.taken_at = filename_dates.from_name(previous.name)
    if previous and previous.source_mtime:
        record.source_mtime = previous.source_mtime

    if thumb:
        s3.put_object(
            Bucket=BUCKET,
            Key=_thumb(key),
            Body=thumb,
            ContentType="image/webp",
            CacheControl="public, max-age=31536000, immutable",
        )
        record.thumb = _thumb(key)
    elif kind != "audio":
        # No cover art is normal for audio; the UI draws its own tile.
        record.reason = "thumbnail extraction produced no frame"
        return _write_meta(record)

    record.renderable = True
    _write_meta(record)


def _remove(key: str) -> None:
    s3.delete_objects(
        Bucket=BUCKET,
        Delete={"Objects": [{"Key": _sidecar(key)}, {"Key": _thumb(key)}], "Quiet": True},
    )


def lambda_handler(event: dict, _context: object) -> dict:
    """Per-object work only, unless asked to rebuild.

    Rebuilding the manifest reads every sidecar, so doing it per object made a
    bulk upload quadratic and forced a concurrency cap that silently dropped S3
    events. Uploads end with one explicit rebuild instead; removals rebuild
    immediately since nothing else will.
    """
    records = event.get("Records", [])
    rebuild_only = not records or event.get("rebuild")

    touched = removed = 0
    for record in records:
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        if not key.startswith(MEDIA_PREFIX) or key.endswith("/"):
            continue
        if record["eventName"].startswith("ObjectRemoved"):
            _remove(key)
            removed += 1
        else:
            _process(key)
            touched += 1

    if rebuild_only or removed:
        result = manifest.rebuild(BUCKET, META_PREFIX, ARCHIVE_PREFIX, MANIFEST_KEY)
        return {"touched": touched, "removed": removed,
                "items": len(result.items), "rebuilt": True}
    return {"touched": touched, "rebuilt": False}
