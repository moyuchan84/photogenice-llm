# ASML 설비 로그 RAG 시스템 — 아키텍처 설계 문서 (v2)

## 0. 전제 조건 정리 (업데이트)

| 항목 | 내용 |
|---|---|
| DB | PostgreSQL + pgvector (이미 세팅 완료) |
| LLM | 사내 API로 호출하는 Gemma4-260430 |
| Embedding | 사내 API로 호출하는 BGE-M3 (차원 1024) |
| **시나리오 1 (UC1, DB 기반 판정)** | 부서 ETL이 **주기적으로 스케줄링**되어 로그 데이터를 DB에 적재. 우리 시스템은 이 DB를 **주기적으로 polling**해서 신규/미임베딩 데이터를 찾아 임베딩 후 벡터로 저장 |
| **시나리오 2 (UC2, API 기반 판정)** | ASML API는 **부서에서 이미 만들어 운영 중인 FastAPI 백엔드**이며, 우리는 이 API를 **호출(client)**해서 부서가 정의한 스키마의 JSON을 받아온다. 응답은 **① 실측 데이터(data) + ② 스펙 데이터(spec)** 두 종류가 결합된 구조이며, 이 둘을 비교해 **spec in/out 판정과 결론**을 내려야 함 |

v1과 가장 크게 달라진 지점:
1. **UC1의 임베딩 동기화는 DB polling 방식**으로 확정 (실시간 이벤트 트리거가 아니라, ETL 주기에 맞춘 배치성 polling)
2. **UC2는 "새 이벤트가 우리 쪽으로 push되는 구조"가 아니라, 우리가 ASML 백엔드 API를 호출(pull)해서 data+spec을 받아오는 구조**
3. **UC2의 핵심 판정(스펙 in/out)은 LLM이 아니라 결정론적(deterministic) 코드로 계산**하고, LLM은 그 판정 결과와 과거 유사 사례(RAG 검색)를 바탕으로 **원인 해석/결론/권고 조치**를 생성하는 역할로 분리. (LLM에게 숫자 비교까지 맡기면 환각으로 오판정할 위험이 있으므로, "판정"과 "설명"의 책임을 분리하는 것이 핵심 설계 포인트)

---

## 1. 전체 아키텍처 개요 (v2)

```
 ┌───────────────────────────┐        ┌───────────────────────────────┐
 │  부서 ETL (기존, 스케줄러)   │        │  ASML API (부서 FastAPI 백엔드)  │
 │  주기적으로 DB에 로그 적재    │        │  data + spec JSON 응답 (기존)    │
 └──────────────┬────────────┘        └───────────────┬─────────────────┘
                │ INSERT (주기적)                       │ HTTP 호출 (우리가 pull)
                ▼                                      │
 ┌────────────────────────────────────┐                │
 │     PostgreSQL (+pgvector)          │                │
 │  - logs_raw (구조화 로그)             │                │
 │  - log_chunks (임베딩)                │                │
 │  - spec_evaluations (UC2 판정이력)    │                │
 │  - judgements (UC1/UC2 공통 결론이력)  │                │
 └───────────────┬─────────────────────┘                │
                 │ SELECT (polling)                      │
                 ▼                                        │
 ┌────────────────────────────────────┐                  │
 │  Embedding Sync Worker (신규)         │                  │
 │  주기적 polling → 미임베딩 row 탐지     │                  │
 │  → 청킹 → BGE-M3 API 호출 → UPSERT    │                  │
 └───────────────┬─────────────────────┘                  │
                 │                                         │
                 ▼                                         ▼
 ┌────────────────────────────────────┐    ┌──────────────────────────────────┐
 │   UC1: /query/history                │    │   UC2: /query/spec-check           │
 │   1) 질의 임베딩                        │    │   1) ASML API 호출 → data+spec 수신 │
 │   2) 하이브리드 검색(log_chunks)        │    │   2) Spec Evaluator(결정론적 계산)   │
 │   3) LLM 판정/결론                     │    │      → IN_SPEC / OUT_OF_SPEC 확정   │
 └────────────────────────────────────┘    │   3) 판정결과+과거 유사 OOS 사례 검색   │
                                            │      (RAG, log_chunks/judgements)   │
                                            │   4) LLM이 원인/결론/권고조치 생성     │
                                            └──────────────────────────────────┘
                                                          │
                                                          ▼
                                                  spec_evaluations, judgements
                                                     저장 (감사로그)
```

