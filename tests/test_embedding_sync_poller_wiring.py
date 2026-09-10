"""workers/embedding_sync_poller.py — feature_type 분류 키워드 주입 배선 테스트.

이 배선이 빠지면 모든 청크가 log_general로 적재되어(chunker 기본값)
focal_curve/final_xy/id_dump 엔드포인트의 검색이 항상 0건이 된다. DB 없이 검증할 수
있는 지점이라 통합테스트(test_embedding_sync_poller_integration.py, Postgres 필요)와
별도로 고정한다.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from workers import embedding_sync_poller as poller


class _FakeConn:
    def __init__(self, records):
        self._records = records
        self.executed = []

    async def fetch(self, _sql, *_args):
        return self._records

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    @asynccontextmanager
    async def transaction(self):
        yield


class _FakePool:
    def __init__(self, records):
        self.conn = _FakeConn(records)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _record(log_id, message, error_code=None):
    return {
        "log_id": log_id,
        "equipment_id": "EQ-01",
        "event_time": datetime(2026, 9, 8, 10, log_id, tzinfo=UTC),
        "error_code": error_code,
        "log_level": "ERROR" if error_code else "INFO",
        "message": message,
    }


@pytest.fixture
def embedding_client():
    client = AsyncMock()
    client.embed = AsyncMock(side_effect=lambda texts: [[0.1] * 8 for _ in texts])
    return client


async def _cycle(pool, embedding_client):
    return await poller.run_embedding_sync_cycle(
        pool,
        embedding_client,
        batch_size=100,
        session_gap_sec=600,
        error_window_before=2,
        error_window_after=2,
    )


async def test_cycle_passes_registry_keywords_to_chunker(monkeypatch, embedding_client):
    captured = {}
    real_chunk_logs = poller.chunk_logs

    def spy(rows, **kwargs):
        captured.update(kwargs)
        return real_chunk_logs(rows, **kwargs)

    monkeypatch.setattr(poller, "chunk_logs", spy)
    pool = _FakePool([_record(1, "정상 동작")])

    await _cycle(pool, embedding_client)

    assert captured["feature_keywords"], "FEATURE_REGISTRY 키워드가 chunker로 전달되지 않았다"
    assert set(captured["feature_keywords"]) == {"focal_curve", "final_xy", "id_dump"}


async def test_focal_log_is_inserted_with_focal_curve_feature_type(embedding_client):
    """적재 SQL에 실제로 log_general이 아닌 feature_type이 들어가는지 — 이 테스트가
    Phase 6 리뷰에서 발견된 '검색 근거 영구 0건' 회귀를 직접 막는다."""
    pool = _FakePool([_record(1, "focus offset drift 감지", error_code="FOC_DRIFT")])

    await _cycle(pool, embedding_client)

    inserts = [args for sql, args in pool.conn.executed if "INSERT INTO log_chunks" in sql]
    assert len(inserts) == 1
    assert inserts[0][-1] == "focal_curve"


async def test_unrelated_log_still_defaults_to_log_general(embedding_client):
    pool = _FakePool([_record(1, "wafer 반송 완료")])

    await _cycle(pool, embedding_client)

    inserts = [args for sql, args in pool.conn.executed if "INSERT INTO log_chunks" in sql]
    assert inserts[0][-1] == "log_general"
