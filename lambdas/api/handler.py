"""Write endpoint for the gallery.

CloudFront validates the viewer's signed cookie on this behaviour before the
request arrives, so there is no session check here — an unauthenticated caller
never reaches the function. The identity cookie is verified because a valid
session says someone is signed in, not who.

What each action does lives in mutations.py, shared with the local dev server.
"""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from functools import partial
from typing import Callable

import boto3
from pydantic import ValidationError

import manifest
import mutations
from schemas import CategoryRequest, FavouriteRequest, KeysRequest, RenameRequest
from store import S3Store

BUCKET = os.environ["BUCKET"]
META_PREFIX = os.environ.get("META_PREFIX", "meta/")
ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "archive/")
MANIFEST_KEY = os.environ.get("MANIFEST_KEY", "manifest.json")
HMAC_PARAM = os.environ["HMAC_PARAM"]

store = S3Store(boto3.client("s3"), BUCKET)
_hmac_key: bytes | None = None


def _identity_secret() -> bytes:
    global _hmac_key
    if _hmac_key is None:
        _hmac_key = boto3.client("ssm").get_parameter(
            Name=HMAC_PARAM, WithDecryption=True
        )["Parameter"]["Value"].encode()
    return _hmac_key


def caller(event: dict) -> str | None:
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


def _first_error(exc: ValidationError) -> str:
    problem = exc.errors()[0]
    field = ".".join(str(p) for p in problem["loc"]) or "body"
    return f"{field}: {problem['msg']}"


def _rebuild(changed: int) -> int | None:
    """Rebuilding reads every sidecar, so skip it when nothing moved."""
    if not changed:
        return None
    return len(manifest.rebuild(BUCKET, META_PREFIX, ARCHIVE_PREFIX, MANIFEST_KEY).items)


def route_archive(body: dict, who: str, at: str) -> dict:
    request = KeysRequest.model_validate(body)
    changed, failures = mutations.apply_to_keys(
        request.keys,
        partial(mutations.archive, store, who=who, at=at),
    )
    return _reply(200 if changed else 400, {
        "archived": changed, "by": who, "at": at,
        "failed": failures, "items": _rebuild(changed),
    })


def route_category(body: dict, who: str, at: str) -> dict:
    request = CategoryRequest.model_validate(body)
    changed, failures = mutations.apply_to_keys(
        request.keys,
        partial(mutations.recategorise, store, category=request.category, who=who, at=at),
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
    changed, failures = mutations.rename_category(store, old, new, who=who, at=at)
    return _reply(200 if changed else 400, {
        "renamed": changed, "from": old, "to": new, "by": who, "at": at,
        "failed": failures, "items": _rebuild(changed),
    })


def route_favourite(body: dict, who: str, at: str) -> dict:
    """Per user, so it never touches the manifest and needs no rebuild."""
    request = FavouriteRequest.model_validate(body)
    keys = mutations.set_favourites(store, who, request.keys, on=request.on, at=at)
    return _reply(200, {"favourites": keys, "by": who, "at": at})


def route_favourites(_body: dict, who: str, _at: str) -> dict:
    return _reply(200, {"favourites": mutations.load_favourites(store, who)})


ROUTES: dict[str, Callable[[dict, str, str], dict]] = {
    "/api/archive": route_archive,
    "/api/category": route_category,
    "/api/rename-category": route_rename,
    "/api/favourite": route_favourite,
    "/api/favourites": route_favourites,
}


def lambda_handler(event: dict, _context: object) -> dict:
    http = event.get("requestContext", {}).get("http", {})
    route = http.get("path", "").rstrip("/")
    if http.get("method") not in ("POST", "GET") or route not in ROUTES:
        return _reply(404, {"error": "unknown route"})

    who = caller(event)
    if not who:
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
