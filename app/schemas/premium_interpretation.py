from __future__ import annotations

from pydantic import BaseModel

CURRENT_WORKFLOW_VERSION = "v1"


class InterpEvidence(BaseModel):
    fact_type: str  # "main_star" | "secondary_star" | "transformation" | "void_marker" | "trang_sinh"
    house_code: str
    star_code: str | None = None
    transformation_code: str | None = None
    marker: str | None = None   # "tuan" | "triet"
    stage: str | None = None    # trang_sinh stage name


class InterpLoop(BaseModel):
    patterns: list[str] = []
    evidence: list[InterpEvidence] = []
    conclusion: str = ""


class InterpLoop3(BaseModel):
    evidence: list[InterpEvidence] = []
    modifiers: list[str] = []
    conclusion: str = ""


class InterpSynthesis(BaseModel):
    final_conclusion: str = ""
    dominant: list[str] = []
    supporting: list[str] = []


class InterpScope(BaseModel):
    target: str
    tu_chinh: str = ""
    tam_hop: list[str] = []


class HouseInterpretation(BaseModel):
    scope: InterpScope
    loop1: InterpLoop
    loop2: InterpLoop
    loop3: InterpLoop3
    loop4: InterpLoop
    synthesis: InterpSynthesis


class InterpretationPayload(BaseModel):
    """GPT output — no metadata fields."""

    houses: dict[str, HouseInterpretation]


class PremiumInterpretationRecord(BaseModel):
    """Full DB row representation."""

    id: int | None = None
    sender_id: str
    generation_id: str
    workflow_version: str = CURRENT_WORKFLOW_VERSION
    status: str = "pending"
    attempt: int = 0
    payload: InterpretationPayload | None = None
    error_detail: str | None = None
