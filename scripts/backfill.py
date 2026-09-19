#!/usr/bin/env python3
"""Replay media objects through the processor.

Needed when S3 events were dropped — a throttled notification retries a few
times and is then discarded, so the thumbnail and metadata never appear. By
default only objects that are actually missing a thumbnail are replayed.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import boto3

BUCKET = os.environ["GALLERY_BUCKET"]
FUNCTION = "gallery-media-processor"
MEDIA_PREFIX = "media/"
THUMB_PREFIX = "thumbs/"


def listing(s3, prefix):
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/"):
                yield obj["Key"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="replay everything, not just gaps")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    s3 = boto3.client("s3")
    media = list(listing(s3, MEDIA_PREFIX))
    have_thumb = {k[len(THUMB_PREFIX):].rsplit(".", 1)[0] for k in listing(s3, THUMB_PREFIX)}

    todo = media if args.all else [
        k for k in media if k[len(MEDIA_PREFIX):] not in have_thumb
    ]
    print(f"  media objects   : {len(media)}")
    print(f"  with a thumbnail: {len(have_thumb)}")
    print(f"  to replay       : {len(todo)}")

    if not args.apply:
        print("\ndry run — re-run with --apply")
        return 0

    awslambda = boto3.client("lambda")
    done = [0]

    def replay(key):
        # Async: the processor writes thumb, sidecar and manifest on its own.
        awslambda.invoke(
            FunctionName=FUNCTION,
            InvocationType="Event",
            Payload=json.dumps({"Records": [{
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": BUCKET}, "object": {"key": key}},
            }]}).encode(),
        )
        done[0] += 1
        if done[0] % 200 == 0:
            print(f"    queued {done[0]}/{len(todo)}")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(replay, todo))

    print(f"\n  queued {done[0]} invocations; thumbnails appear as they complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
