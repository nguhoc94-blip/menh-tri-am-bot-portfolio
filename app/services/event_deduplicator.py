"""
In-memory idempotency key store for Messenger webhook event deduplication.

Strategy: keep a bounded set of recently-seen event IDs (message mid).
When Meta retries a webhook event, the same mid is seen again and skipped.

LIMITATION — single-process / local-dev only:
- This store lives in process memory. If the server restarts, the set is cleared
  (safe: a retry after restart is unlikely to cause harm in practice).
- In a multi-process / multi-instance production deployment this will NOT work
  correctly across processes. Replace with a shared cache (Redis, DB) before
  scaling to multiple workers.
- Max size is bounded to prevent unbounded memory growth.

Typical Messenger mid format: "m_<base64-string>"
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)

_MAX_SIZE = 2000  # keep last N event ids; after this oldest are evicted


class _BoundedSet:
    """Thread-safe ordered set with a max capacity (FIFO eviction)."""

    def __init__(self, maxsize: int) -> None:
        self._data: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def add_if_absent(self, key: str) -> bool:
        """Return True if key was NOT seen before (and is now registered).
        Return False if key was already present (duplicate — should skip).
        """
        with self._lock:
            if key in self._data:
                return False
            self._data[key] = None
            if len(self._data) > self._maxsize:
                self._data.popitem(last=False)  # evict oldest
            return True


_store = _BoundedSet(maxsize=_MAX_SIZE)


def is_new_event(event_id: str) -> bool:
    """Return True if this event_id has not been seen before.
    Side-effect: registers the event_id so subsequent calls return False.
    """
    result = _store.add_if_absent(event_id)
    if not result:
        logger.info(
            "webhook_event_duplicate_skipped event_id=%s event=duplicate_skipped",
            event_id,
        )
    return result
