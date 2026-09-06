#!/usr/bin/env python3
"""CLI: retention cleanup for webhook_dedupe (24h). Run from repo: python scripts/cleanup_webhook_dedupe.py"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.db import close_pool, init_pool
from app.services.webhook_dedupe_cleanup import run_webhook_dedupe_retention_cleanup


def main() -> None:
    init_pool()
    try:
        n = run_webhook_dedupe_retention_cleanup()
        print(f"webhook_dedupe rows deleted: {n}")
    finally:
        close_pool()


if __name__ == "__main__":
    main()
