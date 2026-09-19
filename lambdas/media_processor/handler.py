import json
import os
import tempfile
import urllib.parse

import boto3

import manifest
import thumbnail

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
STICKY_FIELDS = ("name", "source_path", "category",
                 "archived", "archived_by", "archived_at",
                 "category_by", "category_at")


def _preserve_manual(key: str, record: dict) -> dict:
    try:
        previous = json.loads(
            s3.get_object(Bucket=BUCKET, Key=_sidecar(key))["Body"].read()
        )
    except Exception:
        return record
    for field in STICKY_FIELDS:
        if field in previous:
            record[field] = previous[field]
    return record


def _write_meta(record: dict) -> None:
    record = _preserve_manual(record["key"], record)
    s3.put_object(
        Bucket=BUCKET,
        Key=_sidecar(record["key"]),
        Body=json.dumps(record, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def _process(key: str) -> None:
    head = s3.head_object(Bucket=BUCKET, Key=key)
    kind = thumbnail.kind_for(key)  # extension survives in the hashed key
    record = {
        "key": key,
        # Overridden by the uploader's sidecar; the key is only a hash.
        "name": os.path.basename(key),
        "kind": kind,
        "size": head["ContentLength"],
        "modified_at": head["LastModified"].isoformat(),
        "renderable": False,
    }

    extractor = EXTRACTORS.get(kind)
    if extractor is None:
        record["reason"] = f"unsupported format: {os.path.splitext(key)[1] or 'none'}"
        _write_meta(record)
        return
    if head["ContentLength"] > thumbnail.MAX_TRANSCODE_BYTES:
        record["reason"] = "file too large to thumbnail"
        _write_meta(record)
        return

    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(key)[1]) as local:
        s3.download_fileobj(BUCKET, key, local)
        local.flush()
        try:
            thumb, meta = extractor(local.name)
        except Exception as exc:
            record["reason"] = f"{type(exc).__name__}: {exc}"
            _write_meta(record)
            return

    record.update({k: v for k, v in meta.items() if v is not None})

    if thumb:
        s3.put_object(
            Bucket=BUCKET,
            Key=_thumb(key),
            Body=thumb,
            ContentType="image/webp",
            CacheControl="public, max-age=31536000, immutable",
        )
        record["thumb"] = _thumb(key)
    elif kind == "audio":
        # No cover art is normal for audio; the UI draws its own tile.
        record["thumb"] = None
    else:
        record["reason"] = "thumbnail extraction produced no frame"
        _write_meta(record)
        return

    record["renderable"] = True
    _write_meta(record)


def _remove(key: str) -> None:
    s3.delete_objects(
        Bucket=BUCKET,
        Delete={"Objects": [{"Key": _sidecar(key)}, {"Key": _thumb(key)}], "Quiet": True},
    )


def lambda_handler(event, _context):
    touched = 0
    for record in event.get("Records", []):
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        if not key.startswith(MEDIA_PREFIX) or key.endswith("/"):
            continue
        if record["eventName"].startswith("ObjectRemoved"):
            _remove(key)
        else:
            _process(key)
        touched += 1

    result = manifest.rebuild(BUCKET, META_PREFIX, ARCHIVE_PREFIX, MANIFEST_KEY)
    return {
        "touched": touched,
        "items": len(result["items"]),
        "archive": len(result["archive"]),
    }
