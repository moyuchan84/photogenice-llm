"""Pydantic v2 요청/응답 스키마."""

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
    # 프론트가 이미 렌더링한 데이터를 그대로 넘기는 경로(FR-2.5). 둘 다 있을 때만 우선
    # 사용되고(rag/features.py::resolve_data_and_spec), 없으면 identifier 기반으로 조회한다.
    inline_data: dict | None = None
    inline_spec: dict | None = None


class SpecCheckResponse(BaseModel):
    eval_id: int
    determination: str
    margin_pct: float | None
    judgement_id: int | None = None
    conclusion: str | None = None
    confidence: float | None = None
    recommended_action: str | None = None
    evidence_chunk_ids: list[int] | None = None


class IdDumpRequest(BaseModel):
    equipment_id: str = Field(min_length=1)
    error_dump_text: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


class IdDumpResponse(BaseModel):
    judgement_id: int
    conclusion: str | None
    confidence: float | None
    recommended_action: str | None
    evidence_chunk_ids: list[int]


# ---------------------------------------------------------------------------
# /chat 탐색 인터페이스 (FR-7) — /query/* 와 분리된 라우터에서만 쓴다.
# ---------------------------------------------------------------------------


class ChatToolCallRef(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)


class ChatHistoryMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(default="", max_length=4000)
    # assistant 턴이 실제로 실행한 도구 호출 — 후속 질문("그럼 ROTATION_CH1은?")의
    # 설비/기능 문맥을 이어받는 데 쓴다.
    calls: list[ChatToolCallRef] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=40)
