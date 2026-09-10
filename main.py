"""FastAPI 앱 진입점. `uvicorn main:app --reload`로 로컬 실행."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routes_final_xy import router as final_xy_router
from api.routes_focal_curve import router as focal_curve_router
from api.routes_history import router as history_router
from api.routes_id_dump import router as id_dump_router
from api.routes_spec_check import router as spec_check_router
from clients.asml_api_client import get_asml_api_client
from clients.embedding_client import get_embedding_client
from clients.llm_client import get_llm_client
from config import get_settings
from db.repo import create_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.pool = await create_pool(settings)
    app.state.embedding_client = get_embedding_client(settings)
    app.state.llm_client = get_llm_client(settings)
    app.state.asml_api_client = get_asml_api_client(settings)
    try:
        yield
    finally:
        if app.state.asml_api_client is not None:
            await app.state.asml_api_client.aclose()
        await app.state.embedding_client.aclose()
        await app.state.llm_client.aclose()
        await app.state.pool.close()


app = FastAPI(title="ASML 설비 로그 RAG 시스템", lifespan=lifespan)
app.include_router(history_router)
app.include_router(spec_check_router)
app.include_router(focal_curve_router)
app.include_router(final_xy_router)
app.include_router(id_dump_router)