### 설계 원칙 (변경 없음 + 1개 추가)
1. RAG 코어(검색+LLM 설명 생성)는 하나만 만들고 UC1/UC2가 공유한다.
2. 구조화 필터 + 벡터 검색을 함께 쓰는 하이브리드 검색을 기본으로 한다.
3. 임베딩은 비동기 + 멱등(idempotent) 워커로 처리한다.
4. 사내 LLM/임베딩 API, ASML API는 모두 어댑터(클라이언트) 레이어 뒤에 숨긴다.
5. **[신규] "판정(숫자 비교)"과 "설명(원인/결론)"의 책임을 분리한다.** 스펙 in/out처럼 정답이 명확한 계산은 절대 LLM에 맡기지 않고 결정론적 코드로 수행하며, LLM은 그 결과를 사람이 이해할 수 있게 설명하고 과거 사례와 비교해 원인 가설/권고를 제시하는 역할만 담당한다.

---

## 2. 시나리오 1 (UC1) — DB Polling 기반 임베딩 동기화

기존 ETL이 이미 주기적으로 스케줄링되어 DB에 데이터를 적재하고 있으므로, 별도의 실시간 트리거(예: DB LISTEN/NOTIFY, CDC)를 새로 구축할 필요 없이 **동일한 주기 감각으로 polling하는 워커 하나만 추가**하면 됩니다.

### 2.1 Polling 방식 설계

```sql
-- logs_raw에 임베딩 처리 여부를 추적하는 컬럼 추가
ALTER TABLE logs_raw ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_logs_raw_unembedded
    ON logs_raw (event_time) WHERE embedded_at IS NULL;
```

- Embedding Sync Worker는 **APScheduler(또는 Celery beat) cron**으로, ETL 주기와 비슷하거나 조금 더 짧은 간격(예: ETL이 10분마다 돌면 워커는 5분마다)으로 실행합니다.
- 매 실행마다 `WHERE embedded_at IS NULL ORDER BY event_time LIMIT N` 으로 미처리 row를 커서 방식으로 가져와 청킹 → BGE-M3 호출 → `log_chunks` INSERT → 원본 row의 `embedded_at`을 갱신(UPDATE)합니다.
- **왜 polling인가**: ETL 자체가 이미 배치/주기 방식이므로, 실시간 이벤트 스트림(Kafka 등)을 새로 도입하는 것은 과설계입니다. Polling + 커서 방식이면 워커가 중간에 죽어도 `embedded_at IS NULL` 조건 덕분에 재시작 시 자동으로 이어서 처리되어 멱등성이 보장됩니다.
- 처리량이 부족해지면(로그량 증가 시) 워커를 여러 개 띄우기보다, `LIMIT N`과 폴링 주기를 먼저 조정하고 그래도 부족하면 `SELECT ... FOR UPDATE SKIP LOCKED`로 워커를 수평 확장하는 순서를 권장합니다.

### 2.2 UC1 조회 흐름 (v1과 동일, 변경 없음)

사용자가 자연어로 질의 → 질의 임베딩 → `log_chunks`에서 하이브리드 검색(설비/기간/에러코드 필터 + 벡터 유사도) → LLM이 근거 chunk를 바탕으로 판정/결론 생성 → `judgements`에 저장.

---

## 3. 시나리오 2 (UC2) — API 호출 기반 Spec In/Out 판정

### 3.1 입력 데이터 구조 (부서 정의 스키마 예시)

```json
{
  "data": {
    "equipment_id": "EQ-Litho-07",
    "parameter": "overlay_x",
    "value": 4.8,
    "unit": "nm",
    "measured_at": "2026-09-08T10:22:00Z",
    "lot_id": "LOT-20260908-003",
    "recipe_id": "RCP-A12"
  },
  "spec": {
    "parameter": "overlay_x",
    "lsl": -5.0,
    "usl": 5.0,
    "target": 0.0,
    "unit": "nm"
  }
}
```

> 실제 필드명은 부서 스키마에 맞춰 조정하되, 핵심은 "measured value"와 "spec limit(lsl/usl 또는 상한/하한)"이 항상 짝을 이뤄 온다는 점입니다.

