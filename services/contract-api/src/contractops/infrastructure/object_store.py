from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error

from contractops.application.documents import ObjectMetadata, ObjectStore
from contractops.errors import ContractOpsError


class MinioObjectStore(ObjectStore):
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        *,
        secure: bool,
    ) -> None:
        parsed = urlparse(endpoint)
        host = parsed.netloc or parsed.path
        self._client = Minio(
            host,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure if parsed.scheme == "" else parsed.scheme == "https",
        )
        self._bucket = bucket

    def presign_put(self, object_key: str, *, expires: timedelta) -> str:
        try:
            return self._client.presigned_put_object(self._bucket, object_key, expires=expires)
        except S3Error as error:
            raise ContractOpsError(
                code="object_store_unavailable",
                message="object storage could not issue an upload URL",
                status_code=503,
            ) from error

    def stat(self, object_key: str) -> ObjectMetadata:
        try:
            value = self._client.stat_object(self._bucket, object_key)
            if value.size is None:
                raise ContractOpsError(
                    code="object_store_metadata_invalid",
                    message="object storage did not return a document size",
                    status_code=503,
                )
            return ObjectMetadata(size_bytes=value.size, etag=value.etag)
        except S3Error as error:
            status = 404 if error.code in {"NoSuchKey", "NoSuchObject"} else 503
            raise ContractOpsError(
                code="uploaded_object_not_found" if status == 404 else "object_store_unavailable",
                message=(
                    "uploaded object was not found"
                    if status == 404
                    else "object storage is unavailable"
                ),
                status_code=status,
            ) from error

    def get_bytes(self, object_key: str) -> bytes:
        try:
            response = self._client.get_object(self._bucket, object_key)
            try:
                buffer = BytesIO()
                for part in response.stream(amt=1024 * 1024):
                    buffer.write(part)
                return buffer.getvalue()
            finally:
                response.close()
                response.release_conn()
        except S3Error as error:
            raise ContractOpsError(
                code="object_store_unavailable",
                message="the document object could not be read",
                status_code=503,
            ) from error
