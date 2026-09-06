#!/usr/bin/env python3
"""
reset_test_data.py — xóa dữ liệu user/session để test lại từ đầu.

Chạy:
    cd backend
    DATABASE_URL=<url> python scripts/reset_test_data.py

Hoặc nếu đã có .env:
    cd backend
    python -c "from dotenv import load_dotenv; load_dotenv('.env')" && python scripts/reset_test_data.py
"""
from __future__ import annotations

import os
import sys

import psycopg


def main() -> None:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    # Tables to truncate (user data only — schema/config/admin tables preserved)
    TRUNCATE_TABLES = [
        "background_jobs",
        "assets",
        "readings",
        "user_memories",
        "messenger_sessions",
        "user_profiles",
        "user_daily_counters",
        "webhook_deliveries",
        "funnel_events",
        "tracking_events",
        "bot_activity_log",
        "donate_reports",
        "abuse_flags",
        "cost_ledger",
        "support_tickets",
    ]

    print("Connecting to database...")
    with psycopg.connect(db_url, autocommit=False) as conn:
        # Safety check: show DB name before nuking
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user")
            db_name, db_user = cur.fetchone()
        print(f"  Database : {db_name}")
        print(f"  User     : {db_user}")
        print()

        confirm = input(
            "Xóa toàn bộ dữ liệu user/session? Nhập 'YES' để xác nhận: "
        ).strip()
        if confirm != "YES":
            print("Hủy.")
            return

        print()
        with conn.cursor() as cur:
            for table in TRUNCATE_TABLES:
                try:
                    cur.execute(
                        f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"
                    )
                    print(f"  ✓ TRUNCATE {table}")
                except psycopg.errors.UndefinedTable:
                    print(f"  - SKIP {table} (table does not exist)")
                    conn.rollback()

        conn.commit()
        print()

        # Verify counts
        print("Kết quả sau reset:")
        with conn.cursor() as cur:
            for table in ["messenger_sessions", "readings", "assets",
                          "user_memories", "background_jobs", "webhook_deliveries"]:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    count = cur.fetchone()[0]
                    status = "✓" if count == 0 else "⚠"
                    print(f"  {status} {table}: {count} rows")
                except psycopg.errors.UndefinedTable:
                    conn.rollback()
                    print(f"  - {table}: (table not found)")

    print()
    print("Reset hoàn tất. Sẵn sàng test lại.")


if __name__ == "__main__":
    main()