### 3.2 Spec Evaluator (신규 컴포넌트, 결정론적 로직)

```python
# app/rag/spec_evaluator.py
from dataclasses import dataclass

@dataclass
class SpecResult:
    determination: str   # "IN_SPEC" | "OUT_OF_SPEC"
    margin_pct: float    # 스펙 한계까지 남은 여유 (%), 음수면 초과

def evaluate_spec(data: dict, spec: dict) -> SpecResult:
    value = data["value"]
    lsl, usl = spec["lsl"], spec["usl"]

    if value < lsl or value > usl:
        determination = "OUT_OF_SPEC"
    else:
        determination = "IN_SPEC"

    half_range = (usl - lsl) / 2
    center = (usl + lsl) / 2

    # lsl == usl(스펙 폭 0)인 특수 케이스는 0으로 나누게 되므로 별도 처리한다.
    # 유닛테스트에서 반드시 이 경계값(half_range == 0)을 커버해야 한다.
    if half_range == 0:
        margin_pct = 100.0 if value == center else -100.0
    else:
        margin_pct = (1 - abs(value - center) / half_range) * 100

    return SpecResult(determination=determination, margin_pct=round(margin_pct, 2))
```

- 이 함수는 **LLM 호출 없이** 즉시, 결정론적으로 in/out을 판정합니다. 단위 테스트로 100% 커버리지를 요구하는 부분입니다.
- 판정 결과는 우선 `spec_evaluations` 테이블에 저장합니다.

```sql
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
    determination   TEXT NOT NULL,      -- IN_SPEC | OUT_OF_SPEC
    margin_pct      DOUBLE PRECISION,
    raw_data_json   JSONB,
    raw_spec_json   JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_spec_eval_equipment_param
    ON spec_evaluations (equipment_id, parameter, measured_at DESC);
```

### 3.3 RAG 결합 — "왜 벗어났는지" 설명이 필요한 경우에만 LLM 호출

- `determination == "IN_SPEC"`이고 margin_pct가 충분히 여유 있다면, 굳이 LLM/RAG를 호출할 필요가 없습니다 (비용 절감). 이 경우 결정론적 결과만 바로 반환합니다.
- `determination == "OUT_OF_SPEC"`이거나 margin_pct가 임계치(예: 10% 이내로 스펙에 근접) 이하일 때만 RAG 파이프라인을 태웁니다.
  1. 현재 상황을 자연어로 요약한 검색 질의 생성: `"[EQ-Litho-07] overlay_x 실측 4.8nm, 스펙 [-5,5]nm, 판정 OUT_OF_SPEC"`
  2. 이 텍스트를 BGE-M3로 임베딩 → `log_chunks`(과거 로그)와 `judgements`(과거 판정이력, 특히 같은 `equipment_id`+`parameter`의 과거 OOS 사례)에서 하이브리드 검색
  3. LLM(Gemma4-260430)에게 "결정론적 판정 결과 + 검색된 과거 유사 사례"를 함께 프롬프트로 제공 → **원인 가설, 권고 조치, 확신도**를 JSON으로 생성
  4. 최종 응답 = `{determination, margin_pct}` (결정론적, 항상 포함) + `{root_cause_hypothesis, recommended_action, confidence, evidence}` (LLM 생성, OOS일 때만 포함)

```python
async def run_spec_check(data: dict, spec: dict) -> dict:
    result = evaluate_spec(data, spec)
    await repo.save_spec_evaluation(data, spec, result)

    response = {"determination": result.determination, "margin_pct": result.margin_pct}

    if result.determination == "OUT_OF_SPEC" or result.margin_pct < 10:
        query = build_spec_query_text(data, spec, result)
        explanation = await run_rag_judgement(query=query, filters={
            "equipment_id": data["equipment_id"],
        })
        response.update(explanation)  # root_cause_hypothesis, recommended_action, confidence, evidence

    return response
```

이 구조 덕분에 **UC2도 결국 UC1과 동일한 `run_rag_judgement()` 함수를 재사용**하며, 차이는 "판정은 이미 결정론적으로 끝났고, RAG는 설명/근거 검색용으로만 쓰인다"는 점뿐입니다.

### 3.4 ASML API 호출 트리거 방식

