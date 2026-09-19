#!/usr/bin/env python3
"""Replay every existing media/ object through the processor.

The Lambda is only wired to future S3 events, so objects already in the bucket
need one synthetic event each to get thumbnails and metadata.
"""
import argparse
import json
import sys

import boto3

BUCKET = os.environ["GALLERY_BUCKET"]
FUNCTION = "gallery-media-processor"
PREFIX = "media/"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default=PREFIX)
    args = parser.parse_args()

    s3 = boto3.client("s3")
    awslambda = boto3.client("lambda")

    keys = [
        obj["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=args.prefix)
        for obj in page.get("Contents", [])
        if not obj["Key"].endswith("/")
    ]
    print(f"{len(keys)} objects under {args.prefix}")

    failures = 0
    for i, key in enumerate(keys, 1):
        event = {
            "Records": [
                {
                    "eventName": "ObjectCreated:Put",
                    "s3": {"bucket": {"name": BUCKET}, "object": {"key": key}},
                }
            ]
        }
        response = awslambda.invoke(
            FunctionName=FUNCTION, Payload=json.dumps(event).encode()
        )
        payload = json.loads(response["Payload"].read())
        if "FunctionError" in response:
            failures += 1
            print(f"  [{i}/{len(keys)}] FAIL {key}: {payload.get('errorMessage', payload)}")
        else:
            print(f"  [{i}/{len(keys)}] ok   {key}")

    print(f"\n{len(keys) - failures} ok, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
