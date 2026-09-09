"""Phase 5(Session 12) 평가 스크립트 — 골든셋 기반 margin_pct 임계치/top_k 튜닝.

이 스크립트는 두 가지를 독립적으로 측정한다 (00-requirements.md §11 TBD 파라미터 확정 목적):

1. margin_pct 임계치 스윕 — 순수 계산(DB/LLM 불필요). 각 골든셋 케이스의
   `gold.requires_explanation`(엔지니어라면 원인 설명을 원했을 상황인지)을 정답으로 두고,
   spec_evaluator.evaluate_spec() 결과에 후보 임계치를 적용했을 때의 게이트 판단
   (OUT_OF_SPEC 이거나 margin_pct <= threshold)이 얼마나 일치하는지를 정확도/과호출율
   (불필요한 LLM 호출)/누락율(설명이 필요했는데 게이트가 막음)로 집계한다.
2. top_k 스윕 — `gold.requires_explanation=true`인 케이스만 대상으로 실제
   run_rag_judgement()를 호출해(로컬 Ollama bge-m3/llama3 사용) 근거 chunk 적중률과
   결론 키워드 일치율을 측정한다.

판정(OUT_OF_SPEC 여부)은 항상 rag/spec_evaluator.py의 순수 함수로만 계산하고, 이
스크립트는 그 결과에 임계치를 적용해 게이트 정확도를 "측정"만 한다 — LLM에게 판정을
맡기지 않는다는 원칙(CLAUDE.md)은 여기서도 유지된다.

사용법:
    python -m scripts.evaluate_golden_set
    python -m scripts.evaluate_golden_set --thresholds 5,10,15,20,25 --top-ks 2,4,6,8
    python -m scripts.evaluate_golden_set --keep-data   # 실행 후 삽입한 log_chunks/judgements 유지(디버깅용)

실제 Postgres(docker-compose) + Ollama(bge-m3/llama3, 또는 EMBEDDING_PROVIDER/LLM_PROVIDER=internal)가
필요하다. 이 스크립트가 삽입한 row만 equipment_id로 추적해 종료 시 정리하므로 기존 데이터에는
영향을 주지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from clients.embedding_client import get_embedding_client
from clients.llm_client import get_llm_client
from config import get_settings
from db.repo import create_pool
from rag.core import run_rag_judgement
from rag.spec_evaluator import SpecResult, evaluate_spec

logger = logging.getLogger("evaluate_golden_set")

_DEFAULT_GOLDEN_SET = Path(__file__).parent / "golden_set" / "spec_check_cases.json"
_DEFAULT_REPORT_PATH = Path(__file__).parent / "golden_set" / "last_run_report.json"

_INSERT_CHUNK_SQL = """
    INSERT INTO log_chunks
        (equipment_id, chunk_text, embedding, log_ids, period_start, period_end, error_codes, feature_type)
    VALUES ($1, $2, $3, $4, $5, $6, $7, 'log_general')
    RETURNING chunk_id