- 기본: `/query/spec-check` 엔드포인트가 호출되면(사용자 요청 또는 상위 시스템 요청 시) 그 안에서 ASML API를 즉시 호출(pull)하여 최신 data+spec을 받아오고 바로 판정합니다.
- 만약 "특정 주기로 계속 감시하다가 이상 시 알림"이 필요하다면, 동일한 `run_spec_check()` 함수를 APScheduler 주기 작업에서 호출하도록 감싸기만 하면 됩니다 (로직 재사용, 트리거만 다름).

---

## 4. 기술 스택 (변경 없음)

Python 3.11+ / FastAPI / asyncpg / pgvector(HNSW) / APScheduler(초기) → Celery+Redis(확장 시) / Pydantic v2 / structlog

ASML API 호출용 HTTP 클라이언트는 `httpx.AsyncClient`를 사용하고, 타임아웃/재시도(예: `tenacity`)를 반드시 적용합니다 (부서 API 장애가 우리 서비스 전체를 블로킹하지 않도록).

---

## 5. 단계별 개발 프로세스 (로드맵 v2)

1. **Phase 0 — 스펙 확정**: `logs_raw` 실제 스키마, ASML API(부서 FastAPI) 엔드포인트/인증/응답 스키마(data+spec 필드명) 확인, BGE-M3/Gemma4-260430 API 스펙 확인
2. **Phase 1 — UC1 임베딩 동기화**: `embedded_at` 컬럼 추가 → Polling 워커 구현 → 소량 데이터로 검증
3. **Phase 2 — UC1 판정 서비스**: `/query/history` 구현 및 검증
4. **Phase 3 — UC2 Spec Evaluator**: `evaluate_spec()` 순수 함수 + 유닛테스트 → `spec_evaluations` 테이블 저장 로직
5. **Phase 4 — UC2 ASML API 클라이언트 + RAG 결합**: `httpx` 클라이언트 구현 → OOS일 때만 RAG 호출하는 조건부 로직 → `/query/spec-check` 엔드포인트
6. **Phase 5 — 평가/튜닝**: 과거 실제 OOS 사례로 골든셋 구성 → 원인 가설 품질 평가 → margin_pct 임계치, top_k, 프롬프트 튜닝

---

## 6. 다음 문서

- `00-requirements.md` : 요구사항 정의서 (무엇을 만드는지, FR/NFR/인수기준)
- `02-roadmap.md` : Phase 단위 로드맵 (언제/어떤 순서로 만드는지)
- `CLAUDE.md` : 클로드 코드로 구현할 때 사용할 프로젝트 가이드 (본 v2 반영)

---

## 7. 기능 확장 — Focal Curve / Final XY / ID Dump

### 7.1 개별 LLM이 아닌 "1 통합 시스템 + 1 RAG 코어 + 기능별 어댑터"

세 기능 모두 기존에 정의한 두 패턴 중 하나에 속합니다.

- **Focal Curve, Final XY → UC2 패턴** (결정론적 spec 평가 + 조건부 RAG 설명)
- **ID Dump → UC1 패턴** (항상 RAG 검색, 원인 추정이 목적이므로 평가자 없이 곧바로 검색+LLM)

세 기능을 별도 모델로 분리하지 않는 이유: (1) 판정 로직의 본질이 동일한 구조를 재사용하므로 모델을 나눌 이유가 없음 (2) 도메인 특화는 "프롬프트에 해당 기능의 도메인 지식/컨텍스트를 얹는 방식"으로 충분히 달성 가능 (3) 모델을 나누면 API 호출 비용·레이턴시가 배로 늘고, 기능 간 이력 데이터 교차 참조가 불가능해짐.

### 7.2 Feature Registry

```python
# app/rag/features.py
FEATURE_REGISTRY = {
    "focal_curve": {
        "kind": "spec_check",
        "evaluator": FocalCurveEvaluator(),      # curve range/sigma/DOF 윈도우 계산
        "prompt_context": FOCAL_CURVE_DOMAIN_NOTES,
    },
    "final_xy": {
        "kind": "spec_check",
        "evaluator": FinalXYEvaluator(),         # X/Y mean±3σ 계산
        "prompt_context": FINAL_XY_DOMAIN_NOTES,
    },
    "id_dump": {
        "kind": "root_cause",
        "evaluator": None,                       # 항상 RAG로만 처리
        "prompt_context": ID_DUMP_DOMAIN_NOTES,
    },
}
```

