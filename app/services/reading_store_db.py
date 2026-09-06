from __future__ import annotations

import json
from typing import Any

from psycopg.rows import dict_row

from app.db import DatabaseUnavailableError, get_connection


class ReadingStoreDb:
    def create_full_free_reading(
        self,
        *,
        sender_id: str,
        normalized_input_json: dict[str, Any],
        chart_json: dict[str, Any],
        free_teaser: str,
        full_reading: str,
        status: str,
    ) -> int:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO readings (
                        sender_id,
                        normalized_input_json,
                        chart_json,
                        free_teaser,
                        full_reading,
                        is_unlocked,
                        status,
                        updated_at
                    )
                    VALUES (%s, %s::jsonb, %s::jsonb, %s, %s, TRUE, %s, NOW())
                    RETURNING id
                    """,
                    (
                        sender_id,
                        json.dumps(normalized_input_json),
                        json.dumps(chart_json),
                        free_teaser,
                        full_reading,
                        status,
                    ),
                )
                row = cur.fetchone()

        if not row or "id" not in row:
            raise DatabaseUnavailableError("Could not create full-free reading row")
        return int(row["id"])

    def create_free_reading(
        self,
        *,
        sender_id: str,
        normalized_input_json: dict[str, Any],
        chart_json: dict[str, Any],
        free_teaser: str,
    ) -> int:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO readings (
                        sender_id,
                        normalized_input_json,
                        chart_json,
                        free_teaser,
                        is_unlocked,
                        status,
                        updated_at
                    )
                    VALUES (%s, %s::jsonb, %s::jsonb, %s, FALSE, 'free_generated', NOW())
                    RETURNING id
                    """,
                    (
                        sender_id,
                        json.dumps(normalized_input_json),
                        json.dumps(chart_json),
                        free_teaser,
                    ),
                )
                row = cur.fetchone()

        if not row or "id" not in row:
            raise DatabaseUnavailableError("Could not create reading row")
        return int(row["id"])

    def get_latest_locked_by_sender(self, sender_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            """
            SELECT * FROM readings
            WHERE sender_id = %s AND is_unlocked = FALSE
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (sender_id,),
        )

    def get_latest_by_sender(self, sender_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            """
            SELECT * FROM readings
            WHERE sender_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (sender_id,),
        )

    def set_status(
        self,
        *,
        reading_id: int,
        status: str,
        is_unlocked: bool | None = None,
        full_reading: str | None = None,
    ) -> None:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if is_unlocked is None and full_reading is None:
                    cur.execute(
                        "UPDATE readings SET status = %s, updated_at = NOW() WHERE id = %s",
                        (status, reading_id),
                    )
                elif full_reading is None:
                    cur.execute(
                        """
                        UPDATE readings
                        SET status = %s, is_unlocked = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (status, is_unlocked, reading_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE readings
                        SET status = %s, is_unlocked = %s, full_reading = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (status, is_unlocked, full_reading, reading_id),
                    )

    def _fetch_one(self, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, params)
                row = cur.fetchone()
        return dict(row) if row else None

