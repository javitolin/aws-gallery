#!/usr/bin/env python3
"""Serve site/ locally for fast frontend iteration.

Two sources:
  --local DIR   build the manifest from files on disk, generating thumbnails
                with ffmpeg into a cache. Nothing needs to be uploaded first.
  (default)     proxy the real bucket with presigned URLs.

Static files always come off disk, so an edit is visible on refresh.
"""
import argparse
import html
import http.server
import json
import mimetypes
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
# The gallery actions are implemented once, in the Lambda package, and reused
# here over a local store so the preview cannot drift from production.
sys.path[:0] = [str(ROOT / "lambdas" / "api"), str(ROOT / "lambdas" / "media_processor")]

# Imported after the path is set, because these live in the Lambda packages and
# are deliberately the same code the deployed API runs.
import boto3  # noqa: E402
import mutations  # noqa: E402
from models import MediaRecord  # noqa: E402
SITE = ROOT / "site"
CACHE = ROOT / ".devcache"

BUCKET = os.environ.get("GALLERY_BUCKET", "")
S3_PREFIXES = ("manifest.json", "thumbs/", "media/", "archive/", "meta/")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mpg", ".mpeg", ".avi", ".wmv"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac"}
# A browser can render these. Images always can; the list only really constrains
# video and audio containers.
PLAYABLE = IMAGE_EXT | {".mp4", ".mov", ".m4v", ".webm", ".mp3", ".m4a", ".wav", ".ogg"}
SKIP = {".sfk", ".ds_store", ".veg", ".bak"}

