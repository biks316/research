"""S3 storage for exported files."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from botocore.exceptions import ClientError

CHUNK_SIZE = 64 * 1024


class Storage:
    def __init__(self, s3: Any, bucket: str, presign_ttl_seconds: int) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._presign_ttl = presign_ttl_seconds

    async def size(self, key: str) -> int | None:
        """Object size in bytes, or None if it does not exist."""
        try:
            head = await self._s3.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return int(head["ContentLength"])

    async def exists(self, key: str) -> bool:
        return await self.size(key) is not None

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    async def get(self, key: str) -> bytes:
        response = await self._s3.get_object(Bucket=self._bucket, Key=key)
        body = response["Body"]
        async with body:  # entering yields the raw HTTP response, so keep the StreamingBody
            return await body.read()

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        response = await self._s3.get_object(Bucket=self._bucket, Key=key)
        body = response["Body"]
        async with body:
            async for chunk in body.iter_chunks(CHUNK_SIZE):
                yield chunk

    async def presigned_url(self, key: str, filename: str | None = None) -> str:
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return await self._s3.generate_presigned_url("get_object", Params=params, ExpiresIn=self._presign_ttl)

    async def delete_prefix(self, prefix: str) -> int:
        deleted = 0
        paginator = self._s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                await self._s3.delete_objects(Bucket=self._bucket, Delete={"Objects": keys, "Quiet": True})
                deleted += len(keys)
        return deleted
