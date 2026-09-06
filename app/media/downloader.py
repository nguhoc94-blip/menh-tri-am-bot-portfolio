"""
Download image from Messenger CDN URL.
Slice 2 · V9.2 §3.2 — max 10 MB, timeout 30s
"""
from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_MAX_BYTES = int(os.environ.get("IMAGE_MAX_FILE_MB", "10")) * 1024 * 1024
_DOWNLOAD_TIMEOUT_SEC = 30


class ImageDownloadError(Exception):
    """Raised when image cannot be downloaded."""

    def __init__(self, reason: str, error_code: str = "DOWNLOAD_FAILED"):
        super().__init__(reason)
        self.error_code = error_code


@dataclass
class DownloadResult:
    data: bytes
    content_type: str
    size_bytes: int


def download_image_from_url(url: str, *, page_access_token: str | None = None) -> DownloadResult:
    """
    Download image bytes from a Messenger attachment URL.
    Enforces max file size and timeout per V9.2 §3.2.

    Args:
        url: Messenger CDN URL for the image attachment.
        page_access_token: Optional FB token appended as query param for private CDN URLs.

    Returns:
        DownloadResult with bytes, content_type, size.

    Raises:
        ImageDownloadError with descriptive error_code.
    """
    if not url or not url.startswith("https://"):
        raise ImageDownloadError("Invalid URL — must be HTTPS", "INVALID_URL")

    # Append access token for Messenger private CDN
    fetch_url = url
    if page_access_token and "access_token" not in url:
        sep = "&" if "?" in url else "?"
        fetch_url = f"{url}{sep}access_token={page_access_token}"

    req = urllib.request.Request(
        url=fetch_url,
        headers={"User-Agent": "MenhTriAm-Bot/1.0"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SEC) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()

            # Stream read with size guard
            chunks: list[bytes] = []
            total = 0
            chunk_size = 65536  # 64 KB
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_BYTES:
                    raise ImageDownloadError(
                        f"Image exceeds {_MAX_BYTES // 1024 // 1024} MB limit",
                        "FILE_TOO_LARGE",
                    )
                chunks.append(chunk)

            data = b"".join(chunks)
            logger.info(
                "image_downloaded size_bytes=%d content_type=%s",
                len(data), content_type,
            )
            return DownloadResult(data=data, content_type=content_type, size_bytes=len(data))

    except ImageDownloadError:
        raise
    except urllib.error.HTTPError as exc:
        logger.warning("image_download_http_error status=%s url=%s", exc.code, url[:100])
        raise ImageDownloadError(f"HTTP {exc.code}", "HTTP_ERROR") from exc
    except urllib.error.URLError as exc:
        logger.warning("image_download_url_error reason=%s", exc.reason)
        raise ImageDownloadError(str(exc.reason), "NETWORK_ERROR") from exc
    except TimeoutError as exc:
        raise ImageDownloadError("Download timed out", "TIMEOUT") from exc
    except Exception as exc:
        logger.exception("image_download_unexpected err=%s", exc)
        raise ImageDownloadError(str(exc), "DOWNLOAD_FAILED") from exc