class LocalStore:
    """Same protocol as S3Store, backed by files under the dev cache."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        return self._root / key

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.exists() else None

    def put(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def list(self, prefix: str) -> Iterator[str]:
        base = self._path(prefix)
        if not base.exists():
            return
        for path in base.rglob("*"):
            if path.is_file():
                yield str(path.relative_to(self._root))

    def tag(self, key: str, tags: dict[str, str]) -> None:
        """No object tags locally; the sidecar already carries the same state."""


state = {"mode": "s3", "source": None, "manifest": None, "store": None}
# Archiving in local mode is recorded here so the preview survives a restart.
def seed_sidecars() -> None:
    """Write a sidecar per scanned file so the shared mutations have something
    to act on, exactly as publish.py does before uploading."""
    store = state["store"]
    for item in state["manifest"]["all"]:
        if store.get(mutations.sidecar_key(item["key"])):
            continue  # keep earlier local edits
        store.put(mutations.sidecar_key(item["key"]),
                  MediaRecord.model_validate(item).to_json())


def local_manifest() -> dict:
    """Same split the production manifest builder makes, over local sidecars."""
    store = state["store"]
    records = []
    for key in store.list(mutations.META_PREFIX):
        body = store.get(key)
        if body:
            records.append(MediaRecord.model_validate_json(body))

    live = [r for r in records if not r.archived]
    archived = [r for r in records if r.archived]
    live.sort(key=MediaRecord.sort_key, reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": [r.model_dump(exclude_none=True) for r in live],
        "archive": [dict(r.model_dump(exclude_none=True), storage_class="archived",
                         playable=r.renderable) for r in archived],
    }


DEV_USER = "you@localhost"


def _archive(payload: dict, who: str, at: str) -> dict:
    changed, failed = mutations.apply_to_keys(
        payload.get("keys") or [], partial(mutations.archive, state["store"], who=who, at=at))
    return {"archived": changed, "failed": failed}


def _category(payload: dict, who: str, at: str) -> dict:
    changed, failed = mutations.apply_to_keys(
        payload.get("keys") or [],
        partial(mutations.recategorise, state["store"],
                category=payload.get("category", ""), who=who, at=at))
    return {"moved": changed, "failed": failed}


def _rename(payload: dict, who: str, at: str) -> dict:
    changed, failed = mutations.rename_category(
        state["store"], (payload.get("from") or "").strip("/"),
        (payload.get("to") or "").strip("/"), who=who, at=at)
    return {"renamed": changed, "failed": failed}


def _favourite(payload: dict, who: str, at: str) -> dict:
    return mutations.set_favourites(
        state["store"], who, payload.get("keys") or [], on=payload.get("on", True), at=at)


def _hide(payload: dict, who: str, at: str) -> dict:
    return mutations.set_hidden(
        state["store"], who, payload.get("categories") or [], on=payload.get("on", True), at=at)


def _prefs(_payload: dict, who: str, _at: str) -> dict:
    return mutations.load_prefs(state["store"], who)


ROUTES = {
    "/api/archive": _archive,
    "/api/category": _category,
    "/api/rename-category": _rename,
    "/api/favourite": _favourite,
    "/api/hide": _hide,
    "/api/prefs": _prefs,
}


def kind_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "other"


def make_thumb(src: Path, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    kind = kind_for(src)
    if kind == "image":
        cmd = ["ffmpeg", "-y", "-i", str(src), "-vf", "scale=512:-2",
               "-frames:v", "1", "-update", "1", "-q:v", "4", str(dest)]
    elif kind == "video":
        # -ss before -i seeks by keyframe, so this stays fast on multi-GB files.
        cmd = ["ffmpeg", "-y", "-ss", "3", "-i", str(src), "-frames:v", "1",
               "-update", "1", "-vf", "scale=512:-2", "-q:v", "4", str(dest)]
    else:
        return False
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    return result.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def _by_modified(item: dict) -> str:
    return item["modified_at"]


def build_manifest(source: Path) -> dict:
    items = []
    print(f"scanning {source} …")
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        # Dotfiles are the uploader's own bookkeeping, not media.
        if (path.suffix.lower() in SKIP or path.name.startswith(".")
                or path.stat().st_size == 0):
            continue
        rel = path.relative_to(source)
        category = rel.parts[0] if len(rel.parts) > 1 else None
        key = f"media/{rel.as_posix()}"
        thumb_rel = f"thumbs/{rel.as_posix()}.jpg"

        ok = make_thumb(path, CACHE / thumb_rel)
        stat = path.stat()
        items.append({
            "key": key,
            "name": path.name,
            "kind": kind_for(path),
            "category": category,
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "taken_at": None,
            "thumb": thumb_rel if ok else None,
            "playable": path.suffix.lower() in PLAYABLE,
            "renderable": True,
        })
        print(f"  {'thumb' if ok else 'no-thumb'}  {key}")

    items.sort(key=_by_modified, reverse=True)
    print(f"{len(items)} items\n")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "all": items}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def _send_file(self, path: Path):
        """Serve with Range support so video seeking works."""
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng and rng.startswith("bytes="):
            part = rng[6:].split("-")
            start = int(part[0]) if part[0] else 0
            if len(part) > 1 and part[1]:
                end = min(int(part[1]), size - 1)
            status = 206

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def do_GET(self):
        key = urllib.parse.unquote(self.path.lstrip("/").split("?")[0])

        if key.startswith("api/") and self.do_GET_api("/" + key):
            return

        if state["mode"] == "local":
            if key == "manifest.json":
                body = json.dumps(local_manifest(), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if key.startswith("thumbs/"):
                target = CACHE / key
                if target.exists():
                    return self._send_file(target)
                self.send_error(404)
                return
            if key.startswith("media/"):
                target = state["source"] / key[len("media/"):]
                if target.exists():
                    return self._send_file(target)
                self.send_error(404)
                return
        elif key.startswith(S3_PREFIXES):
            url = boto3.client("s3").generate_presigned_url(
                "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=3600)
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()
            return

        if key.startswith("auth/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<!doctype html><meta charset=utf-8>"
                b"<body style='font-family:system-ui;background:#111;color:#eee;padding:3rem'>"
                b"<h1>Auth stub</h1><p>Sign-in only runs on CloudFront. "
                b"<a style='color:#8ab4f8' href='/'>Back</a></p>")
            return

        super().do_GET()

    def do_POST(self):
        key = urllib.parse.unquote(self.path.lstrip("/").split("?")[0])
        route = "/" + key
        if state["mode"] != "local" or route not in ROUTES:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_error(400)
            return

        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        try:
            result = ROUTES[route](payload, DEV_USER, at)
        except Exception as exc:
            body = json.dumps({"error": str(exc)}).encode()
            self.send_response(400)
        else:
            body = json.dumps(result).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET_api(self, route: str) -> bool:
        if state["mode"] != "local" or route not in ROUTES:
            return False
        body = json.dumps(ROUTES[route]({}, DEV_USER, "")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def log_message(self, fmt, *args):
        print(f"  {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--local", type=Path, help="preview a local directory instead of S3")
    args = parser.parse_args()

    if args.local:
        state["mode"] = "local"
        state["source"] = args.local.resolve()
        state["store"] = LocalStore(CACHE / "store")
        state["manifest"] = build_manifest(state["source"])
        seed_sidecars()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving {SITE} on http://localhost:{args.port}")
    print(f"content: {state['source'] if args.local else BUCKET + ' (presigned)'}")
    print("edit site/* and refresh\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
