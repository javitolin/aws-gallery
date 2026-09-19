"""Write endpoint for the gallery.

CloudFront validates the viewer's signed cookie on this behaviour before the
request arrives, so there is no auth logic here — an unauthenticated caller
never reaches the function.

Archiving tags the object so a lifecycle rule can cool it, and flags the
metadata sidecar so the manifest moves it out of the timeline. It never
deletes or renames anything.
"""
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable

import boto3
from pydantic import ValidationError

import manifest
from models import MediaRecord
from schemas import CategoryRequest, KeysRequest, RenameRequest

BUCKET = os.environ["BUCKET"]
MEDIA_PREFIX = os.environ.get("MEDIA_PREFIX", "media/")
META_PREFIX = os.environ.get("META_PREFIX", "meta/")
ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "archive/")
MANIFEST_KEY = os.environ.get("MANIFEST_KEY", "manifest.json")
HMAC_PARAM = os.environ["HMAC_PARAM"]

s3 = boto3.client("s3")

# Bulk operations are network-bound, so a pool turns minutes into seconds.
WORKERS = 32
_hmac_key = None

# S3 tag values allow Unicode letters and digits, spaces, and + - = . _ : / @
# Hebrew category names are therefore fine; the ASCII-only version was too strict.
TAG_SAFE = re.compile(r"[^\w +\-=.:/@]", re.UNICODE)


def _identity_secret() -> bytes:
    global _hmac_key
    if _hmac_key is None:
        _hmac_key = boto3.client("ssm").get_parameter(
            Name=HMAC_PARAM, WithDecryption=True
        )["Parameter"]["Value"].encode()
    return _hmac_key


def caller(event) -> str | None:
    """Email from the signed identity cookie, or None if absent or forged."""
    cookies = {}
    for raw in event.get("cookies") or []:
        name, _, value = raw.partition("=")
        cookies[name.strip()] = value

    token = cookies.get("gallery_user")
    if not token:
        return None
    try:
        email, expires, digest = token.rsplit("|", 2)
    except ValueError:
        return None

    expected = hmac.new(
        _identity_secret(), f"{email}|{expires}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, digest):
        return None
    if int(expires) < time.time():
        return None
    return email


def _reply(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body),
    }


def _sidecar(key: str) -> str:
    return f"{META_PREFIX}{key[len(MEDIA_PREFIX):]}.json"


def _merge_tags(key: str, updates: dict) -> None:
    """put_object_tagging replaces the whole set, so read-modify-write."""
    try:
        existing = {
            t["Key"]: t["Value"]
            for t in s3.get_object_tagging(Bucket=BUCKET, Key=key)["TagSet"]
        }
    except Exception:
        existing = {}
    existing.update(updates)
    s3.put_object_tagging(
        Bucket=BUCKET,
        Key=key,
        Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in existing.items()]},
    )


def _load_record(key: str) -> MediaRecord | None:
    """Sidecar for a media key, or None if it has not been processed yet."""
    if not key.startswith(MEDIA_PREFIX) or key.endswith("/"):
        return None
    try:
        body = s3.get_object(Bucket=BUCKET, Key=_sidecar(key))["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None
    return MediaRecord.model_validate_json(body)


def _save_record(record: MediaRecord) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=_sidecar(record.key),
        Body=record.to_json(),
        ContentType="application/json; charset=utf-8",
    )


def _archive_one(key: str, *, who: str, at: str) -> str | None:
    """Returns an error string, or None on success."""
    record = _load_record(key)
    if record is None:
        return "no metadata for this item"

    # Drives the lifecycle rule; the object itself stays exactly where it is.
    _merge_tags(key, {
        "archived": "true",
        "archived_by": TAG_SAFE.sub("_", who)[:256],
        "archived_at": at,
    })
    record.archived, record.archived_by, record.archived_at = True, who, at
    _save_record(record)
    return None


def _recategorise_one(key: str, *, category: str, who: str, at: str) -> str | None:
    """Category lives in the sidecar, so the object never has to move."""
    record = _load_record(key)
    if record is None:
        return "no metadata for this item"

    _merge_tags(key, {
        "category": TAG_SAFE.sub("_", category)[:256],
        "category_by": TAG_SAFE.sub("_", who)[:256],
        "category_at": at,
    })
    record.category, record.category_by, record.category_at = category, who, at
    _save_record(record)
    return None


MAX_CATEGORY = 120


