"""Pydantic v2 요청/응답 스키마.

SpecCheckRequest는 Phase 4(Session 11)에서 identifier 기반 조회만 지원하는 최소
형태로 추가한다. inline_data/inline_spec 지원 및 resolve_data_and_spec()는
Phase 6(Session 17)에서 이 클래스에 필드를 추가하는 방식으로 확장할 예정이다.
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


class SpecCheckRequest(BaseModel):
    equipment_id: str = Field(min_length=1)
    parameter: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


class SpecCheckResponse(BaseModel):
    eval_id: int
    determination: str
    margin_pct: float | None
    judgement_id: int | None = None
    conclusion: str | None = None
    confidence: float | None = None
    recommended_action: str | None = None
    evidence_chunk_ids: list[int] | None = None