"""


# ---------------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedChunk:
    days_ago: int
    relevant: bool
    error_codes: list[str]
    chunk_text: str


@dataclass(frozen=True)
class GoldenCase:
    case_id: str
    equipment_id: str
    parameter: str
    unit: str
    measured_value: float
    spec: dict
    requires_explanation: bool
    expected_error_codes: list[str]
    expected_keywords: list[str]
    seed_chunks: list[SeedChunk] = field(default_factory=list)


def load_golden_set(path: Path) -> list[GoldenCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for c in payload["cases"]:
        cases.append(
            GoldenCase(
                case_id=c["case_id"],
                equipment_id=c["equipment_id"],
                parameter=c["parameter"],
                unit=c["unit"],
                measured_value=c["measured_value"],
                spec=c["spec"],
                requires_explanation=c["gold"]["requires_explanation"],
                expected_error_codes=c["expected_error_codes"],
                expected_keywords=c["expected_keywords"],
                seed_chunks=[SeedChunk(**sc) for sc in c["seed_chunks"]],
            )
        )
    return cases


# ---------------------------------------------------------------------------
# 순수 함수 (DB/LLM 불필요) — 유닛테스트 대상
# ---------------------------------------------------------------------------


def gate_decision(result: SpecResult, threshold: float) -> bool:
    """spec_evaluator 결과에 margin_pct 임계치를 적용한 RAG 호출 게이트 판단.

    api/routes_spec_check.py의 조건 로직과 동일해야 하며, 여기서 별도로 재구현하지
    않고 동일한 불리언 식을 그대로 사용한다 — 로직 분기가 둘로 갈라지지 않도록.
    """
    return result.determination == "OUT_OF_SPEC" or result.margin_pct <= threshold


def keyword_score(text: str, expected_keywords: list[str]) -> float:
    """text에 expected_keywords가 (대소문자 무시, 부분 문자열로) 몇 % 포함되는지."""
    if not expected_keywords:
        return 1.0
    haystack = text.lower()
    hits = sum(1 for kw in expected_keywords if kw.lower() in haystack)
    return hits / len(expected_keywords)


@dataclass(frozen=True)
class GateSweepResult:
    threshold: float
    accuracy: float
    unnecessary_rag_rate: float  # gold=False인데 게이트가 True로 판단(불필요한 LLM 호출)
    missed_explanation_rate: float  # gold=True인데 게이트가 False로 판단(설명 누락)
    wrong_case_ids: list[str]


def sweep_gate_thresholds(
    cases: list[GoldenCase], spec_results: dict[str, SpecResult], thresholds: list[float]
) -> list[GateSweepResult]:
    gold_true = [c for c in cases if c.requires_explanation]
    gold_false = [c for c in cases if not c.requires_explanation]

    results = []
    for threshold in thresholds:
        wrong = []
        for c in cases:
            predicted = gate_decision(spec_results[c.case_id], threshold)
            if predicted != c.requires_explanation:
                wrong.append(c.case_id)

        unnecessary = sum(
            1 for c in gold_false if gate_decision(spec_results[c.case_id], threshold)
        )
        missed = sum(1 for c in gold_true if not gate_decision(spec_results[c.case_id], threshold))
        results.append(
            GateSweepResult(
                threshold=threshold,
                accuracy=(len(cases) - len(wrong)) / len(cases),
                unnecessary_rag_rate=unnecessary / len(gold_false) if gold_false else 0.0,
                missed_explanation_rate=missed / len(gold_true) if gold_true else 0.0,
                wrong_case_ids=wrong,
            )
        )
    return results


def recommend_threshold(sweep: list[GateSweepResult]) -> GateSweepResult:
    """정확도 우선, 동률이면 '설명 누락'을 '불필요한 호출'보다 더 나쁘게 취급해 그 다음
    기준으로 삼는다 — 놓친 원인 분석이 낭비된 LLM 호출보다 비용이 크다는 판단."""
    return min(
        sweep, key=lambda r: (-r.accuracy, r.missed_explanation_rate, r.unnecessary_rag_rate)
    )


@dataclass(frozen=True)
class TopKSweepResult:
    top_k: int
    mean_evidence_hit_rate: float
    mean_keyword_score: float
    mean_confidence: float
    per_case: dict[str, dict]


def aggregate_topk_results(top_k: int, per_case: dict[str, dict]) -> TopKSweepResult:
    n = len(per_case) or 1
    return TopKSweepResult(
        top_k=top_k,
        mean_evidence_hit_rate=sum(v["evidence_hit"] for v in per_case.values()) / n,
        mean_keyword_score=sum(v["keyword_score"] for v in per_case.values()) / n,
        mean_confidence=sum(v["confidence"] or 0.0 for v in per_case.values()) / n,
        per_case=per_case,
    )


def recommend_top_k(sweep: list[TopKSweepResult]) -> TopKSweepResult:
    """근거 적중률을 최우선으로 하고, 동률이면 키워드 일치율, 다시 동률이면 프롬프트
    비용(더 작은 top_k)을 우선한다."""
    return max(sweep, key=lambda r: (r.mean_evidence_hit_rate, r.mean_keyword_score, -r.top_k))


# ---------------------------------------------------------------------------
# DB/LLM 연동 (Ollama 로컬 스택 또는 internal 프로바이더 사용)
# ---------------------------------------------------------------------------


async def _seed_case_chunks(
    pool, embedding_client, case: GoldenCase, base_time: datetime
) -> list[int]:
    chunk_ids = []
    for sc in case.seed_chunks:
        [embedding] = await embedding_client.embed([sc.chunk_text])
        period_start = base_time - timedelta(days=sc.days_ago)
        period_end = period_start + timedelta(minutes=45)
        async with pool.acquire() as conn:
            chunk_id = await conn.fetchval(
                _INSERT_CHUNK_SQL,
                case.equipment_id,
                sc.chunk_text,
                embedding,
                [],
                period_start,
                period_end,
                sc.error_codes,
            )
        chunk_ids.append(chunk_id)
    return chunk_ids


def _build_query(case: GoldenCase, result: SpecResult) -> str:
    return (
        f"{case.equipment_id}의 {case.parameter} 측정값이 spec을 벗어났거나 근접했습니다 "
        f"(measured={case.measured_value}, lsl={case.spec.get('lsl')}, usl={case.spec.get('usl')}, "
        f"determination={result.determination}, margin_pct={result.margin_pct}%). "
        "과거 유사 사례를 참고해 원인과 권고 조치를 설명해 주세요."
    )


async def run_topk_sweep(
    pool, embedding_client, llm_client, rag_cases: list[GoldenCase], top_ks: list[int]
) -> list[TopKSweepResult]:
    sweep = []
    for top_k in top_ks:
        per_case = {}
        for case in rag_cases:
            spec_result = evaluate_spec({"value": case.measured_value}, case.spec)
            rag_result = await run_rag_judgement(
                pool,
                embedding_client,
                llm_client,
                use_case="spec_check_eval",
                query=_build_query(case, spec_result),
                equipment_id=case.equipment_id,
                top_k=top_k,
            )
            retrieved_codes = {ec for c in rag_result.retrieved_chunks for ec in c.error_codes}
            evidence_hit = bool(retrieved_codes & set(case.expected_error_codes))
            text = f"{rag_result.conclusion or ''} {rag_result.recommended_action or ''}"
            per_case[case.case_id] = {
                "evidence_hit": evidence_hit,
                "keyword_score": keyword_score(text, case.expected_keywords),
                "confidence": rag_result.confidence,
                "retrieved_chunk_count": len(rag_result.retrieved_chunks),
                "conclusion": rag_result.conclusion,
                "recommended_action": rag_result.recommended_action,
            }
        sweep.append(aggregate_topk_results(top_k, per_case))
    return sweep


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _print_gate_table(sweep: list[GateSweepResult]) -> None:
    print("\n[margin_pct 임계치 스윕]")
    print(f"{'threshold':>10} {'accuracy':>9} {'불필요호출율':>10} {'누락율':>8}  틀린 케이스")
    for r in sweep:
        print(
            f"{r.threshold:>10} {r.accuracy:>9.0%} {r.unnecessary_rag_rate:>10.0%} "
            f"{r.missed_explanation_rate:>8.0%}  {', '.join(r.wrong_case_ids) or '-'}"
        )


def _print_topk_table(sweep: list[TopKSweepResult]) -> None:
    print("\n[top_k 스윕 - requires_explanation=true 케이스만 대상]")
    print(f"{'top_k':>6} {'evidence_hit':>13} {'keyword_score':>14} {'mean_confidence':>16}")
    for r in sweep:
        print(
            f"{r.top_k:>6} {r.mean_evidence_hit_rate:>13.0%} "
            f"{r.mean_keyword_score:>14.0%} {r.mean_confidence:>16.2f}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden-set", type=Path, default=_DEFAULT_GOLDEN_SET)
    parser.add_argument("--thresholds", default="5,10,15,20,25")
    parser.add_argument("--top-ks", default="2,4,6,8")
    parser.add_argument("--output", type=Path, default=_DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="실행 후 삽입한 log_chunks/judgements를 정리하지 않고 남겨둔다(디버깅용).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    thresholds = [float(t) for t in args.thresholds.split(",")]
    top_ks = [int(k) for k in args.top_ks.split(",")]
    cases = load_golden_set(args.golden_set)
    rag_cases = [c for c in cases if c.requires_explanation]

    spec_results = {c.case_id: evaluate_spec({"value": c.measured_value}, c.spec) for c in cases}

    settings = get_settings()
    pool = await create_pool(settings)
    embedding_client = get_embedding_client(settings)
    llm_client = get_llm_client(settings)

    inserted_chunk_ids: list[int] = []
    base_time = datetime.now(UTC)
    try:
        logger.info("골든셋 %d개 케이스 로드 (RAG 대상 %d개)", len(cases), len(rag_cases))

        gate_sweep = sweep_gate_thresholds(cases, spec_results, thresholds)
        best_threshold = recommend_threshold(gate_sweep)

        logger.info("근거 chunk 시딩 중...")
        for case in rag_cases:
            inserted_chunk_ids += await _seed_case_chunks(pool, embedding_client, case, base_time)

        logger.info("top_k 스윕 실행 중 (LLM 호출 %d회)...", len(rag_cases) * len(top_ks))
        topk_sweep = await run_topk_sweep(pool, embedding_client, llm_client, rag_cases, top_ks)
        best_top_k = recommend_top_k(topk_sweep)

        # 콘솔 출력(Windows cp949 콘솔에서 실패할 수 있음, main() 상단의 UTF-8
        # 강제 설정으로 방지되지만 방어적으로) 전에 리포트부터 파일로 저장한다 —
        # 출력에서 예외가 나도 이 실행에서 얻은 결과가 손실되지 않도록.
        report = {
            "golden_set": str(args.golden_set),
            "case_count": len(cases),
            "rag_case_count": len(rag_cases),
            "gate_sweep": [vars(r) for r in gate_sweep],
            "recommended_margin_pct_threshold": best_threshold.threshold,
            "topk_sweep": [
                {
                    "top_k": r.top_k,
                    "mean_evidence_hit_rate": r.mean_evidence_hit_rate,
                    "mean_keyword_score": r.mean_keyword_score,
                    "mean_confidence": r.mean_confidence,
                    "per_case": r.per_case,
                }
                for r in topk_sweep
            ],
            "recommended_top_k": best_top_k.top_k,
        }
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("리포트 저장: %s", args.output)

        _print_gate_table(gate_sweep)
        print(
            f"\n=> 추천 SPEC_CHECK_MARGIN_THRESHOLD_PCT = {best_threshold.threshold:g} "
            f"(정확도 {best_threshold.accuracy:.0%})"
        )
        _print_topk_table(topk_sweep)
        print(f"\n=> 추천 top_k = {best_top_k.top_k}")
    finally:
        if inserted_chunk_ids and not args.keep_data:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM judgements WHERE use_case = 'spec_check_eval'")
                await conn.execute(
                    "DELETE FROM log_chunks WHERE chunk_id = ANY($1::bigint[])",
                    inserted_chunk_ids,
                )
            logger.info("평가용 임시 데이터 정리 완료 (chunk %d건)", len(inserted_chunk_ids))
        elif inserted_chunk_ids:
            logger.info(
                "--keep-data 지정됨 - chunk_id %s 를 정리하지 않고 유지함", inserted_chunk_ids
            )
        await embedding_client.aclose()
        await llm_client.aclose()
        await pool.close()


if __name__ == "__main__":
    # Windows 콘솔(cp949 등)이 한글/em-dash 등을 출력하다 UnicodeEncodeError로
    # 죽는 것을 방지 — 리포트 저장 자체는 이미 encoding="utf-8"로 안전하지만,
    # 콘솔 출력도 이 실행 결과를 눈으로 보려면 필요하다.
    for _stream in (sys.stdout, sys.stderr):
        if _stream.encoding and _stream.encoding.lower() != "utf-8":
            _stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
