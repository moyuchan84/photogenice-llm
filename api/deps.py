"""FastAPI 의존성 주입 — DB 풀/임베딩 클라이언트/LLM 클라이언트를 app.state에서 꺼내온다.

app.state.pool/embedding_client/llm_client는 main.py의 lifespan에서 1회 생성해 채운다
(요청마다 새로 만들지 않는다).
"""

from typing import Annotated

import asyncpg
from fastapi import Depends, Request

from clients.embedding_client import EmbeddingClient
from clients.llm_client import LLMClient


def get_db_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def get_embedding_client_dep(request: Request) -> EmbeddingClient:
    return request.app.state.embedding_client


def get_llm_client_dep(request: Request) -> LLMClient:
    return request.app.state.llm_client


DbPool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embedding_client_dep)]
LLMClientDep = Annotated[LLMClient, Depends(get_llm_client_dep)]
