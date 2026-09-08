-- ============================================================================
-- ASML 설비 로그 RAG 시스템 — PostgreSQL(+pgvector) 스키마
-- Phase 1 (Session 1): logs_raw / log_chunks / spec_evaluations / judgements
--                       4개 테이블 전체 DDL.
-- 모든 문장은 재실행 가능(idempotent)하도록 IF NOT EXISTS 등을 사용한다.
--
-- 적용 방법: 이 파일의 DDL은 사람이 마이그레이션 도구(예: psql -f, Alembic,
-- Flyway 등)를 통해 직접 적용한다. 에이전트가 Bash로 실제 DB에 ALTER/DROP/
-- TRUNCATE를 실행하는 것은 금지되어 있으며, 이 파일에는 SQL 작성만 한다.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- 1. logs_raw — 부서 ETL이 적재하는 원본 로그
--
-- [TBD] 부서 확인 필요(00-requirements.md §11). 실제 운영 환경의 logs_raw는
-- 부서 ETL이 소유하는 테이블이며 정확한 컬럼명/타입이 아직 미확정이다.
-- 아래 CREATE TABLE은 로컬 개발/테스트 및 계약 문서화 목적의 추정 스키마다.
-- 실제 배포 시에는 이미 존재하는 부서 테이블에 embedded_at 컬럼만 추가하면
-- 된다. 컬럼명이 다르면 workers/embedding_sync_poller.py의 SELECT 매핑을
-- 조정해야 한다.
-- ============================================================================

