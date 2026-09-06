"""
Storage backend — abstract interface + DiskStorage + R2Storage.
Slice 2 · V9.2 §7 / docs/ARCHITECTURE/07_storage_and_privacy.md

Select via STORAGE_BACKEND=disk|r2 (default: disk).
Signed URLs: max 24h per V9.2 §7.2.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

SIGNED_URL_MAX_SECONDS = 86400  # 24h hard cap (V9.2 §7.2)


class StorageError(Exception):
    """Raised when storage operation fails."""


class StorageBackend(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None:
        """Upload asset. Raises StorageError on failure."""

    @abstractmethod
    def get_signed_url(self, key: str, expires_in_seconds: int = SIGNED_URL_MAX_SECONDS) -> str:
        """Return a signed/temporary URL. expires_in_seconds capped at 24h."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete asset. Silent if key does not exist."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return True if key exists in storage."""

    def _cap_expiry(self, seconds: int) -> int:
        return min(seconds, SIGNED_URL_MAX_SECONDS)


class DiskStorage(StorageBackend):
    """
    Local filesystem storage for dev/staging.
    Signed URLs are served via FastAPI endpoint /assets/{id}?token=...&expires=...
    """

    def __init__(self, root_path: str | None = None) -> None:
        path = root_path or (os.environ.get("STORAGE_DISK_ROOT_PATH") or "").strip()
        if not path:
            # Default to backend/storage/ relative to this file
            path = str(Path(__file__).resolve().parents[3] / "storage")
        self._root = Path(path)
        self._root.mkdir(parents=True, exist_ok=True)
        logger.info("disk_storage_init root=%s", self._root)

    def _path(self, key: str) -> Path:
        # Sanitize: only allow safe path characters
        safe = key.replace("..", "").replace("//", "/").lstrip("/")
        return self._root / safe

    def put(self, key: str, data: bytes, content_type: str) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        logger.debug("disk_storage_put key=%s size=%d", key, len(data))

    def get_signed_url(self, key: str, expires_in_seconds: int = SIGNED_URL_MAX_SECONDS) -> str:
        """
        Generate HMAC-signed URL served by FastAPI /assets/{asset_id} endpoint.
        Token = HMAC-SHA256(key:expires_at, DISK_SIGN_SECRET).
        """
        exp = int(time.time()) + self._cap_expiry(expires_in_seconds)
        secret = (os.environ.get("USER_HASH_HMAC_SECRET") or "disk-sign-default").encode()
        token = hmac.new(secret, f"{key}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        # FastAPI endpoint constructs this URL when serving
        return f"/assets/serve/{key}?token={token}&expires={exp}"

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()
            logger.debug("disk_storage_delete key=%s", key)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def read_bytes(self, key: str) -> bytes:
        """Read raw bytes — only for internal signed URL serving."""
        p = self._path(key)
        if not p.exists():
            raise StorageError(f"Key not found: {key}")
        return p.read_bytes()

    def verify_signed_url_token(self, key: str, token: str, expires: int) -> bool:
        """Verify a signed URL token for DiskStorage /assets/serve/ endpoint."""
        if time.time() > expires:
            return False
        secret = (os.environ.get("USER_HASH_HMAC_SECRET") or "disk-sign-default").encode()
        expected = hmac.new(secret, f"{key}:{expires}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(token, expected)


class R2Storage(StorageBackend):
    """
    Cloudflare R2 storage (S3-compatible) for production.
    Requires: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_ENDPOINT_URL
    """

    def __init__(self) -> None:
        self._bucket = (os.environ.get("R2_BUCKET_NAME") or "").strip()
        self._endpoint = (os.environ.get("R2_ENDPOINT_URL") or "").strip()
        if not self._bucket or not self._endpoint:
            raise StorageError(
                "R2_BUCKET_NAME and R2_ENDPOINT_URL must be set for R2Storage"
            )
        self._client = self._make_client()
        logger.info("r2_storage_init bucket=%s", self._bucket)

    def _make_client(self):
        try:
            import boto3
            from botocore.config import Config
            return boto3.client(
                "s3",
                endpoint_url=self._endpoint,
                aws_access_key_id=(os.environ.get("R2_ACCESS_KEY_ID") or "").strip(),
                aws_secret_access_key=(os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip(),
                config=Config(signature_version="s3v4"),
                region_name="auto",
            )
        except ImportError as exc:
            raise StorageError("boto3 not installed — add boto3==1.35.0 to requirements.txt") from exc

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
            logger.debug("r2_storage_put key=%s size=%d", key, len(data))
        except Exception as exc:
            logger.error("r2_storage_put_failed key=%s err=%s", key, exc)
            raise StorageError(f"R2 put failed: {exc}") from exc

    def get_signed_url(self, key: str, expires_in_seconds: int = SIGNED_URL_MAX_SECONDS) -> str:
        capped = self._cap_expiry(expires_in_seconds)
        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=capped,
            )
            return url
        except Exception as exc:
            logger.error("r2_presign_failed key=%s err=%s", key, exc)
            raise StorageError(f"R2 presign failed: {exc}") from exc

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            logger.warning("r2_delete_failed key=%s err=%s", key, exc)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    def read_bytes(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()
        except Exception as exc:
            raise StorageError(f"R2 read failed: {exc}") from exc


_backend_instance: StorageBackend | None = None


def get_storage_backend() -> StorageBackend:
    """Return the configured storage backend singleton."""
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    backend_type = (os.environ.get("STORAGE_BACKEND") or "disk").strip().lower()
    if backend_type == "r2":
        _backend_instance = R2Storage()
    else:
        _backend_instance = DiskStorage()
    return _backend_instance


def make_storage_key(asset_id: str, sender_id: str, asset_type: str, mime: str) -> str:
    """Deterministic storage key for an asset."""
    ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
    ext = ext_map.get(mime, "bin")
    # Partition by sender hash prefix to avoid hot spots
    prefix = hashlib.sha256(sender_id.encode()).hexdigest()[:4]
    return f"{prefix}/{asset_type}/{asset_id}.{ext}"