`kind="spec_check"`는 3.4절의 `run_spec_check()` 흐름(평가 → 조건부 RAG)을 그대로 타고, `kind="root_cause"`는 평가자 없이 곧바로 `run_rag_judgement()`를 호출합니다. `prompt_context`는 기능별 도메인 지식(예: "focal curve는 필드 중심에서의 편차가 가장자리보다 중요하다" 같은 엔지니어링 지식)을 시스템 프롬프트에 얹는 부분으로, **이 부분이 사실상 "특화"의 실체**입니다.

### 7.3 스키마 확장

```sql
ALTER TABLE spec_evaluations ADD COLUMN feature_type TEXT NOT NULL DEFAULT 'generic';
ALTER TABLE spec_evaluations ADD COLUMN metrics_json JSONB;  -- curve/배열 데이터의 계산된 통계 (mean/sigma/range/min/max 등)

ALTER TABLE log_chunks ADD COLUMN feature_type TEXT DEFAULT 'log_general';
ALTER TABLE judgements ADD COLUMN feature_type TEXT;

CREATE INDEX IF NOT EXISTS idx_log_chunks_feature ON log_chunks (feature_type);
```

- Focal Curve/Final XY는 단일값이 아니라 **배열(curve/포인트 집합)**이므로, `raw_data_json`에는 원본 배열을 그대로 저장하고 `metrics_json`에는 evaluator가 계산한 요약 통계(range, sigma, 초과 포인트 수 등)를 저장합니다.
- ID Dump 검색 시 `WHERE feature_type = 'id_dump'` 필터를 반드시 걸어, focal curve/final xy 관련 로그와 섞이지 않도록 합니다.

### 7.4 데이터 소스 유연성 (HTML 렌더링 데이터 vs DB 조회)

```python
class SpecCheckRequest(BaseModel):
    feature_type: Literal["focal_curve", "final_xy"]
    equipment_id: str
    lot_id: str | None = None
    measured_at: datetime | None = None
    inline_data: dict | None = None   # 프론트에 이미 렌더링된 데이터를 그대로 전달하는 경우
    inline_spec: dict | None = None

async def resolve_data_and_spec(req: SpecCheckRequest) -> tuple[dict, dict]:
    if req.inline_data and req.inline_spec:
        return req.inline_data, req.inline_spec
    # inline이 없으면 DB 또는 ASML API에서 identifier 기준으로 조회
    return await repo.fetch_measurement_and_spec(
        req.equipment_id, req.lot_id, req.measured_at, req.feature_type
    )
```

프론트(HTML 페이지)가 이미 계산된 데이터를 들고 있으면 그걸 그대로 넘기고, 없으면 백엔드가 identifier만으로 DB/API에서 직접 조회하도록 이중 경로를 열어둡니다.

### 7.5 ID Dump 흐름

```python
async def run_id_dump_analysis(error_dump_text: str, equipment_id: str) -> dict:
    query = build_id_dump_query(error_dump_text)  # 파싱된 에러 텍스트를 검색 질의로 정리
    return await run_rag_judgement(
        query=query,
        filters={"equipment_id": equipment_id, "feature_type": "id_dump"},
    )
```

에러 로그 파싱 결과를 그대로 질의로 임베딩 → 과거에 사람이 원인을 규명해 `judgements`에 저장해둔 유사 사례를 검색 → LLM이 "가장 유사한 과거 사례 기준으로 원인은 ~로 추정됨"을 근거(evidence chunk)와 함께 생성합니다. 이 기능은 **과거 분석 이력이 쌓일수록 정확도가 올라가는 구조**이므로, 초기에는 엔지니어가 직접 원인을 기록한 `judgements` 데이터를 시드로 채워두는 작업이 선행되어야 합니다.

---

## 8. MCP 기반 에이전트 확장 (선택 사항 — 탐색형 인터페이스)

### 8.1 왜 핵심 판정 파이프라인에는 적용하지 않는가

