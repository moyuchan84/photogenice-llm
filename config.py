"""환경변수 로딩 (Pydantic v2 BaseSettings). 모든 모듈은 get_settings()를 통해서만 설정에 접근한다."""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Database ---
    database_url: str

    # --- Provider 스위치: 로컬 개발은 ollama, 사내 VM 배포 시 internal로 전환 ---
    embedding_provider: Literal["ollama", "internal"] = "ollama"
    llm_provider: Literal["ollama", "internal"] = "ollama"

    # --- Ollama (로컬) ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_embedding_model: str = "bge-m3"
    ollama_llm_model: str = "llama3"

    # --- 사내 Embedding API (BGE-M3) — TBD: 실제 엔드포인트/인증/응답 스키마 부서 확인 필요 ---
    internal_embedding_api_base: str | None = None
    internal_embedding_api_key: str | None = None
    internal_embedding_model: str = "bge-m3"
    embedding_dim: int = 1024

    # --- 사내 LLM API (Gemma4-260430) — TBD: 실제 엔드포인트/인증/JSON 강제 출력 방식 부서 확인 필요 ---
    internal_llm_api_base: str | None = None
    internal_llm_api_key: str | None = None
    internal_llm_model: str = "gemma4-260430"

    # --- ASML API (Phase 4에서 사용) ---
    asml_api_base: str | None = None
    asml_api_key: str | None = None

    # --- 임베딩 동기화 워커 / 청킹 파라미터 ---
    embedding_sync_poll_interval_sec: int = 300
    embedding_sync_batch_size: int = 200
    chunk_session_gap_sec: int = 600
    # chunk_error_window_before/after=5: Phase 5(scripts/evaluate_chunk_window.py)에서
    # 합성 이상감지->복구 시나리오로 실측 확정. 5 미만이면 복구 조치 로그가 청크에서
    # 잘려나가고, 5보다 크게 키워도 복구 커버리지는 더 늘지 않고 무관한 정상 로그만
    # 늘어 청크 크기가 커진다 (scripts/golden_set/ 참고).
    chunk_error_window_before: int = 5
    chunk_error_window_after: int = 5

    # --- Spec-check (Phase 3~4) ---
    # 10.0 -> 15.0: Phase 5(scripts/evaluate_golden_set.py) 골든셋 평가에서 실측 확정.
    # 8개 케이스(OOS 2건 + margin 12/18/30/60/85% IN_SPEC 6건)에 대해 5/10/15/20/25%
    # 후보를 스윕한 결과 15%가 게이트 정확도 100%(다른 값은 88%)로 유일하게 전부
    # 일치했다 — 자세한 수치는 scripts/golden_set/last_run_report.json 참고.
    spec_check_margin_threshold_pct: float = 15.0

    # --- HTTP 클라이언트 공통 (httpx + tenacity) ---
    embedding_http_timeout_sec: float = 30.0
    llm_http_timeout_sec: float = 60.0
    asml_http_timeout_sec: float = 30.0
    http_retry_max_attempts: int = 3

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, v: str) -> str:
        # asyncpg(직접 사용, ORM 금지)는 SQLAlchemy 스타일 접두사를 이해하지 못하므로 방어적으로 정규화.
        return v.replace("postgresql+asyncpg://", "postgresql://")

    @model_validator(mode="after")
    def _check_internal_provider_requirements(self) -> "Settings":
        if self.embedding_provider == "internal" and not (
            self.internal_embedding_api_base and self.internal_embedding_api_key
        ):
            raise ValueError(
                "EMBEDDING_PROVIDER=internal 이면 INTERNAL_EMBEDDING_API_BASE/"
                "INTERNAL_EMBEDDING_API_KEY가 필요합니다."
            )
        if self.llm_provider == "internal" and not (
            self.internal_llm_api_base and self.internal_llm_api_key
        ):
            raise ValueError(
                "LLM_PROVIDER=internal 이면 INTERNAL_LLM_API_BASE/INTERNAL_LLM_API_KEY가 필요합니다."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
