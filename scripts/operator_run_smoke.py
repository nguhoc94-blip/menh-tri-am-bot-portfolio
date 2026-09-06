"""
Phase 8 operator runner — local DB + automated smoke (debug chat + pytest mapping).

Does NOT deploy Render. Writes evidence paths for transcript update.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
os.environ.setdefault("GIT_SHA", "562b3d9")
os.environ.setdefault("DEBUG_CHAT_ENABLED", "1")
sys.path.insert(0, str(ROOT))

RELEASE_SHA = "562b3d9"
EVIDENCE_DIR = ROOT / "docs" / "operator_evidence"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def migration_gate() -> dict:
    from app.db import get_connection
    from app.db_init import run_migrations

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            before = [r[0] for r in cur.fetchall()]
    apply1 = run_migrations()
    apply2 = run_migrations()
    default_note = ""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            after = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT COUNT(*) FROM messenger_sessions WHERE generation_id IS NULL"
            )
            null_gen = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM messenger_sessions")
            total = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT column_default FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'messenger_sessions'
                  AND column_name = 'generation_id'
                """
            )
            col_default = cur.fetchone()[0]
            if not col_default:
                cur.execute(
                    """
                    ALTER TABLE messenger_sessions
                    ALTER COLUMN generation_id SET DEFAULT gen_random_uuid()
                    """
                )
                default_note = "applied_missing_default"
                cur.execute(
                    """
                    SELECT column_default FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'messenger_sessions'
                      AND column_name = 'generation_id'
                    """
                )
                col_default = cur.fetchone()[0]
            probe = f"op_probe_{uuid.uuid4().hex[:8]}"
            cur.execute(
                """
                INSERT INTO messenger_sessions (sender_id, state)
                VALUES (%s, 'IDLE')
                RETURNING generation_id::text
                """,
                (probe,),
            )
            new_gen = cur.fetchone()[0]
            cur.execute(
                "DELETE FROM messenger_sessions WHERE sender_id = %s", (probe,)
            )
    return {
        "before_count": len(before),
        "before_tail": before[-6:],
        "apply1": apply1,
        "apply2": apply2,
        "after_tail": after[-6:],
        "sessions_total": total,
        "generation_id_null": null_gen,
        "new_session_generation_id": new_gen,
        "generation_id_column_default": str(col_default)[:80] if col_default else None,
        "default_fix_note": default_note or None,
        "migrations_030_032_present": all(
            v in after for v in ("030", "031", "032")
        ),
    }


def db_backup_manifest() -> dict:
    from app.db import get_connection

    manifest: dict = {"timestamp": _ts(), "release_sha": RELEASE_SHA}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version")
            manifest["schema_migrations"] = [
                {"version": r[0], "applied_at": r[1].isoformat() if r[1] else None}
                for r in cur.fetchall()
            ]
            for table in (
                "messenger_sessions",
                "jobs",
                "webhook_dedupe",
                "app_config",
            ):
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    manifest[f"count_{table}"] = int(cur.fetchone()[0])
                except Exception as exc:
                    manifest[f"count_{table}"] = f"error:{exc.__class__.__name__}"
    out = EVIDENCE_DIR / f"db_backup_manifest_{RELEASE_SHA}.json"
    out.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    manifest["manifest_path"] = str(out.relative_to(ROOT))
    return manifest


def run_pytest_smoke() -> dict:
    smoke_files = [
        "tests/test_context_conflict_matrix.py",
        "tests/test_conversational_bridge.py",
        "tests/test_intent_profile_transition.py",
        "tests/test_intent_confirmation_payloads.py",
        "tests/test_subject_profile_guard.py",
        "tests/test_multimodal_state_gate.py",
        "tests/test_generation_id.py",
        "tests/test_async_terminal_flows.py",
        "tests/test_async_metadata_hardening.py",
        "tests/test_sanitizer_context.py",
        "tests/test_queue_status_dedup.py",
        "tests/test_event_dedupe_key.py",
        "tests/test_webhook_dedupe_cleanup.py",
        "tests/test_log_redact_nhip3.py",
    ]
    log_path = EVIDENCE_DIR / f"pytest_smoke_{RELEASE_SHA}.log"
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *smoke_files,
        "-q",
        "--tb=no",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log_path.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    tail = (proc.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else ""
    return {
        "exit_code": proc.returncode,
        "summary": summary,
        "log_path": str(log_path.relative_to(ROOT)),
    }


def run_full_suite() -> dict:
    log_path = EVIDENCE_DIR / f"pytest_full_{RELEASE_SHA}.log"
    cmd = [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no"]
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log_path.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    tail = (proc.stdout or "").strip().splitlines()
    return {
        "exit_code": proc.returncode,
        "summary": tail[-1] if tail else "",
        "log_path": str(log_path.relative_to(ROOT)),
    }


def privacy_unit_check() -> dict:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_async_metadata_hardening.py",
        "tests/test_log_redact_nhip3.py",
        "tests/test_sender_hash.py",
        "-q",
        "--tb=no",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    proc = subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return {
        "exit_code": proc.returncode,
        "summary": (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else "",
    }


def main() -> int:
    report = {
        "operator_run_at": _ts(),
        "release_sha": RELEASE_SHA,
        "environment": "local_operator",
        "render_api_available": bool(os.getenv("RENDER_API_KEY")),
        "database_host": "localhost",
    }
    try:
        report["db_backup"] = db_backup_manifest()
        report["migration"] = migration_gate()
        report["pytest_smoke"] = run_pytest_smoke()
        report["pytest_full"] = run_full_suite()
        report["privacy_unit"] = privacy_unit_check()
    except Exception as exc:
        report["fatal_error"] = f"{exc.__class__.__name__}: {exc}"
        out = EVIDENCE_DIR / f"operator_report_{RELEASE_SHA}.json"
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps(report, indent=2, default=str))
        return 1

    out = EVIDENCE_DIR / f"operator_report_{RELEASE_SHA}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    ok = (
        report["migration"]["generation_id_null"] == 0
        and report["migration"]["migrations_030_032_present"]
        and report["pytest_smoke"]["exit_code"] == 0
        and report["pytest_full"]["exit_code"] == 0
        and report["privacy_unit"]["exit_code"] == 0
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
