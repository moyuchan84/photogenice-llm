"""1차 E2E 시나리오 실행 — 떠 있는 우리 앱(:8000)과 ftpmodule API(:8001)를 실제로 호출한다.

  1. UC2 계열: scenario.E2E_SPEC_CASES를 identifier 방식(inline_data 없음)으로 호출
     -> 우리 앱이 ftpmodule에서 값·기준을 조합 -> evaluate_criterion 판정
     -> OOS/근접이면 RAG + LLM 설명. 판정/RAG 여부를 기대값과 비교하고,
     spec_evaluations/judgements 감사 레코드를 DB에서 되읽는다.
  2. ID Dump: ftpmodule dump item을 받아 detector 상태로 error_dump_text를 만들어 /query/id-dump
  3. UC1: /query/history 자연어 질의
  4. 오류 경로: 없는 설비(404), 기준 없는 항목(404)

실행(저장소 루트, 우리 venv — .env의 DATABASE_URL/ASML_API_BASE 사용):
    .venv/Scripts/python -m scripts.mock_fleet.run_e2e [--app http://localhost:8000] [--only focal]
LLM(로컬 llama3)은 호출당 수십 초~1분 이상 걸릴 수 있다.
"""

import argparse
import asyncio
import time

import asyncpg
import httpx

from clients.asml_api_client import get_asml_api_client, summarize_dump_item
from config import get_settings
from scripts.mock_fleet import scenario

OK, FAIL = "PASS", "FAIL"


def _short(text: str | None, limit: int = 160) -> str:
    if not text:
        return "-"
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _audit(conn: asyncpg.Connection, eval_id: int | None, judgement_id: int | None) -> str:
    parts = []
    if eval_id is not None:
        row = await conn.fetchrow(
            "SELECT measured_value, lsl, usl, determination, margin_pct, feature_type,"
            " raw_spec_json->'criterion' AS criterion, raw_data_json->'source' AS source"
            " FROM spec_evaluations WHERE eval_id = $1",
            eval_id,
        )
        parts.append(
            f"spec_evaluations#{eval_id}: value={row['measured_value']} lsl={row['lsl']} "
            f"usl={row['usl']} criterion={row['criterion']} source={row['source']}"
        )
    if judgement_id is not None:
        row = await conn.fetchrow(
            "SELECT use_case, feature_type, eval_id, retrieved_chunk_ids FROM judgements"
            " WHERE judgement_id = $1",
            judgement_id,
        )
        parts.append(
            f"judgements#{judgement_id}: use_case={row['use_case']} feature={row['feature_type']} "
            f"eval_id={row['eval_id']} retrieved={list(row['retrieved_chunk_ids'])}"
        )
    return "\n        ".join(parts)


async def run_spec_cases(
    http: httpx.AsyncClient, conn: asyncpg.Connection, only: str | None
) -> list[bool]:
    results = []
    for case in scenario.E2E_SPEC_CASES:
        label = f"{case['route']} {case['equipment_id']} {case['parameter']}"
        if only and only not in label:
            continue
        started = time.perf_counter()
        resp = await http.post(
            case["route"],
            json={"equipment_id": case["equipment_id"], "parameter": case["parameter"]},
        )
        elapsed = time.perf_counter() - started
        if resp.status_code != 200:
            print(f"[{FAIL}] {label} -> HTTP {resp.status_code}: {resp.text[:300]}")
            results.append(False)
            continue
        body = resp.json()
        rag_called = body["judgement_id"] is not None
        passed = body["determination"] == case["expect"] and rag_called == case["expect_rag"]
        results.append(passed)
        print(
            f"[{OK if passed else FAIL}] {label} ({elapsed:.1f}s)\n"
            f"    판정={body['determination']} margin={body['margin_pct']}% "
            f"(기대 {case['expect']}, RAG {'O' if case['expect_rag'] else 'X'} / 실제 RAG {'O' if rag_called else 'X'})"
        )
        if rag_called:
            print(
                f"    결론: {_short(body['conclusion'])}\n"
                f"    권고: {_short(body['recommended_action'])} (confidence={body['confidence']}, "
                f"근거 chunk={body['evidence_chunk_ids']})"
            )
        print(f"    감사: {await _audit(conn, body['eval_id'], body['judgement_id'])}")
    return results


