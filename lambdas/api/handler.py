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

import boto3

import manifest

BUCKET = os.environ["BUCKET"]
MEDIA_PREFIX = os.environ.get("MEDIA_PREFIX", "media/")
META_PREFIX = os.environ.get("META_PREFIX", "meta/")
ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "archive/")
MANIFEST_KEY = os.environ.get("MANIFEST_KEY", "manifest.json")
HMAC_PARAM = os.environ["HMAC_PARAM"]

s3 = boto3.client("s3")
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


def _load_record(key: str):
    """Sidecar for a media key, or None if it has not been processed yet."""
    if not key.startswith(MEDIA_PREFIX) or key.endswith("/"):
        return None
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=_sidecar(key))["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def _save_record(key: str, record: dict) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=_sidecar(key),
        Body=json.dumps(record, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def _archive_one(key: str, who: str, at: str) -> str | None:
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
    record.update({"archived": True, "archived_by": who, "archived_at": at})
    _save_record(key, record)
    return None


def _recategorise_one(key: str, category: str, who: str, at: str) -> str | None:
    """Category lives in the sidecar, so the object never has to move."""
    record = _load_record(key)
    if record is None:
        return "no metadata for this item"

    _merge_tags(key, {
        "category": TAG_SAFE.sub("_", category)[:256],
        "category_by": TAG_SAFE.sub("_", who)[:256],
        "category_at": at,
    })
    record.update({"category": category, "category_by": who, "category_at": at})
    _save_record(key, record)
    return None


MAX_CATEGORY = 120


def _rename_category(old: str, new: str, who: str, at: str) -> tuple[int, dict]:
    """Rewrite a category across every item, descendants included.

    Categories nest with '/', so renaming a parent has to carry its children
    with it or they would be orphaned under a name that no longer exists.
    """
    changed, failures = 0, {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=META_PREFIX):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".json"):
                continue
            try:
                record = json.loads(
                    s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
                )
            except Exception as exc:
                failures[obj["Key"]] = str(exc)
                continue

            current = record.get("category")
            if current != old and not (current or "").startswith(old + "/"):
                continue

            record["category"] = new + current[len(old):]
            record["category_by"] = who
            record["category_at"] = at
            _save_record(record["key"], record)
            _merge_tags(record["key"], {
                "category": TAG_SAFE.sub("_", record["category"])[:256],
                "category_by": TAG_SAFE.sub("_", who)[:256],
                "category_at": at,
            })
            changed += 1
    return changed, failures


def lambda_handler(event, _context):
    http = event.get("requestContext", {}).get("http", {})
    route = http.get("path", "").rstrip("/")
    if http.get("method") != "POST" or route not in (
        "/api/archive", "/api/category", "/api/rename-category"
    ):
        return _reply(404, {"error": "unknown route"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _reply(400, {"error": "malformed body"})

    who = caller(event)
    if not who:
        # CloudFront already proved there is a session; this only proves who.
        return _reply(401, {"error": "identity cookie missing or invalid; sign in again"})
    at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    if route == "/api/rename-category":
        old = (body.get("from") or "").strip().strip("/")
        new = (body.get("to") or "").strip().strip("/")
        if not old or not new:
            return _reply(400, {"error": "both from and to are required"})
        if len(new) > MAX_CATEGORY:
            return _reply(400, {"error": f"category longer than {MAX_CATEGORY} characters"})
        if old == new:
            return _reply(400, {"error": "from and to are the same"})
        changed, failures = _rename_category(old, new, who, at)
        result = manifest.rebuild(BUCKET, META_PREFIX, ARCHIVE_PREFIX, MANIFEST_KEY)
        return _reply(200 if changed else 400, {
            "renamed": changed, "from": old, "to": new,
            "by": who, "at": at, "failed": failures,
            "items": len(result["items"]),
        })

    keys = body.get("keys") or []
    if not keys:
        return _reply(400, {"error": "no keys given"})

    if route == "/api/category":
        category = (body.get("category") or "").strip()
        if not category:
            return _reply(400, {"error": "no category given"})
        if len(category) > MAX_CATEGORY:
            return _reply(400, {"error": f"category longer than {MAX_CATEGORY} characters"})
        apply_one = lambda key: _recategorise_one(key, category, who, at)  # noqa: E731
        verb = "moved"
    else:
        apply_one = lambda key: _archive_one(key, who, at)  # noqa: E731
        verb = "archived"

    failures = {}
    changed = 0
    for key in keys:
        error = apply_one(key)
        if error:
            failures[key] = error
        else:
            changed += 1

    result = manifest.rebuild(BUCKET, META_PREFIX, ARCHIVE_PREFIX, MANIFEST_KEY)
    return _reply(200 if changed else 400, {
        verb: changed,
        "by": who,
        "at": at,
        "failed": failures,
        "items": len(result["items"]),
    })
