"""Funnel env helpers for admin panel (quota + premium CTA trigger)."""

from __future__ import annotations

from typing import Any, Mapping

QUOTA_ENABLED_KEY = "FREE_DAILY_ASTROLOGY_LIMIT_ENABLED"
QUOTA_LIMIT_KEY = "FREE_DAILY_ASTROLOGY_LIMIT"
PREMIUM_TRIGGER_KEY = "PREMIUM_TRIGGER_ENABLED"
PREMIUM_COOLDOWN_KEY = "PREMIUM_COOLDOWN_TURNS"

FUNNEL_ENV_KEYS: tuple[str, ...] = (
    QUOTA_ENABLED_KEY,
    QUOTA_LIMIT_KEY,
    PREMIUM_TRIGGER_KEY,
    PREMIUM_COOLDOWN_KEY,
)

FUNNEL_FIELD_LABELS: dict[str, str] = {
    QUOTA_ENABLED_KEY: "Quota chat miễn phí (bật/tắt)",
    QUOTA_LIMIT_KEY: "Giới hạn lượt substantive / ngày",
    PREMIUM_TRIGGER_KEY: "Premium CTA trigger (bật/tắt)",
    PREMIUM_COOLDOWN_KEY: "Cooldown sau mỗi CTA (số turn)",
}


def env_flag_on(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def env_flag_str(enabled: bool) -> str:
    return "1" if enabled else "0"


def env_int(
    value: str | None,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 100,
) -> int:
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def read_funnel_settings(environ: Mapping[str, str]) -> dict[str, Any]:
    return {
        "quota_enabled": env_flag_on(environ.get(QUOTA_ENABLED_KEY)),
        "quota_limit": env_int(environ.get(QUOTA_LIMIT_KEY), 5, minimum=1, maximum=50),
        "premium_trigger_enabled": env_flag_on(environ.get(PREMIUM_TRIGGER_KEY)),
        "premium_cooldown_turns": env_int(
            environ.get(PREMIUM_COOLDOWN_KEY), 3, minimum=1, maximum=30,
        ),
    }


def funnel_settings_to_env_updates(
    *,
    quota_enabled: bool,
    quota_limit: int,
    premium_trigger_enabled: bool,
    premium_cooldown_turns: int,
) -> dict[str, str]:
    return {
        QUOTA_ENABLED_KEY: env_flag_str(quota_enabled),
        QUOTA_LIMIT_KEY: str(env_int(str(quota_limit), 5, minimum=1, maximum=50)),
        PREMIUM_TRIGGER_KEY: env_flag_str(premium_trigger_enabled),
        PREMIUM_COOLDOWN_KEY: str(
            env_int(str(premium_cooldown_turns), 3, minimum=1, maximum=30),
        ),
    }


def runtime_env_slice(runtime: Mapping[str, Any] | None) -> dict[str, str | None]:
    if not runtime:
        return {key: None for key in FUNNEL_ENV_KEYS}
    return {key: runtime.get(key) for key in FUNNEL_ENV_KEYS}


def funnel_runtime_matches_file(
    file_values: Mapping[str, str],
    runtime_values: Mapping[str, Any] | None,
) -> bool:
    runtime = runtime_env_slice(runtime_values)
    for key in FUNNEL_ENV_KEYS:
        file_raw = (file_values.get(key) or "").strip()
        runtime_raw = runtime.get(key)
        if runtime_raw is None and not file_raw:
            continue
        if str(runtime_raw or "").strip() != file_raw:
            return False
    return True
