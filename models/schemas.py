"""Pydantic v2 요청/응답 스키마.

UC2(spec-check) 전용 스키마(SpecCheckRequest 등)는 Phase 4(Session 17)에서 추가한다.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class HistoryQueryRequest(BaseModel):
    query: str = Field(min_length=1, description="자연어 질의")
    equipment_id: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class HistoryQueryResponse(BaseModel):
    judgement_id: int
    conclusion: str | None
    confidence: float | None
    recommended_action: str | None
    evidence_chunk_ids: list[int]
