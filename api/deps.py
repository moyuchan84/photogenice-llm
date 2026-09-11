"""FastAPI 의존성 주입 — DB 풀/임베딩 클라이언트/LLM 클라이언트를 app.state에서 꺼내온다.

app.state.pool/embedding_client/llm_client는 main.py의 lifespan에서 1회 생성해 채운다
(요청마다 새로 만들지 않는다).
"""

from typing import Annotated

import asyncpg
from fastapi import Depends, Request

from clients.asml_api_client import AsmlApiClient
from clients.embedding_client import EmbeddingClient
from clients.llm_client import LLMClient
from config import Settings


def get_db_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_embedding_client_dep(request: Request) -> EmbeddingClient:
    return request.app.state.embedding_client


def get_llm_client_dep(request: Request) -> LLMClient:
    return request.app.state.llm_client


def get_asml_api_client_dep(request: Request) -> AsmlApiClient | None:
    """None을 그대로 반환한다(즉시 503을 던지지 않음) — inline_data/inline_spec 경로는
    ASML API를 아예 호출하지 않아도 되기 때문(FR-2.5). 실제로 호출이 필요한 시점에만
    rag/features.py::resolve_data_and_spec()가 None 여부를 확인해 503을 던진다."""
    return request.app.state.asml_api_client


DbPool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embedding_client_dep)]
LLMClientDep = Annotated[LLMClient, Depends(get_llm_client_dep)]
AsmlApiClientDep = Annotated[AsmlApiClient | None, Depends(get_asml_api_client_dep)]
