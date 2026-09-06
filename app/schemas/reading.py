from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from app.services.DEMO_rule_sets import DEMO_FULL_V2

GenderType = Literal["male", "female"]
CalendarType = Literal["solar", "lunar"]

SCHEMA_VERSION = "1.0"
DEFAULT_TIMEZONE = "Asia/Bangkok"
DEFAULT_RULE_SET_ID = DEMO_FULL_V2


class GenerateReadingRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    full_name: str = Field(..., min_length=1, max_length=100)
    birth_day: int = Field(..., ge=1, le=31)
    birth_month: int = Field(..., ge=1, le=12)
    birth_year: int = Field(..., ge=1900, le=2100)
    birth_hour: int = Field(..., ge=0, le=23)
    birth_minute: int = Field(..., ge=0, le=59)
    gender: GenderType
    calendar_type: CalendarType = "solar"
    is_leap_lunar_month: Optional[bool] = None

    @model_validator(mode="after")
    def validate_calendar_and_dates(self) -> GenerateReadingRequest:
        if self.calendar_type == "solar":
            try:
                date(self.birth_year, self.birth_month, self.birth_day)
            except ValueError as e:
                raise PydanticCustomError(
                    "invalid_gregorian_date",
                    "INVALID_GREGORIAN_DATE",
                ) from e

        if self.calendar_type == "lunar":
            if self.is_leap_lunar_month is None:
                raise PydanticCustomError(
                    "missing_is_leap_lunar_month",
                    "MISSING_IS_LEAP_LUNAR_MONTH",
                )
            # MVP: lunar months are 1–12, days typically 1–30
            if self.birth_day < 1 or self.birth_day > 30:
                raise PydanticCustomError(
                    "invalid_lunar_day",
                    "INVALID_LUNAR_DAY",
                )

        return self


class NormalizedBirthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str
    birth_day: int
    birth_month: int
    birth_year: int
    birth_hour: int
    birth_minute: int
    gender: GenderType
    calendar_type: CalendarType
    is_leap_lunar_month: bool
    timezone: str
    rule_set_id: str


class ChartJson(BaseModel):
    """Chart payload from the deterministic engine: natal core plus Full V2 enrichments."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    rule_set_id: str
    timezone: str
    input_normalized: NormalizedBirthInput
    conversion: dict[str, Any]
    chart_metadata: dict[str, Any]
    houses: list[dict[str, Any]]
    major_fortunes: list[dict[str, Any]]
    validation: dict[str, Any]
    menh: dict[str, Any] = Field(default_factory=dict)
    can_chi: dict[str, Any] = Field(default_factory=dict)
    transformations: list[dict[str, Any]] = Field(default_factory=list)
    void_markers: list[dict[str, Any]] = Field(default_factory=list)
    dignities: list[dict[str, Any]] = Field(default_factory=list)
    element_context: dict[str, Any] = Field(default_factory=dict)
    house_relations: dict[str, Any] = Field(default_factory=dict)


class GenerateReadingResponse(BaseModel):
    ok: bool
    message: str
    normalized_input: NormalizedBirthInput
    chart_json: ChartJson
    teaser: str