def _meta_keys() -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    return [
        obj["Key"]
        for page in paginator.paginate(Bucket=BUCKET, Prefix=META_PREFIX)
        for obj in page.get("Contents", [])
        if obj["Key"].endswith(".json")
    ]


def _read_sidecar(meta_key: str) -> MediaRecord | None:
    try:
        body = s3.get_object(Bucket=BUCKET, Key=meta_key)["Body"].read()
        return MediaRecord.model_validate_json(body)
    except Exception:
        return None


def _rename_one(record: MediaRecord, old: str, new: str, who: str, at: str) -> bool:
    current = record.category or ""
    if current != old and not current.startswith(old + "/"):
        return False
    record.category = new + current[len(old):]
    record.category_by, record.category_at = who, at
    _save_record(record)
    _merge_tags(record.key, {
        "category": TAG_SAFE.sub("_", record.category)[:256],
        "category_by": TAG_SAFE.sub("_", who)[:256],
        "category_at": at,
    })
    return True


def _rename_category(old: str, new: str, who: str, at: str) -> tuple[int, dict]:
    """Rewrite a category across every item, descendants included.

    Categories nest with '/', so renaming a parent has to carry its children
    with it or they would be orphaned under a name that no longer exists.

    Reads and writes run in a pool: serially this is one GetObject per item and
    timed out the function at a few thousand items.
    """
    keys = _meta_keys()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        records = [r for r in pool.map(_read_sidecar, keys) if r is not None]

    failures: dict[str, str] = {}
    if len(records) < len(keys):
        failures["unreadable"] = f"{len(keys) - len(records)} sidecar(s) could not be read"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = pool.map(partial(_rename_one, old=old, new=new, who=who, at=at), records)
    return sum(1 for renamed in results if renamed), failures


def lambda_handler(event: dict, _context: object) -> dict:
    http = event.get("requestContext", {}).get("http", {})
    route = http.get("path", "").rstrip("/")
    if http.get("method") != "POST" or route not in ROUTES:
        return _reply(404, {"error": "unknown route"})

    who = caller(event)
    if not who:
        # CloudFront already proved there is a session; this only proves who.
        return _reply(401, {"error": "identity cookie missing or invalid; sign in again"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _reply(400, {"error": "malformed body"})

    at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        return ROUTES[route](body, who, at)
    except ValidationError as exc:
        return _reply(400, {"error": _first_error(exc)})


def _first_error(exc: ValidationError) -> str:
    problem = exc.errors()[0]
    field = ".".join(str(p) for p in problem["loc"]) or "body"
    return f"{field}: {problem['msg']}"


def _apply_to_keys(keys: list[str], action: Callable[[str], str | None]) -> tuple[int, dict]:
    failures: dict[str, str] = {}
    changed = 0
    for key in keys:
        error = action(key)
        if error:
            failures[key] = error
        else:
            changed += 1
    return changed, failures


def _rebuild(changed: int) -> int | None:
    """Rebuilding reads every sidecar, so skip it when nothing moved."""
    if not changed:
        return None
    return len(manifest.rebuild(BUCKET, META_PREFIX, ARCHIVE_PREFIX, MANIFEST_KEY).items)


def route_archive(body: dict, who: str, at: str) -> dict:
    request = KeysRequest.model_validate(body)
    changed, failures = _apply_to_keys(
        request.keys, partial(_archive_one, who=who, at=at)
    )
    return _reply(200 if changed else 400, {
        "archived": changed, "by": who, "at": at,
        "failed": failures, "items": _rebuild(changed),
    })


def route_category(body: dict, who: str, at: str) -> dict:
    request = CategoryRequest.model_validate(body)
    changed, failures = _apply_to_keys(
        request.keys, partial(_recategorise_one, category=request.category, who=who, at=at)
    )
    return _reply(200 if changed else 400, {
        "moved": changed, "by": who, "at": at,
        "failed": failures, "items": _rebuild(changed),
    })


def route_rename(body: dict, who: str, at: str) -> dict:
    request = RenameRequest.model_validate(body)
    old, new = request.source.strip("/"), request.target.strip("/")
    if old == new:
        return _reply(400, {"error": "from and to are the same"})
    changed, failures = _rename_category(old, new, who, at)
    return _reply(200 if changed else 400, {
        "renamed": changed, "from": old, "to": new, "by": who, "at": at,
        "failed": failures, "items": _rebuild(changed),
    })


ROUTES: dict[str, Callable[[dict, str, str], dict]] = {
    "/api/archive": route_archive,
    "/api/category": route_category,
    "/api/rename-category": route_rename,
}