async def run_id_dump(http: httpx.AsyncClient, conn: asyncpg.Connection) -> bool:
    asml = get_asml_api_client(get_settings())
    if asml is None:
        print(f"[{FAIL}] /query/id-dump — ASML_API_BASE가 설정되지 않았습니다")
        return False
    try:
        item = await asml.fetch_item(equipment_id="MOCK-EUV-01", item="dump")
    finally:
        await asml.aclose()
    text = summarize_dump_item(item)
    started = time.perf_counter()
    resp = await http.post(
        "/query/id-dump", json={"equipment_id": "MOCK-EUV-01", "error_dump_text": text}
    )
    elapsed = time.perf_counter() - started
    if resp.status_code != 200:
        print(f"[{FAIL}] /query/id-dump -> HTTP {resp.status_code}: {resp.text[:300]}")
        return False
    body = resp.json()
    passed = bool(body["evidence_chunk_ids"]) and body["conclusion"] is not None
    print(
        f"[{OK if passed else FAIL}] /query/id-dump MOCK-EUV-01 ({elapsed:.1f}s)\n"
        f"    입력(ftpmodule dump에서 생성): {_short(text, 220)}\n"
        f"    결론: {_short(body['conclusion'])}\n"
        f"    권고: {_short(body['recommended_action'])} (근거 chunk={body['evidence_chunk_ids']})\n"
        f"    감사: {await _audit(conn, None, body['judgement_id'])}"
    )
    return passed


async def run_history(http: httpx.AsyncClient) -> bool:
    query = "MOCK-EUV-01에서 focus scale 편차로 노광이 보류된 적이 있나? 원인과 조치는?"
    started = time.perf_counter()
    resp = await http.post(
        "/query/history", json={"query": query, "equipment_id": "MOCK-EUV-01", "top_k": 5}
    )
    elapsed = time.perf_counter() - started
    if resp.status_code != 200:
        print(f"[{FAIL}] /query/history -> HTTP {resp.status_code}: {resp.text[:300]}")
        return False
    body = resp.json()
    # UC1은 feature_type=log_general만 검색한다 — 목업 이력은 전부 기능별로 분류되므로
    # 근거 0건 -> LLM 생략(conclusion=None)이 정상 동작이다(rag/core.py 가드).
    print(
        f"[INFO] /query/history ({elapsed:.1f}s) 근거 chunk={body['evidence_chunk_ids']} "
        f"결론={_short(body['conclusion'])}"
    )
    return True


async def run_error_cases(http: httpx.AsyncClient) -> list[bool]:
    cases = [
        ("없는 설비", {"equipment_id": "MOCK-EUV-99", "parameter": "SCALE_CH1"}, 404),
        ("기준 없는 항목", {"equipment_id": "MOCK-EUV-01", "parameter": "NO_SUCH_ITEM"}, 404),
    ]
    results = []
    for label, payload, expected in cases:
        resp = await http.post("/query/focal-curve", json=payload)
        passed = resp.status_code == expected
        results.append(passed)
        detail = (
            resp.json().get("detail")
            if resp.headers.get("content-type", "").startswith("application/json")
            else resp.text
        )
        print(
            f"[{OK if passed else FAIL}] {label} -> HTTP {resp.status_code} (기대 {expected}): {_short(str(detail))}"
        )
    return results


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", default="http://localhost:8000")
    parser.add_argument("--only", help="spec 케이스 라벨 부분 문자열 필터 (예: SCALE_CH1)")
    parser.add_argument("--skip-llm-extras", action="store_true", help="id-dump/history 생략")
    args = parser.parse_args()

    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url)
    results: list[bool] = []
    try:
        async with httpx.AsyncClient(base_url=args.app, timeout=600) as http:
            print("== UC2: ftpmodule identifier 조회 -> 결정론 판정 -> 조건부 RAG ==")
            results += await run_spec_cases(http, conn, args.only)
            if not args.only and not args.skip_llm_extras:
                print("\n== ID Dump: ftpmodule dump item -> RAG 원인 추정 ==")
                results.append(await run_id_dump(http, conn))
                print("\n== UC1: 이력 질의 ==")
                results.append(await run_history(http))
            if not args.only:
                print("\n== 오류 경로 ==")
                results += await run_error_cases(http)
    finally:
        await conn.close()

    passed = sum(results)
    print(f"\n결과: {passed}/{len(results)} 통과")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
