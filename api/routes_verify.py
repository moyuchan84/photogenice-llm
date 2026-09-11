"""검증 콘솔 — 00-requirements.md의 인수 기준(§10)/FR을 로컬에서 눈으로 확인하기 위한
개발용 라우터.

설계 원칙:
- **판정 로직을 복제하지 않는다.** UI는 실제 판정 엔드포인트(/query/*)를 브라우저에서
  그대로 호출하고, 이 라우터는 (a) 스택 상태 (b) 데모 로그 시드/동기화
  (c) 판정 후 DB에 남은 감사 레코드 readback 만 제공한다. 여기서 determination이나
  margin_pct를 다시 계산하는 코드는 절대 두지 않는다(CLAUDE.md '하지 말아야 할 것').
- **쓰기는 예약 접두사 안에서만.** 시드/정리 대상은 equipment_id가
  VERIFY_EQUIPMENT_PREFIX로 시작하는 row로 한정되므로 실제 데이터에는 영향을 주지 않는다.
- 운영 배포에서 끄고 싶으면 VERIFY_UI_ENABLED=false (config.Settings.verify_ui_enabled).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

from api.deps import DbPool, EmbeddingClientDep, LLMClientDep, SettingsDep
from db.repo import fetch_chunks_by_ids, fetch_judgement, fetch_spec_evaluation
from rag.features import FEATURE_REGISTRY
from workers.embedding_sync_poller import run_embedding_sync_cycle

router = APIRouter(prefix="/verify", tags=["verify"])

VERIFY_EQUIPMENT_PREFIX = "VERIFY-"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ---------------------------------------------------------------------------
# 데모 로그 시드
#
# 4개 시나리오. feature_type은 chunker.classify_feature_type()이 메시지 어휘로
# 분류하므로(FEATURE_REGISTRY.log_keywords), 각 시나리오의 문구가 곧 분류 기대값이다.
# 시나리오 D(log_general)는 어떤 기능 키워드도 포함하지 않도록 dose/레지스트 어휘만
# 쓴다 — 하나라도 섞이면 log_general이 아닌 기능으로 분류되어 UC1 검색이 비게 된다.
# ---------------------------------------------------------------------------

_SEED_SCENARIOS: list[dict] = [
    {
        "equipment_id": f"{VERIFY_EQUIPMENT_PREFIX}ASML-01",
        "expect_feature_type": "focal_curve",
        "logs": [
            ("INFO", None, "레티클 로드 완료, 노광 시퀀스 시작"),
            ("INFO", None, "field_curve 측정 시작 (focus offset 스캔)"),
            ("WARNING", "FOC-2201", "focal curve range가 관리 상한의 80%에 도달 (DOF 여유 감소)"),
            (
                "ERROR",
                "FOC-2210",
                "focus offset 편차 초과 — depth of focus 마진 부족으로 노광 중단",
            ),
            ("INFO", None, "레벨링 센서 재캘리브레이션 수행"),
            ("INFO", None, "웨이퍼 스테이지 평탄도 재측정 완료"),
            ("INFO", None, "focus offset 재설정 후 field_curve 재측정 — range 정상 범위 복귀"),
            ("INFO", None, "노광 시퀀스 재개, 이후 3롯트 이상 없음"),
        ],
    },
    {
        "equipment_id": f"{VERIFY_EQUIPMENT_PREFIX}ASML-02",
        "expect_feature_type": "final_xy",
        "logs": [
            ("INFO", None, "alignment 마크 스캔 시작"),
            ("INFO", None, "overlay 측정 루틴 진입 (final XY 산출)"),
            ("WARNING", "OVL-3105", "overlay dx 산포 증가 — chuck 흡착압 변동 의심"),
            ("ERROR", "OVL-3120", "final_xy 오차가 스펙 한계 초과 (X축 3시그마 이탈)"),
            ("INFO", None, "chuck 진공 라인 점검 — 미세 누설 확인"),
            ("INFO", None, "chuck 클리닝 및 흡착압 재설정"),
            ("INFO", None, "alignment 재수행 후 overlay 재측정 — dx/dy 산포 정상화"),
            ("INFO", None, "layer_shift 보정값 갱신 완료"),
        ],
    },
    {
        "equipment_id": f"{VERIFY_EQUIPMENT_PREFIX}ASML-03",
        "expect_feature_type": "id_dump",
        "logs": [
            ("INFO", None, "scan_raw 수집 세션 시작"),
            ("WARNING", "DMP-4402", "detector 응답 지연 — scan_spec 타임아웃 임박"),
            ("ERROR", "DMP-4410", "ID dump 생성: detector 채널 3번 신호 손실, tdf 레코드 불완전"),
            ("INFO", None, "덤프 파일 저장 완료 (scan_raw/scan_spec 페어 불일치)"),
            ("INFO", None, "detector 케이블 커넥터 재체결 후 자체 진단 통과"),
            ("INFO", None, "scan_raw 재수집 — tdf 레코드 정상 생성 확인"),
        ],
    },
    {
        "equipment_id": f"{VERIFY_EQUIPMENT_PREFIX}ASML-04",
        "expect_feature_type": "log_general",
        "logs": [
            ("INFO", None, "노광량(dose) 모니터링 루틴 시작"),
            ("INFO", None, "레지스트 두께 측정값 기록"),
            ("WARNING", "DSE-1102", "노광량이 목표 대비 하향 편차 — 광원 출력 저하 추정"),
            ("ERROR", "DSE-1110", "dose 편차가 관리 한계를 초과해 롯트 진행 보류"),
            ("INFO", None, "광원 출력 캘리브레이션 수행, 에너지 센서 재기준화"),
            ("INFO", None, "레지스트 현상 조건 재확인 — 이상 없음"),
            ("INFO", None, "dose 재측정 결과 목표 범위 내 복귀, 롯트 진행 재개"),
        ],
    },
]

_INSERT_SEED_LOG_SQL = """
    INSERT INTO logs_raw (equipment_id, event_time, error_code, log_level, message)
    VALUES ($1, $2, $3, $4, $5)
