"""FastAPI 의존성 주입 — DB 풀/임베딩 클라이언트/LLM 클라이언트를 app.state에서 꺼내온다.

app.state.pool/embedding_client/llm_client는 main.py의 lifespan에서 1회 생성해 채운다
(요청마다 새로 만들지 않는다).
"""

from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, Request

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


def get_asml_api_client_dep(request: Request) -> AsmlApiClient:
    client = request.app.state.asml_api_client
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="ASML API가 설정되지 않았습니다 (ASML_API_BASE/ASML_API_KEY 확인 필요).",
        )
    return client


DbPool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embedding_client_dep)]
LLMClientDep = Annotated[LLMClient, Depends(get_llm_client_dep)]
AsmlApiClientDep = Annotated[AsmlApiClient, Depends(get_asml_api_client_dep)]