7절까지의 설계는 "판정(숫자 비교)은 결정론적 코드, LLM은 설명만"이라는 원칙 위에 서 있습니다. LLM이 스스로 API를 호출해 데이터를 가져오게(tool-calling/MCP) 하면, 호출 파라미터·호출 시점·결과 해석까지 모델 재량에 들어가 이 원칙이 깨집니다. 또한 사내 Gemma4-260430 API가 표준 tool-calling 포맷을 지원하는지 확인이 선행되어야 하며(내부 LLM API는 순수 completion만 지원하는 경우가 많음), 멀티턴 호출로 레이턴시/비용도 늘어납니다. 따라서 `/query/spec-check`, `/query/history`, `/query/id-dump`, `/query/focal-curve`, `/query/final-xy` 등 **판정 결과를 반환하는 엔드포인트는 기존 결정론적 파이프라인을 그대로 유지**합니다.

### 8.2 MCP가 적합한 지점 — 탐색형 대화 인터페이스

엔지니어가 판정 결과를 보고 "왜 이렇게 나왔는지 더 보여줘", "비슷한 과거 케이스 더 찾아줘", "이 설비 최근 이력도 같이 보여줘" 처럼 열린 질의를 이어가는 용도에는 MCP + tool-calling이 적합합니다. 이건 하나의 정답이 있는 "판정"이 아니라 탐색적 Q&A이기 때문입니다.

### 8.3 설계 원칙 — MCP 툴은 기존 결정론적 함수를 감싸기만 함

```python
# mcp_server/tools.py
# 모든 툴은 새 계산을 하지 않고, 이미 계산/저장된 결과 또는 검색 함수를 그대로 wrapping

async def get_spec_evaluation(equipment_id: str, feature_type: str, time_range: tuple) -> dict:
    """spec_evaluations 테이블에서 이미 계산된 판정 결과를 조회만 함 (재계산 없음)"""
    return await repo.fetch_spec_evaluations(equipment_id, feature_type, time_range)

async def search_historical_cases(query: str, feature_type: str) -> list[dict]:
    """run_rag_judgement()의 검색(retriever) 부분만 노출 — LLM 재판정 없이 근거 chunk만 반환"""
    return await retriever.hybrid_search(query, filters={"feature_type": feature_type})

async def fetch_raw_log(equipment_id: str, time_range: tuple) -> list[dict]:
    return await repo.fetch_logs_raw(equipment_id, time_range)

async def call_asml_api(equipment_id: str, parameter: str) -> dict:
    return await asml_api_client.fetch(equipment_id, parameter)
```

**에이전트 모드에서도 LLM은 숫자를 재계산하지 않습니다.** `get_spec_evaluation`이 반환하는 값은 이미 `spec_evaluator.py`가 계산해 저장해둔 결과이고, LLM은 이 결과들을 조합해 자연어로 설명/탐색하는 역할만 합니다. 즉 "판정과 설명의 분리" 원칙을 에이전트 레이어에서도 그대로 유지합니다.

### 8.4 아키텍처 위치

```
핵심(결정론적):  /query/spec-check, /query/history, /query/id-dump  ← 지금까지 설계 그대로, 변경 없음
탐색(에이전트):  /chat  ← 신규, MCP 서버의 툴들을 Gemma4-260430이 tool-calling으로 자유롭게 호출
```

`/chat` 엔드포인트는 별도 서비스(또는 같은 FastAPI 앱의 별도 라우터)로 분리해서 개발하고, 핵심 판정 엔드포인트와 배포/장애 도메인을 분리하는 것을 권장합니다 (에이전트 쪽에서 문제가 생겨도 핵심 판정 서비스에는 영향이 없도록).

### 8.5 선행 확인 사항

- 사내 Gemma4-260430 API가 OpenAI 스타일 `tools`/`function_call` 파라미터를 지원하는지, 아니면 순수 텍스트 completion만 지원하는지 확인 (미지원 시 ReAct 스타일 프롬프트로 흉내내야 하며 신뢰도가 낮아짐)
- MCP 서버 구현체(예: Python `mcp` SDK)와 사내 네트워크/인증 정책 호환 여부 확인
- 이 레이어는 우선순위상 UC1/UC2/7절 기능들이 안정화된 이후 착수하는 것을 권장 (핵심 판정 파이프라인이 먼저 신뢰도를 확보해야 그 위의 탐색 기능도 의미가 있음)