"""


def _seed_rows() -> list[tuple[str, datetime, str | None, str, str]]:
    """시드 row를 생성한다. event_time은 60초 간격(= CHUNK_SESSION_GAP_SEC 600 미만)이라
    한 시나리오가 하나의 세션으로 묶이고, ERROR 로그 전후 윈도우로 청킹된다."""
    base = datetime.now(UTC) - timedelta(hours=6)
    rows: list[tuple[str, datetime, str | None, str, str]] = []
    for s_idx, scenario in enumerate(_SEED_SCENARIOS):
        # 시나리오끼리는 1시간씩 벌려 세션이 서로 섞이지 않게 한다.
        origin = base + timedelta(hours=s_idx)
        for l_idx, (level, code, message) in enumerate(scenario["logs"]):
            rows.append(
                (
                    scenario["equipment_id"],
                    origin + timedelta(seconds=60 * l_idx),
                    code,
                    level,
                    message,
                )
            )
    return rows


# ---------------------------------------------------------------------------
# 페이지
# ---------------------------------------------------------------------------


def _static_page(name: str) -> FileResponse:
    path = _STATIC_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"static/{name} 을 찾을 수 없습니다.")
    return FileResponse(path, media_type="text/html; charset=utf-8")


@router.get("", include_in_schema=False)
async def verify_page() -> FileResponse:
    return _static_page("verify.html")


@router.get("/chat", include_in_schema=False)
async def verify_chat_page() -> FileResponse:
    """대화형 탐색 화면. POST /chat(api/routes_chat.py, FR-7)의 스트림을 표시만 한다 —
    /query/* 는 이 화면에서도 도구 호출 방식으로 바뀌지 않는다."""
    return _static_page("chat.html")


# ---------------------------------------------------------------------------
# 상태 / 통계
# ---------------------------------------------------------------------------


def _query_routes(app: FastAPI) -> list[str]:
    """실제로 노출된 /query/* 라우트 목록(FR-5.1 검사용).

    app.routes를 직접 훑지 않는다 — 최근 FastAPI는 include_router()로 붙인 라우터를
    _IncludedRouter로 감싸 넣기 때문에 거기엔 .path도 .routes도 없고, 한 겹만 훑으면
    빈 목록이 나와 검사가 조용히 통과해 버린다. OpenAPI 스키마는 버전에 상관없이
    "실제 공개된 경로"를 그대로 담고 있으므로 이쪽을 원본으로 삼는다."""
    paths = app.openapi().get("paths", {})
    return sorted(
        f"{method.upper()} {path}"
        for path, operations in paths.items()
        if path.startswith("/query")
        for method in operations
    )


async def _db_status(pool: asyncpg.Pool) -> dict:
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            has_vector = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
            )
            tables = [
                r["tablename"]
                for r in await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                )
            ]
        return {"ok": True, "pgvector": bool(has_vector), "tables": tables}
    except Exception as exc:  # noqa: BLE001 — 상태 패널은 어떤 실패도 화면에 보여줘야 한다
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/status")
async def verify_status(
    pool: DbPool,
    embedding_client: EmbeddingClientDep,
    llm_client: LLMClientDep,
    settings: SettingsDep,
    request: Request,
    deep: bool = Query(False, description="true면 LLM에 실제 1회 질의해 응답까지 확인(느림)"),
) -> dict:
    db = await _db_status(pool)

    try:
        [probe_vector] = await embedding_client.embed(["헬스체크"])
        embedding = {
            "ok": True,
            "dim": len(probe_vector),
            "dim_matches_schema": len(probe_vector) == settings.embedding_dim,
        }
    except Exception as exc:  # noqa: BLE001
        embedding = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if deep:
        try:
            payload = await llm_client.generate_json(
                'JSON만 출력한다. 형식: {"pong": true}',
                '헬스체크. {"pong": true} 를 그대로 출력하라.',
            )
            llm = {"ok": True, "sample": payload}
        except Exception as exc:  # noqa: BLE001
            llm = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        llm = {"ok": None, "note": "deep=1로 호출하면 실제 LLM 응답까지 확인합니다."}

    query_routes = _query_routes(request.app)

    return {
        "db": db,
        "embedding": {
            "provider": settings.embedding_provider,
            "model": (
                settings.ollama_embedding_model
                if settings.embedding_provider == "ollama"
                else settings.internal_embedding_model
            ),
            "base_url": (
                settings.ollama_base_url
                if settings.embedding_provider == "ollama"
                else settings.internal_embedding_api_base
            ),
            **embedding,
        },
        "llm": {
            "provider": settings.llm_provider,
            "model": (
                settings.ollama_llm_model
                if settings.llm_provider == "ollama"
                else settings.internal_llm_model
            ),
            "base_url": (
                settings.ollama_base_url
                if settings.llm_provider == "ollama"
                else settings.internal_llm_api_base
            ),
            **llm,
        },
        "asml_api_configured": request.app.state.asml_api_client is not None,
        "tuning": {
            "margin_threshold_pct": settings.spec_check_margin_threshold_pct,
            "chunk_session_gap_sec": settings.chunk_session_gap_sec,
            "chunk_error_window_before": settings.chunk_error_window_before,
            "chunk_error_window_after": settings.chunk_error_window_after,
            "embedding_dim": settings.embedding_dim,
        },
        "registry": [
            {
                "feature_type": ft,
                "kind": cfg.kind,
                "evaluator": type(cfg.evaluator).__name__ if cfg.evaluator else None,
                "has_prompt_context": cfg.prompt_context is not None,
                "log_keywords": list(cfg.log_keywords),
            }
            for ft, cfg in FEATURE_REGISTRY.items()
        ],
        "query_routes": query_routes,
    }


@router.get("/stats")
async def verify_stats(pool: DbPool) -> dict:
    async with pool.acquire() as conn:
        logs_total = await conn.fetchval("SELECT count(*) FROM logs_raw")
        logs_unembedded = await conn.fetchval(
            "SELECT count(*) FROM logs_raw WHERE embedded_at IS NULL"
        )
        chunk_rows = await conn.fetch(
            "SELECT feature_type, count(*) AS n FROM log_chunks"
            " GROUP BY feature_type ORDER BY feature_type"
        )
        evals_total = await conn.fetchval("SELECT count(*) FROM spec_evaluations")
        judgements_total = await conn.fetchval("SELECT count(*) FROM judgements")
        seeded = await conn.fetchval(
            "SELECT count(*) FROM logs_raw WHERE equipment_id LIKE $1",
            f"{VERIFY_EQUIPMENT_PREFIX}%",
        )
    return {
        "logs_raw": {"total": logs_total, "unembedded": logs_unembedded, "seeded": seeded},
        "log_chunks": {r["feature_type"]: r["n"] for r in chunk_rows},
        "spec_evaluations": evals_total,
        "judgements": judgements_total,
    }


# ---------------------------------------------------------------------------
# 시드 / 동기화 / 정리 (모두 VERIFY_EQUIPMENT_PREFIX 범위 안에서만 동작)
# ---------------------------------------------------------------------------


@router.post("/seed")
async def verify_seed(pool: DbPool) -> dict:
    rows = _seed_rows()
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(_INSERT_SEED_LOG_SQL, rows)
    return {
        "inserted": len(rows),
        "equipment_ids": [s["equipment_id"] for s in _SEED_SCENARIOS],
        "expected_feature_types": {
            s["equipment_id"]: s["expect_feature_type"] for s in _SEED_SCENARIOS
        },
    }


@router.post("/sync")
async def verify_sync(
    pool: DbPool, embedding_client: EmbeddingClientDep, settings: SettingsDep
) -> dict:
    """실제 워커 함수(workers/embedding_sync_poller.run_embedding_sync_cycle)를 1회
    호출한다 — 검증용 사본을 만들지 않으므로 여기서 통과하면 실제 워커도 같은 경로다."""
    async with pool.acquire() as conn:
        before = await conn.fetchval("SELECT count(*) FROM log_chunks")

    processed = await run_embedding_sync_cycle(
        pool,
        embedding_client,
        batch_size=settings.embedding_sync_batch_size,
        session_gap_sec=settings.chunk_session_gap_sec,
        error_window_before=settings.chunk_error_window_before,
        error_window_after=settings.chunk_error_window_after,
    )

    async with pool.acquire() as conn:
        after = await conn.fetchval("SELECT count(*) FROM log_chunks")
        remaining = await conn.fetchval("SELECT count(*) FROM logs_raw WHERE embedded_at IS NULL")
        by_feature = await conn.fetch(
            "SELECT feature_type, count(*) AS n FROM log_chunks WHERE equipment_id LIKE $1"
            " GROUP BY feature_type ORDER BY feature_type",
            f"{VERIFY_EQUIPMENT_PREFIX}%",
        )
    return {
        "processed_rows": processed,
        "chunks_before": before,
        "chunks_after": after,
        "chunks_created": after - before,
        "unembedded_remaining": remaining,
        "seeded_chunks_by_feature": {r["feature_type"]: r["n"] for r in by_feature},
    }


@router.post("/reset")
async def verify_reset(pool: DbPool) -> dict:
    """시드 데이터와 그로부터 파생된 판정/설명 이력만 삭제한다.
    judgements -> spec_evaluations 순서(FK)를 지킨다."""
    like = f"{VERIFY_EQUIPMENT_PREFIX}%"
    async with pool.acquire() as conn, conn.transaction():
        judgements = await conn.execute("DELETE FROM judgements WHERE equipment_id LIKE $1", like)
        evals = await conn.execute("DELETE FROM spec_evaluations WHERE equipment_id LIKE $1", like)
        chunks = await conn.execute("DELETE FROM log_chunks WHERE equipment_id LIKE $1", like)
        logs = await conn.execute("DELETE FROM logs_raw WHERE equipment_id LIKE $1", like)
    return {
        "deleted": {
            "judgements": judgements,
            "spec_evaluations": evals,
            "log_chunks": chunks,
            "logs_raw": logs,
        }
    }


# ---------------------------------------------------------------------------
# 감사 추적 readback (FR-6.1)
# ---------------------------------------------------------------------------


def _jsonb(value: object) -> object:
    """asyncpg는 jsonb를 str로 돌려준다 — 화면에서 다루기 좋게 파싱한다."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


@router.get("/audit/spec-evaluation/{eval_id}")
async def verify_audit_spec_evaluation(pool: DbPool, eval_id: int) -> dict:
    record = await fetch_spec_evaluation(pool, eval_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"eval_id={eval_id} 레코드가 없습니다.")
    row = dict(record)
    for key in ("raw_data_json", "raw_spec_json", "metrics_json"):
        row[key] = _jsonb(row[key])
    return row


@router.get("/audit/judgement/{judgement_id}")
async def verify_audit_judgement(pool: DbPool, judgement_id: int) -> dict:
    record = await fetch_judgement(pool, judgement_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"judgement_id={judgement_id} 레코드가 없습니다."
        )
    row = dict(record)
    row["raw_response"] = _jsonb(row["raw_response"])
    row["retrieved_chunk_ids"] = list(row["retrieved_chunk_ids"] or [])
    return row


@router.get("/audit/chunks")
async def verify_audit_chunks(pool: DbPool, ids: str = Query("", description="쉼표 구분")) -> dict:
    chunk_ids = [int(part) for part in ids.split(",") if part.strip()]
    records = await fetch_chunks_by_ids(pool, chunk_ids)
    return {
        "chunks": [
            {**dict(r), "log_ids": list(r["log_ids"]), "error_codes": list(r["error_codes"])}
            for r in records
        ]
    }
