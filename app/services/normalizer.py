from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.schemas.reading import (
    DEFAULT_RULE_SET_ID,
    DEFAULT_TIMEZONE,
    GenerateReadingRequest,
    NormalizedBirthInput,
)

logger = logging.getLogger(__name__)


def normalize_generate_reading_input(
    payload: GenerateReadingRequest,
    *,
    request_id: str,
) -> NormalizedBirthInput:
    full_name = " ".join(payload.full_name.split())

    if payload.calendar_type == "solar" and payload.is_leap_lunar_month is True:
        logger.warning(
            "solar_calendar_leap_flag_overridden_to_false calendar_type=solar request_id=%s birth_year=%s ts=%s",
            request_id,
            payload.birth_year,
            datetime.now(timezone.utc).isoformat(),
        )

    is_leap_lunar_month = (
        False
        if payload.calendar_type == "solar"
        else bool(payload.is_leap_lunar_month)
    )

    return NormalizedBirthInput(
        full_name=full_name,
        birth_day=payload.birth_day,
        birth_month=payload.birth_month,
        birth_year=payload.birth_year,
        birth_hour=payload.birth_hour,
        birth_minute=payload.birth_minute,
        gender=payload.gender,
        calendar_type=payload.calendar_type,
        is_leap_lunar_month=is_leap_lunar_month,
        timezone=DEFAULT_TIMEZONE,
        rule_set_id=DEFAULT_RULE_SET_ID,
    )
