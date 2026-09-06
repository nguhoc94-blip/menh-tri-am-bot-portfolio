from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from app.db import DatabaseUnavailableError, get_connection
from app.services.messenger_state import (
    ConversationState,
    LEGACY_CHATTING_STATES,
    MessengerSession,
    new_generation_id,
)

# Flat keys that belong to birth_data in the old wizard format.
# Used for automatic migration of sessions written before the conversational bridge.
_V9_COLUMNS = (
    "session_model_calls",
    "cohort_label",
    "admin_granted_combined",
    "face_consent_given",
    "palm_asset_ids",
    "face_asset_ids",
    "image_retry_count",
    "abuse_flag_count",
    "active_mode",
)
_V9_SELECT = ", ".join(_V9_COLUMNS)
_BASE_RETURNING = "sender_id, state, data_json, updated_at, generation_id"
_FULL_RETURNING = f"{_BASE_RETURNING}, {_V9_SELECT}"

_LEGACY_BIRTH_KEYS = {
    "full_name",
    "birth_day",
    "birth_month",
    "birth_year",
    "birth_hour",
    "birth_minute",
    "gender",
    "calendar_type",
    "is_leap_lunar_month",
}


def _migrate_legacy_data(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate flat wizard data_json to the new nested format.

    Old format: {"full_name": "...", "birth_day": 1, ...}
    New format: {"birth": {...}, "history": [...], "chart": null, "reading_id": null}

    If `raw` already has the "birth" key it is assumed to be in new format.
    """
    if "birth" in raw:
        if "routing" not in raw:
            raw = {**raw, "routing": {}}
        if "order_id" not in raw:
            raw = {**raw, "order_id": None}
        return raw
    birth = {k: v for k, v in raw.items() if k in _LEGACY_BIRTH_KEYS}
    return {
        "birth": birth,
        "history": [],
        "chart": None,
        "reading_id": None,
        "order_id": None,
        "routing": {},
    }


def _serialize(session: MessengerSession) -> str:
    payload: dict[str, Any] = {
        "birth": session.birth_data,
        "history": session.history,
        "chart": session.chart_json,
        "reading_id": session.reading_id,
        "order_id": session.order_id,
        "routing": session.routing if isinstance(session.routing, dict) else {},
    }
    return json.dumps(payload, ensure_ascii=False)


def _v9_persist_enabled() -> bool:
    raw = (os.environ.get("SESSION_V9_PERSIST_ENABLED") or "0").strip().lower()
    return raw in ("1", "true", "yes")


def _jsonb_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _v9_fields_from_row(row: dict[str, Any]) -> dict[str, Any]:
    active_mode_raw = row.get("active_mode")
    active_mode: str | None = None
    if active_mode_raw is not None and str(active_mode_raw).strip():
        active_mode = str(active_mode_raw)

    cohort_raw = row.get("cohort_label")
    cohort_label = "new"
    if cohort_raw is not None and str(cohort_raw).strip():
        cohort_label = str(cohort_raw)

    return {
        "session_model_calls": int(row.get("session_model_calls") or 0),
        "cohort_label": cohort_label,
        "admin_granted_combined": bool(row.get("admin_granted_combined") or False),
        "face_consent_given": bool(row.get("face_consent_given") or False),
        "palm_asset_ids": _jsonb_string_list(row.get("palm_asset_ids")),
        "face_asset_ids": _jsonb_string_list(row.get("face_asset_ids")),
        "image_retry_count": int(row.get("image_retry_count") or 0),
        "abuse_flag_count": int(row.get("abuse_flag_count") or 0),
        "active_mode": active_mode,
    }


logger = logging.getLogger(__name__)


class DbMessengerStateStore:
    def get_or_create(self, sender_id: str) -> MessengerSession:
        session, _created = self.get_or_create_ex(sender_id)
        return session

    def get_or_create_ex(self, sender_id: str) -> tuple[MessengerSession, bool]:
        created = False
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                    SELECT {_BASE_RETURNING}, {_V9_SELECT}
                    FROM messenger_sessions
                    WHERE sender_id = %s
                    """,
                    (sender_id,),
                )
                row = cur.fetchone()
                if row is None:
                    created = True
                    gen_id = new_generation_id()
                    returning = _FULL_RETURNING if _v9_persist_enabled() else _BASE_RETURNING
                    cur.execute(
                        f"""
                        INSERT INTO messenger_sessions (sender_id, state, data_json, generation_id)
                        VALUES (%s, %s, %s::jsonb, %s)
                        RETURNING {returning}
                        """,
                        (
                            sender_id,
                            ConversationState.CHATTING.value,
                            json.dumps(
                                {
                                    "birth": {},
                                    "history": [],
                                    "chart": None,
                                    "reading_id": None,
                                    "order_id": None,
                                    "routing": {},
                                }
                            ),
                            gen_id,
                        ),
                    )
                    row = cur.fetchone()
        if row is None:
            raise DatabaseUnavailableError("Could not create session")
        return self._row_to_session(row), created

    def reset(self, sender_id: str) -> MessengerSession:
        return self._upsert(
            sender_id=sender_id,
            state=ConversationState.CHATTING,
            birth_data={},
            history=[],
            chart_json=None,
            reading_id=None,
            order_id=None,
            routing={},
            generation_id=new_generation_id(),
            session_model_calls=0,
            cohort_label="new",
            admin_granted_combined=False,
            face_consent_given=False,
            palm_asset_ids=[],
            face_asset_ids=[],
            image_retry_count=0,
            abuse_flag_count=0,
            active_mode=None,
        )

    def get_current_generation_id(self, sender_id: str) -> str | None:
        """Lightweight live read of the current generation_id for sender_id, or None if the row doesn't exist."""
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT generation_id FROM messenger_sessions WHERE sender_id = %s",
                    (sender_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        raw = row.get("generation_id")
        return str(raw) if raw is not None else None

    @staticmethod
    def read_busy_status(sender_id: str) -> dict[str, Any] | None:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT busy_owner_id, busy_started_at, busy_notice_sent,
                           busy_request_preview
                    FROM messenger_sessions
                    WHERE sender_id = %s
                    """,
                    (sender_id,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    def claim_busy(
        sender_id: str,
        owner_id: str,
        *,
        max_lifetime_seconds: int,
        request_preview: str | None = None,
    ) -> bool:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    UPDATE messenger_sessions SET
                      busy_owner_id = %s::uuid,
                      busy_started_at = NOW(),
                      busy_notice_sent = false,
                      busy_request_preview = %s
                    WHERE sender_id = %s
                      AND (
                        busy_owner_id IS NULL
                        OR busy_started_at < NOW() - (%s * interval '1 second')
                      )
                    """,
                    (owner_id, request_preview, sender_id, max_lifetime_seconds),
                )
                return cur.rowcount == 1

    @staticmethod
    def mark_busy_notice_sent(sender_id: str, owner_id: str) -> bool:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    UPDATE messenger_sessions SET busy_notice_sent = true
                    WHERE sender_id = %s
                      AND busy_owner_id = %s::uuid
                      AND busy_notice_sent = false
                    """,
                    (sender_id, owner_id),
                )
                return cur.rowcount == 1

    @staticmethod
    def clear_busy_if_owner_matches(sender_id: str, owner_id: str) -> bool:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    UPDATE messenger_sessions SET
                      busy_owner_id = NULL,
                      busy_started_at = NULL,
                      busy_notice_sent = false,
                      busy_request_preview = NULL
                    WHERE sender_id = %s AND busy_owner_id = %s::uuid
                    """,
                    (sender_id, owner_id),
                )
                return cur.rowcount == 1

    @staticmethod
    def force_clear_busy(sender_id: str) -> bool:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    UPDATE messenger_sessions SET
                      busy_owner_id = NULL,
                      busy_started_at = NULL,
                      busy_notice_sent = false,
                      busy_request_preview = NULL
                    WHERE sender_id = %s
                    """,
                    (sender_id,),
                )
                return cur.rowcount == 1

    def save_if_current_generation(
        self,
        session: MessengerSession,
        expected_generation_id: str,
    ) -> bool:
        """Compare-and-swap commit: writes state/data_json/generation_id/updated_at
        [+V9 cols when enabled] ONLY WHERE sender_id matches AND generation_id
        equals expected_generation_id.

        The SET clause writes session.generation_id (the object's own value), which may
        differ from expected_generation_id when this save represents a legitimate
        correction/bump happening under the guard scope — the row's generation_id must
        advance so that any subsequent stale commit attempt (still pinned to the old
        expected_generation_id) fails its own CAS check. Returns True if exactly one row
        was updated, False if zero rows were affected (stale).
        """
        payload = _serialize(session)
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if _v9_persist_enabled():
                    cur.execute(
                        """
                        UPDATE messenger_sessions SET
                          state = %s,
                          data_json = %s::jsonb,
                          generation_id = %s,
                          updated_at = NOW(),
                          session_model_calls = %s,
                          cohort_label = %s,
                          admin_granted_combined = %s,
                          face_consent_given = %s,
                          palm_asset_ids = %s::jsonb,
                          face_asset_ids = %s::jsonb,
                          image_retry_count = %s,
                          abuse_flag_count = %s,
                          active_mode = %s
                        WHERE sender_id = %s AND generation_id = %s
                        """,
                        (
                            session.state.value,
                            payload,
                            session.generation_id,
                            session.session_model_calls,
                            session.cohort_label,
                            session.admin_granted_combined,
                            session.face_consent_given,
                            json.dumps(session.palm_asset_ids),
                            json.dumps(session.face_asset_ids),
                            session.image_retry_count,
                            session.abuse_flag_count,
                            session.active_mode,
                            session.sender_id,
                            expected_generation_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE messenger_sessions SET
                          state = %s,
                          data_json = %s::jsonb,
                          generation_id = %s,
                          updated_at = NOW()
                        WHERE sender_id = %s AND generation_id = %s
                        """,
                        (
                            session.state.value,
                            payload,
                            session.generation_id,
                            session.sender_id,
                            expected_generation_id,
                        ),
                    )
                return cur.rowcount == 1

    @staticmethod
    def patch_support_cta_metadata_if_generation(
        sender_id: str,
        expected_generation_id: str,
        patch: dict,
    ) -> bool:
        """Atomically merge keys into data_json.routing.support_cta (generation CAS).

        Does not rewrite the full session snapshot — safe when concurrent turns
        share the same generation_id.
        """
        if not patch:
            return False
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE messenger_sessions SET
                      data_json = jsonb_set(
                        COALESCE(data_json, '{}'::jsonb),
                        '{routing,support_cta}',
                        COALESCE(data_json->'routing'->'support_cta', '{}'::jsonb)
                          || %s::jsonb,
                        true
                      ),
                      updated_at = NOW()
                    WHERE sender_id = %s AND generation_id = %s
                    """,
                    (json.dumps(patch), sender_id, expected_generation_id),
                )
                return cur.rowcount == 1

    @staticmethod
    def get_support_cta_metadata(sender_id: str) -> dict:
        """Read routing.support_cta from DB without loading full session."""
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT data_json->'routing'->'support_cta' AS support_cta
                    FROM messenger_sessions
                    WHERE sender_id = %s
                    """,
                    (sender_id,),
                )
                row = cur.fetchone()
        if not row:
            return {}
        raw = row.get("support_cta")
        return dict(raw) if isinstance(raw, dict) else {}

    @staticmethod
    def get_premium_cta_metadata(sender_id: str) -> dict:
        """Read routing.premium_cta from DB without loading full session."""
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT data_json->'routing'->'premium_cta' AS premium_cta
                    FROM messenger_sessions
                    WHERE sender_id = %s
                    """,
                    (sender_id,),
                )
                row = cur.fetchone()
        if not row:
            return {}
        raw = row.get("premium_cta")
        return dict(raw) if isinstance(raw, dict) else {}

    @staticmethod
    def patch_premium_cta_metadata_if_generation(
        sender_id: str,
        expected_generation_id: str,
        patch: dict,
        *,
        unless_cooldown_advance_id: str | None = None,
    ) -> bool:
        """Atomically merge keys into data_json.routing.premium_cta (generation CAS).

        `unless_cooldown_advance_id` adds a second predicate to the same
        statement: the update applies only when the stored
        `last_cooldown_advance_id` differs from the given id. Two concurrent
        retries of the same turn therefore cannot both decrement the cooldown —
        the dedupe check must not be a separate Python read.
        """
        if not patch:
            return False
        sql = """
            UPDATE messenger_sessions SET
              data_json = jsonb_set(
                COALESCE(data_json, '{}'::jsonb),
                '{routing,premium_cta}',
                COALESCE(data_json->'routing'->'premium_cta', '{}'::jsonb)
                  || %s::jsonb,
                true
              ),
              updated_at = NOW()
            WHERE sender_id = %s AND generation_id = %s
        """
        params: list[Any] = [json.dumps(patch), sender_id, expected_generation_id]
        if unless_cooldown_advance_id is not None:
            sql += (
                " AND COALESCE("
                "data_json->'routing'->'premium_cta'->>'last_cooldown_advance_id', ''"
                ") <> %s"
            )
            params.append(unless_cooldown_advance_id)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return cur.rowcount == 1

    @staticmethod
    def get_free_usage_metadata(sender_id: str) -> dict:
        """Read routing.free_usage from DB without loading full session."""
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT data_json->'routing'->'free_usage' AS free_usage
                    FROM messenger_sessions
                    WHERE sender_id = %s
                    """,
                    (sender_id,),
                )
                row = cur.fetchone()
        if not row:
            return {}
        raw = row.get("free_usage")
        return dict(raw) if isinstance(raw, dict) else {}

    @staticmethod
    def charge_free_usage_if_generation(
        sender_id: str,
        expected_generation_id: str,
        local_date: str,
        charge_id: str,
        limit: int,
    ) -> dict | None:
        """Atomically charge one FREE usage (generation CAS + charge_id dedupe).

        Returns the updated free_usage dict on success, or None if stale/duplicate/at-limit.
        """
        sql = """
            UPDATE messenger_sessions SET
              data_json = jsonb_set(
                COALESCE(data_json, '{}'::jsonb),
                '{routing,free_usage}',
                CASE
                  WHEN COALESCE(data_json->'routing'->'free_usage'->>'date_local', '')
                       <> %s::text
                  THEN jsonb_build_object(
                    'date_local', %s::text,
                    'used_count', 1,
                    'last_charge_id', %s::text
                  )
                  ELSE COALESCE(data_json->'routing'->'free_usage', '{}'::jsonb)
                    || jsonb_build_object(
                      'date_local', %s::text,
                      'used_count',
                        COALESCE(
                          (data_json->'routing'->'free_usage'->>'used_count')::int, 0
                        ) + 1,
                      'last_charge_id', %s::text
                    )
                END,
                true
              ),
              updated_at = NOW()
            WHERE sender_id = %s
              AND generation_id = %s
              AND COALESCE(data_json->'routing'->'free_usage'->>'last_charge_id', '')
                  <> %s::text
              AND (
                COALESCE(data_json->'routing'->'free_usage'->>'date_local', '') <> %s::text
                OR COALESCE(
                  (data_json->'routing'->'free_usage'->>'used_count')::int, 0
                ) < %s::int
              )
            RETURNING data_json->'routing'->'free_usage' AS free_usage
        """
        params = (
            local_date,
            local_date,
            charge_id,
            local_date,
            charge_id,
            sender_id,
            expected_generation_id,
            charge_id,
            local_date,
            limit,
        )
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
        if not row:
            return None
        raw = row.get("free_usage")
        return dict(raw) if isinstance(raw, dict) else None

    @staticmethod
    def get_free_usage_charge_duplicate(
        sender_id: str,
        charge_id: str,
        local_date: str,
    ) -> dict | None:
        """Read current free_usage when charge UPDATE matched 0 rows (duplicate vs at-limit)."""
        meta = DbMessengerStateStore.get_free_usage_metadata(sender_id)
        if str(meta.get("last_charge_id") or "") == charge_id:
            return meta
        stored_date = str(meta.get("date_local") or "")
        try:
            used = int(meta.get("used_count") or 0)
        except (TypeError, ValueError):
            used = 0
        if stored_date == local_date and used > 0:
            return meta
        return None

    @staticmethod
    def set_free_usage_seed(
        sender_id: str,
        used_count: int,
        local_date: str,
    ) -> dict:
        """Debug-only: seed free_usage.used_count for today's VN date."""
        used_count = max(0, int(used_count))
        sql = """
            UPDATE messenger_sessions SET
              data_json = jsonb_set(
                COALESCE(data_json, '{}'::jsonb),
                '{routing,free_usage}',
                jsonb_build_object(
                  'date_local', %s::text,
                  'used_count', %s::int,
                  'last_charge_id', COALESCE(
                    data_json->'routing'->'free_usage'->>'last_charge_id', ''
                  )
                ),
                true
              ),
              updated_at = NOW()
            WHERE sender_id = %s
            RETURNING data_json->'routing'->'free_usage' AS free_usage
        """
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (local_date, used_count, sender_id))
                row = cur.fetchone()
        if not row:
            return {}
        raw = row.get("free_usage")
        return dict(raw) if isinstance(raw, dict) else {}

    def save(self, session: MessengerSession) -> None:
        from app.services import generation_guard

        expected = generation_guard.active_expected_generation(session.sender_id)
        if expected is not None:
            ok = self.save_if_current_generation(session, expected)
            if not ok:
                from app.utils.sender_hash import hash_sender_id

                logger.info(
                    "session_save_stale_commit_skipped sender_id_hash=%s expected_generation_id=%s",
                    hash_sender_id(session.sender_id),
                    expected,
                )
                from app.workers.job_outcome import CancelledStaleError

                raise CancelledStaleError(
                    f"stale commit for sender (expected_generation_id={expected})"
                )
            return
        self._upsert(
            sender_id=session.sender_id,
            state=session.state,
            birth_data=session.birth_data,
            history=session.history,
            chart_json=session.chart_json,
            reading_id=session.reading_id,
            order_id=session.order_id,
            routing=session.routing if isinstance(session.routing, dict) else {},
            generation_id=session.generation_id,
            session_model_calls=session.session_model_calls,
            cohort_label=session.cohort_label,
            admin_granted_combined=session.admin_granted_combined,
            face_consent_given=session.face_consent_given,
            palm_asset_ids=session.palm_asset_ids,
            face_asset_ids=session.face_asset_ids,
            image_retry_count=session.image_retry_count,
            abuse_flag_count=session.abuse_flag_count,
            active_mode=session.active_mode,
        )

    def mark_cancelled(self, sender_id: str) -> MessengerSession:
        """Reset session to clean CHATTING state (replaces old CANCELLED behavior)."""
        return self.reset(sender_id)

    def _upsert(
        self,
        *,
        sender_id: str,
        state: ConversationState,
        birth_data: dict[str, Any],
        history: list[dict[str, str]],
        chart_json: dict[str, Any] | None,
        reading_id: int | None,
        order_id: int | None = None,
        routing: dict[str, Any] | None = None,
        generation_id: str | None = None,
        session_model_calls: int = 0,
        cohort_label: str = "new",
        admin_granted_combined: bool = False,
        face_consent_given: bool = False,
        palm_asset_ids: list[str] | None = None,
        face_asset_ids: list[str] | None = None,
        image_retry_count: int = 0,
        abuse_flag_count: int = 0,
        active_mode: str | None = None,
    ) -> MessengerSession:
        route = routing if routing is not None else {}
        gen_id = generation_id or new_generation_id()
        palm_ids = palm_asset_ids if palm_asset_ids is not None else []
        face_ids = face_asset_ids if face_asset_ids is not None else []
        payload = json.dumps(
            {
                "birth": birth_data,
                "history": history,
                "chart": chart_json,
                "reading_id": reading_id,
                "order_id": order_id,
                "routing": route,
            },
            ensure_ascii=False,
        )
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if _v9_persist_enabled():
                    cur.execute(
                        f"""
                        INSERT INTO messenger_sessions (
                          sender_id, state, data_json, updated_at, generation_id,
                          session_model_calls, cohort_label, admin_granted_combined,
                          face_consent_given, palm_asset_ids, face_asset_ids,
                          image_retry_count, abuse_flag_count, active_mode
                        )
                        VALUES (
                          %s, %s, %s::jsonb, NOW(), %s,
                          %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s
                        )
                        ON CONFLICT (sender_id)
                        DO UPDATE SET
                          state = EXCLUDED.state,
                          data_json = EXCLUDED.data_json,
                          generation_id = EXCLUDED.generation_id,
                          updated_at = NOW(),
                          session_model_calls = EXCLUDED.session_model_calls,
                          cohort_label = EXCLUDED.cohort_label,
                          admin_granted_combined = EXCLUDED.admin_granted_combined,
                          face_consent_given = EXCLUDED.face_consent_given,
                          palm_asset_ids = EXCLUDED.palm_asset_ids,
                          face_asset_ids = EXCLUDED.face_asset_ids,
                          image_retry_count = EXCLUDED.image_retry_count,
                          abuse_flag_count = EXCLUDED.abuse_flag_count,
                          active_mode = EXCLUDED.active_mode
                        RETURNING {_FULL_RETURNING}
                        """,
                        (
                            sender_id,
                            state.value,
                            payload,
                            gen_id,
                            session_model_calls,
                            cohort_label,
                            admin_granted_combined,
                            face_consent_given,
                            json.dumps(palm_ids),
                            json.dumps(face_ids),
                            image_retry_count,
                            abuse_flag_count,
                            active_mode,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO messenger_sessions (sender_id, state, data_json, updated_at, generation_id)
                        VALUES (%s, %s, %s::jsonb, NOW(), %s)
                        ON CONFLICT (sender_id)
                        DO UPDATE SET
                          state = EXCLUDED.state,
                          data_json = EXCLUDED.data_json,
                          generation_id = EXCLUDED.generation_id,
                          updated_at = NOW()
                        RETURNING sender_id, state, data_json, updated_at, generation_id
                        """,
                        (sender_id, state.value, payload, gen_id),
                    )
                row = cur.fetchone()

        if row is None:
            raise DatabaseUnavailableError("Could not save session")
        return self._row_to_session(row)

    def _row_to_session(self, row: dict[str, Any]) -> MessengerSession:
        raw_state = str(row["state"])
        try:
            state = ConversationState(raw_state)
        except ValueError:
            state = ConversationState.CHATTING

        # Normalize to CHATTING if this is a legacy wizard state
        if state in LEGACY_CHATTING_STATES:
            state = ConversationState.CHATTING

        data_json = row["data_json"]
        if isinstance(data_json, str):
            raw: dict[str, Any] = json.loads(data_json)
        else:
            raw = dict(data_json or {})

        # Migrate old flat format to new nested format
        data = _migrate_legacy_data(raw)

        birth_data: dict[str, Any] = data.get("birth") or {}
        history: list[dict[str, str]] = data.get("history") or []
        chart_json: dict[str, Any] | None = data.get("chart") or None
        reading_id_raw = data.get("reading_id")
        reading_id: int | None = int(reading_id_raw) if reading_id_raw is not None else None

        order_id_raw = data.get("order_id")
        order_id: int | None = int(order_id_raw) if order_id_raw is not None else None

        routing_raw = data.get("routing")
        routing: dict[str, Any] = dict(routing_raw) if isinstance(routing_raw, dict) else {}

        updated_at_raw = row["updated_at"]
        updated_at = updated_at_raw if isinstance(updated_at_raw, datetime) else datetime.now()

        generation_raw = row.get("generation_id")
        if generation_raw is None:
            generation_id = new_generation_id()
        else:
            generation_id = str(generation_raw)

        session_kwargs: dict[str, Any] = {
            "sender_id": str(row["sender_id"]),
            "state": state,
            "birth_data": birth_data,
            "history": history,
            "chart_json": chart_json,
            "reading_id": reading_id,
            "order_id": order_id,
            "routing": routing,
            "updated_at": updated_at,
            "generation_id": generation_id,
        }
        if _v9_persist_enabled():
            session_kwargs.update(_v9_fields_from_row(row))

        return MessengerSession(**session_kwargs)
