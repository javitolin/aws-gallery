"""Storage seam shared by the Lambda and the local dev server.

The two differ only in where bytes live, so the mutations are written against
this protocol and implemented once. Without it every action has to be written
twice and the two drift.
"""
from typing import Iterator, Protocol


class Store(Protocol):
    def get(self, key: str) -> bytes | None: ...

    def put(self, key: str, body: bytes, content_type: str = "application/json") -> None: ...

    def list(self, prefix: str) -> Iterator[str]: ...

    def tag(self, key: str, tags: dict[str, str]) -> None: ...


class S3Store:
    def __init__(self, client, bucket: str) -> None:
        self._s3 = client
        self._bucket = bucket

    def get(self, key: str) -> bytes | None:
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except Exception:
            return None

    def put(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        self._s3.put_object(
            Bucket=self._bucket, Key=key, Body=body,
            ContentType=f"{content_type}; charset=utf-8",
        )

    def list(self, prefix: str) -> Iterator[str]:
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def tag(self, key: str, tags: dict[str, str]) -> None:
        """put_object_tagging replaces the whole set, so read-modify-write."""
        try:
            existing = {
                t["Key"]: t["Value"]
                for t in self._s3.get_object_tagging(Bucket=self._bucket, Key=key)["TagSet"]
            }
        except Exception:
            existing = {}
        existing.update(tags)
        self._s3.put_object_tagging(
            Bucket=self._bucket, Key=key,
            Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in existing.items()]},
        )
