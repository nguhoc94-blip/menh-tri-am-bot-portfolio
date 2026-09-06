"""
Image ingest pipeline — orchestrate download → validate → store → DB record.
Slice 2 · V9.2 §3.2 / docs/ARCHITECTURE/07_storage_and_privacy.md

Flow:
  1. Download image from Messenger URL (or accept raw bytes in debug)
  2. Validate (resolution, blur, brightness, MIME)
  3. Upload to storage backend (R2 or Disk)
  4. Insert asset record into DB
  5. Return IngestResult with asset_id (or failure details)
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from app.db import get_connection
from app.media.downloader import ImageDownloadError, download_image_from_url
from app.media.storage import StorageError, get_storage_backend, make_storage_key
from app.media.validator import validate_image

logger = logging.getLogger(__name__)

# Retention per V9.2 §7.1
_INPUT_RETENTION_DAYS = int(os.environ.get("ASSET_INPUT_RETENTION_DAYS", "7"))
_OUTPUT_RETENTION_DAYS = int(os.environ.get("ASSET_OUTPUT_RETENTION_DAYS", "30"))


@dataclass
class IngestResult:
    success: bool
    asset_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    width_px: int | None = None
    height_px: int | None = None
    storage_key: str | None = None


def _face_consent_check(
    mode: Literal["palm", "face"],
    consent_token: str | None,
) -> IngestResult | None:
    if mode == "face" and not consent_token:
        return IngestResult(
            success=False,
            error_code="CONSENT_REQUIRED",
            error_detail="Face mode requires consent before image upload",
        )
    return None


def _persist_validated_image(
    *,
    data: bytes,
    content_type: str | None,
    sender_id: str,
    session_id: str,
    mode: Literal["palm", "face"],
    consent_token: str | None,
    validation_result,
) -> IngestResult:
    asset_type = f"{mode}_input"
    asset_id = str(uuid.uuid4())
    mime = validation_result.detected_mime or content_type or "image/jpeg"
    storage_key = make_storage_key(asset_id, sender_id, asset_type, mime)

    try:
        backend = get_storage_backend()
        backend.put(storage_key, data, mime)
    except StorageError as exc:
        logger.error("asset_store_failed asset_id=%s err=%s", asset_id, exc)
        return IngestResult(
            success=False,
            error_code="STORAGE_FAILED",
            error_detail=str(exc),
        )

    expires_at = datetime.now(timezone.utc) + timedelta(days=_INPUT_RETENTION_DAYS)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO assets (
                        id, sender_id, session_id, asset_type,
                        storage_key, mime_type, file_size_bytes,
                        width_px, height_px, consent_token, mode,
                        status, expires_at, metadata
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        'active', %s, %s
                    )
                    """,
                    (
                        asset_id, sender_id, session_id, asset_type,
                        storage_key, mime, len(data),
                        validation_result.width_px, validation_result.height_px,
                        consent_token, mode,
                        expires_at,
                        json.dumps({
                            "blur_score": validation_result.blur_score,
                            "brightness": validation_result.brightness,
                        }),
                    ),
                )
        logger.info(
            "asset_ingested id=%s mode=%s size=%d w=%d h=%d",
            asset_id, mode, len(data),
            validation_result.width_px or 0, validation_result.height_px or 0,
        )
    except Exception as exc:
        logger.exception("asset_db_insert_failed asset_id=%s err=%s", asset_id, exc)
        try:
            backend.delete(storage_key)
        except Exception:
            pass
        return IngestResult(
            success=False,
            error_code="DB_INSERT_FAILED",
            error_detail=str(exc),
        )

    return IngestResult(
        success=True,
        asset_id=asset_id,
        storage_key=storage_key,
        width_px=validation_result.width_px,
        height_px=validation_result.height_px,
    )


def ingest_image_bytes(
    *,
    data: bytes,
    content_type: str | None,
    sender_id: str,
    session_id: str,
    mode: Literal["palm", "face"],
    consent_token: str | None = None,
) -> IngestResult:
    """Ingest uploaded image bytes (debug/local upload — skips Messenger download)."""
    consent_err = _face_consent_check(mode, consent_token)
    if consent_err:
        return consent_err

    if not data:
        return IngestResult(
            success=False,
            error_code="EMPTY_IMAGE",
            error_detail="Image payload is empty",
        )

    result = validate_image(data, content_type)
    if not result.passed:
        return IngestResult(
            success=False,
            error_code=result.error_code,
            error_detail=f"Validation failed: {result.reason}",
            width_px=result.width_px,
            height_px=result.height_px,
        )

    return _persist_validated_image(
        data=data,
        content_type=content_type,
        sender_id=sender_id,
        session_id=session_id,
        mode=mode,
        consent_token=consent_token,
        validation_result=result,
    )


def ingest_image(
    *,
    url: str,
    sender_id: str,
    session_id: str,
    mode: Literal["palm", "face"],
    consent_token: str | None = None,
    page_access_token: str | None = None,
) -> IngestResult:
    """
    Full ingest pipeline for a Messenger image attachment.

    Args:
        url: Messenger CDN URL for the image
        sender_id: Facebook PSID
        session_id: conversation session ID
        mode: "palm" or "face"
        consent_token: required for face mode
        page_access_token: FB page token for private CDN access

    Returns:
        IngestResult — check .success before using asset_id.
    """
    consent_err = _face_consent_check(mode, consent_token)
    if consent_err:
        return consent_err

    try:
        download = download_image_from_url(url, page_access_token=page_access_token)
    except ImageDownloadError as exc:
        return IngestResult(
            success=False,
            error_code=exc.error_code,
            error_detail=str(exc),
        )

    result = validate_image(download.data, download.content_type)
    if not result.passed:
        return IngestResult(
            success=False,
            error_code=result.error_code,
            error_detail=f"Validation failed: {result.reason}",
            width_px=result.width_px,
            height_px=result.height_px,
        )

    return _persist_validated_image(
        data=download.data,
        content_type=download.content_type,
        sender_id=sender_id,
        session_id=session_id,
        mode=mode,
        consent_token=consent_token,
        validation_result=result,
    )
