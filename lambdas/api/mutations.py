"""What each gallery action does, independent of where the bytes live.

Nothing here deletes or renames an object: archiving and recategorising write a
tag and a sidecar field, and the object keeps its key throughout.
"""
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from models import MediaRecord
from store import Store

MEDIA_PREFIX = "media/"
META_PREFIX = "meta/"
FAVOURITES_PREFIX = "favourites/"

# S3 tag values allow Unicode letters and digits, spaces, and + - = . _ : / @
TAG_SAFE = re.compile(r"[^\w +\-=.:/@]", re.UNICODE)

# Bulk work here is network-bound, so a pool turns minutes into seconds.
WORKERS = 32


def sidecar_key(media_key: str) -> str:
    return f"{META_PREFIX}{media_key[len(MEDIA_PREFIX):]}.json"


def favourites_key(email: str) -> str:
    """One file per user. Hashed so member emails never appear in object keys."""
    return f"{FAVOURITES_PREFIX}{hashlib.sha256(email.encode()).hexdigest()[:16]}.json"


def load_record(store: Store, media_key: str) -> MediaRecord | None:
    if not media_key.startswith(MEDIA_PREFIX) or media_key.endswith("/"):
        return None
    body = store.get(sidecar_key(media_key))
    return MediaRecord.model_validate_json(body) if body else None


def save_record(store: Store, record: MediaRecord) -> None:
    store.put(sidecar_key(record.key), record.to_json())


def archive(store: Store, media_key: str, *, who: str, at: str) -> str | None:
    record = load_record(store, media_key)
    if record is None:
        return "no metadata for this item"
    # Drives the lifecycle rule; the object itself stays exactly where it is.
    store.tag(media_key, {
        "archived": "true",
        "archived_by": TAG_SAFE.sub("_", who)[:256],
        "archived_at": at,
    })
    record.archived, record.archived_by, record.archived_at = True, who, at
    save_record(store, record)
    return None


def recategorise(store: Store, media_key: str, *, category: str, who: str, at: str) -> str | None:
    record = load_record(store, media_key)
    if record is None:
        return "no metadata for this item"
    store.tag(media_key, {
        "category": TAG_SAFE.sub("_", category)[:256],
        "category_by": TAG_SAFE.sub("_", who)[:256],
        "category_at": at,
    })
    record.category, record.category_by, record.category_at = category, who, at
    save_record(store, record)
    return None


def apply_to_keys(keys: list[str], action) -> tuple[int, dict[str, str]]:
    failures: dict[str, str] = {}
    changed = 0
    for key in keys:
        error = action(key)
        if error:
            failures[key] = error
        else:
            changed += 1
    return changed, failures


def _read_sidecar(store: Store, meta_key: str) -> MediaRecord | None:
    body = store.get(meta_key)
    if not body:
        return None
    try:
        return MediaRecord.model_validate_json(body)
    except Exception:
        return None


def _rename_one(record: MediaRecord, store: Store, old: str, new: str, who: str, at: str) -> bool:
    current = record.category or ""
    if current != old and not current.startswith(old + "/"):
        return False
    record.category = new + current[len(old):]
    record.category_by, record.category_at = who, at
    save_record(store, record)
    store.tag(record.key, {
        "category": TAG_SAFE.sub("_", record.category)[:256],
        "category_by": TAG_SAFE.sub("_", who)[:256],
        "category_at": at,
    })
    return True


def rename_category(store: Store, old: str, new: str, *, who: str, at: str) -> tuple[int, dict]:
    """Categories nest with '/', so a rename must carry descendants with it or
    they are orphaned under a name that no longer exists."""
    keys = [k for k in store.list(META_PREFIX) if k.endswith(".json")]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        records = [r for r in pool.map(partial(_read_sidecar, store), keys) if r is not None]

    failures: dict[str, str] = {}
    if len(records) < len(keys):
        failures["unreadable"] = f"{len(keys) - len(records)} sidecar(s) could not be read"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = pool.map(
            partial(_rename_one, store=store, old=old, new=new, who=who, at=at), records
        )
    return sum(1 for renamed in results if renamed), failures


def load_favourites(store: Store, email: str) -> list[str]:
    body = store.get(favourites_key(email))
    return json.loads(body).get("keys", []) if body else []


def set_favourites(store: Store, email: str, keys: list[str], *, on: bool, at: str) -> list[str]:
    """Per user, in their own file: one person's favourites are not another's,
    and keeping them out of the manifest leaves it identical for everyone."""
    current = set(load_favourites(store, email))
    wanted = {k for k in keys if k.startswith(MEDIA_PREFIX)}
    updated = sorted(current | wanted if on else current - wanted)
    store.put(
        favourites_key(email),
        json.dumps({"email": email, "keys": updated, "updated_at": at}).encode("utf-8"),
    )
    return updated