CREATE TABLE IF NOT EXISTS logs_raw (
    log_id       BIGSERIAL PRIMARY KEY,
    equipment_id TEXT NOT NULL,
    event_time   TIMESTAMPTZ NOT NULL,
    error_code   TEXT,
    log_level    TEXT,
    message      TEXT,
    raw_json     JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- embedded_at: UC1 임베딩 동기화 워커(embedding_sync_poller.py)의 polling
-- 커서. NULL이면 미임베딩 상태. 실제 부서 테이블에는 이 컬럼만 추가하면 됨.
ALTER TABLE logs_raw ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;

-- 미임베딩 row를 빠르게 찾기 위한 부분 인덱스(polling 쿼리 최적화)
CREATE INDEX IF NOT EXISTS idx_logs_raw_unembedded
    ON logs_raw (event_time) WHERE embedded_at IS NULL;

-- 설비별 시계열 조회 최적화
CREATE INDEX IF NOT EXISTS idx_logs_raw_equipment_time
    ON logs_raw (equipment_id, event_time);

-- ============================================================================
-- 2. log_chunks — 임베딩된 로그 청크 (벡터 검색 대상)
--
-- log_ids는 logs_raw.log_id 배열을 참조하지만 FK 제약을 걸지 않는다.
-- 이유: logs_raw가 실제 배포 시 부서 소유 테이블일 수 있어(위 TBD 참고),
-- 우리 시스템이 그 테이블의 스키마/제약을 통제할 수 없기 때문이다.
-- 배열 컬럼에는 표준 FK 제약도 걸 수 없다(PostgreSQL 미지원).
-- ============================================================================

CREATE TABLE IF NOT EXISTS log_chunks (
    chunk_id     BIGSERIAL PRIMARY KEY,
    equipment_id TEXT NOT NULL,
    chunk_text   TEXT NOT NULL,
    embedding    vector(1024) NOT NULL,
    log_ids      BIGINT[] NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end   TIMESTAMPTZ NOT NULL,
    error_codes  TEXT[] NOT NULL DEFAULT '{}',
    -- feature_type: 검색 스코프 분리용 기능 구분자.
    -- 사용 값(FEATURE_REGISTRY, 01-architecture.md 기준): 'log_general'(UC1
    -- 일반 이력 검색 기본값), 'focal_curve', 'final_xy', 'id_dump'.
    -- 다른 테이블(spec_evaluations, judgements)에도 동일 이름의 값이
    -- 존재하지만(예: 'focal_curve'), 이는 세 테이블이 같은 기능 taxonomy를
    -- 공유하도록 의도된 설계이며 컬럼/테이블이 분리되어 있어 필터링 충돌은
    -- 없다.
    feature_type TEXT NOT NULL DEFAULT 'log_general',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_log_chunks_feature ON log_chunks (feature_type);
CREATE INDEX IF NOT EXISTS idx_log_chunks_equipment ON log_chunks (equipment_id);

-- 벡터 컬럼 HNSW 인덱스 (cosine distance) — 하이브리드 검색의 벡터 유사도
-- 검색 성능을 위해 필수.
CREATE INDEX IF NOT EXISTS idx_log_chunks_embedding_hnsw
    ON log_chunks USING hnsw (embedding vector_cosine_ops);

-- ============================================================================
-- 3. spec_evaluations — UC2 spec in/out 판정 이력
--
-- Phase 1(Session 1)에서는 테이블 골격만 생성한다. 결정론적 판정 로직은
-- rag/spec_evaluator.py(Session 9)에서 구현하고, feature_type/metrics_json
-- 컬럼은 로드맵상 Session 13에서 추가되는 항목이므로 별도 ALTER문으로
-- 표현한다(실행 시점이 다르더라도 이 파일 하나로 전체 이력을 재구성 가능).
-- ============================================================================

CREATE TABLE IF NOT EXISTS spec_evaluations (
    eval_id         BIGSERIAL PRIMARY KEY,
    equipment_id    TEXT NOT NULL,
    parameter       TEXT NOT NULL,
    measured_value  DOUBLE PRECISION,
    unit            TEXT,
    lsl             DOUBLE PRECISION,
    usl             DOUBLE PRECISION,
    target          DOUBLE PRECISION,
    measured_at     TIMESTAMPTZ,
    determination   TEXT NOT NULL,
    margin_pct      DOUBLE PRECISION,
    raw_data_json   JSONB,
    raw_spec_json   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_spec_eval_equipment_param
    ON spec_evaluations (equipment_id, parameter, measured_at DESC);

-- feature_type: 'generic'(기본, 단일 파라미터 spec 판정) | 'focal_curve' |
-- 'final_xy'. log_chunks.feature_type의 'log_general'/'id_dump' 값과는
-- 겹치지 않으며, 겹치는 'focal_curve'/'final_xy' 값은 두 테이블이 같은
-- 기능을 가리키도록 의도된 것이라 필터링 충돌이 없다(00-architecture.md
-- FEATURE_REGISTRY 참고).
ALTER TABLE spec_evaluations ADD COLUMN IF NOT EXISTS feature_type TEXT NOT NULL DEFAULT 'generic';

-- metrics_json: focal_curve/final_xy 등 evaluator가 계산한 부가 통계
-- (range/sigma/DOF, mean±3σ 등)를 저장하는 확장 컬럼.
ALTER TABLE spec_evaluations ADD COLUMN IF NOT EXISTS metrics_json JSONB;

-- determination 값은 00-requirements.md §10 인수 기준에 명시된 두 값만
-- 사용하는 통제 어휘(controlled vocabulary)이므로, 오탈자/오입력으로 인한
-- 판정값 오염을 막기 위해 CHECK 제약을 추가한다. ADD CONSTRAINT는
-- IF NOT EXISTS 문법이 없으므로 pg_constraint 카탈로그를 확인하는 DO 블록으로
-- 재실행 가능하게 작성한다.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_spec_evaluations_determination'
    ) THEN
        ALTER TABLE spec_evaluations
            ADD CONSTRAINT chk_spec_evaluations_determination
            CHECK (determination IN ('IN_SPEC', 'OUT_OF_SPEC'));
    END IF;
END $$;

-- ============================================================================
-- 4. judgements — UC1/UC2 공통 LLM 설명·결론 이력
--
-- use_case: 'history'(UC1) | 'spec_check'(UC2) | 'root_cause'(UC1의 id_dump
-- 등 원인 분석 하위 유형). feature_type은 nullable이며 값 존재 시
-- 'focal_curve' | 'final_xy' | 'id_dump' 등 log_chunks/spec_evaluations와
-- 동일 taxonomy를 공유한다(충돌 아님, 의도된 공유).
-- eval_id는 UC2(spec_check)에서 spec_evaluations와 연계할 때만 사용하고,
-- UC1(history/root_cause)에서는 NULL이다.
-- ============================================================================

CREATE TABLE IF NOT EXISTS judgements (
    judgement_id         BIGSERIAL PRIMARY KEY,
    use_case             TEXT NOT NULL,
    feature_type         TEXT,
    equipment_id         TEXT,
    eval_id              BIGINT REFERENCES spec_evaluations (eval_id),
    query_text           TEXT,
    retrieved_chunk_ids  BIGINT[] NOT NULL DEFAULT '{}',
    conclusion           TEXT,
    confidence           DOUBLE PRECISION,
    recommended_action   TEXT,
    raw_response         JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_judgements_equipment ON judgements (equipment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_judgements_feature ON judgements (feature_type);
