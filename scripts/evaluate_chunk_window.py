"""Phase 5(Session 12) 평가 스크립트 — 청킹 윈도우 크기(CHUNK_ERROR_WINDOW_BEFORE/AFTER)
튜닝. DB/LLM이 필요 없는 순수 계산이다 — workers/chunker.py의 chunk_logs()는 이미
외부 의존성 없는 순수 함수이므로, 합성한 "전형적인 이상 감지→조치→복구" 로그
타임라인에 후보 윈도우 크기를 적용해 두 가지를 측정한다:

1. 복구 시퀀스 커버리지 — ERROR/WARNING 이후의 실제 조치/복구 로그가 청크에 몇 %
   포함되는지 (너무 작은 윈도우는 원인 분석에 필요한 조치 결과를 잘라낸다)
2. 청크 크기 — 윈도우가 커질수록 chunk_text에 포함되는 무관한 정상 로그가 늘어나
   임베딩/프롬프트 비용이 커지는지 (복구 커버리지가 이미 100%인데도 계속 커지면
   그 이상은 낭비)

CHUNK_SESSION_GAP_SEC은 "세션(이벤트 버스트) 분리" 기준이라 이 스크립트의 관심사인
"에러 하나당 앞뒤로 몇 줄을 포함할지"와는 독립적이므로, 여기서는 충분히 큰 고정값(600,
현재 기본값)으로 두고 error_window_before/after만 스윕한다.

사용법: python -m scripts.evaluate_chunk_window
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from workers.chunker import RawLogRow, chunk_logs

_BASE = datetime(2026, 9, 1, 8, 0, 0, tzinfo=UTC)
_SESSION_GAP_SEC = 600

# 합성 시나리오: 선행 정상 로그 12개 -> 경고 2개 -> 에러 1개 -> 조치/복구 로그 5개
# (실제 조치가 끝나는 지점) -> 후행 정상 로그 12개. 30초 간격.
_LEAD_NORMAL = 12
_RECOVERY_STEPS = [
    "재조정 시작",
    "서보 파라미터 조정 중",
    "1차 확인",
    "안정화 확인",
    "정상 복귀 확인",
]
_TRAIL_NORMAL = 12


def _build_incident_timeline() -> list[RawLogRow]:
    rows: list[RawLogRow] = []
    minute = 0.0
    log_id = 0

    def _add(level: str | None, error_code: str | None, message: str) -> None:
        nonlocal minute, log_id
        rows.append(
            RawLogRow(
                log_id=log_id,
                equipment_id="EQ-EVAL",
                event_time=_BASE + timedelta(minutes=minute),
                error_code=error_code,
                log_level=level,
                message=message,
            )
        )
        log_id += 1
        minute += 0.5  # 30초 간격

    for i in range(_LEAD_NORMAL):
        _add("INFO", None, f"정상 동작 {i}")
    _add("WARNING", "FOC-114", "focus drift 경향 감지")
    _add("WARNING", "FOC-114", "focus drift 지속")
    _add("ERROR", "FOC-114", "focus 스펙 이탈")
    for step in _RECOVERY_STEPS:
        _add("INFO", None, step)
    for i in range(_TRAIL_NORMAL):
        _add("INFO", None, f"정상 동작 {i}")

    return rows


@dataclass(frozen=True)
class WindowSweepResult:
    window: int
    recovery_coverage: float  # 복구 로그가 청크에 포함된 비율(0~1)
    total_rows_in_incident_chunk: int
    avg_chunk_text_chars: int


def sweep_error_windows(windows: list[int]) -> list[WindowSweepResult]:
    rows = _build_incident_timeline()
    recovery_log_ids = set(range(_LEAD_NORMAL + 3, _LEAD_NORMAL + 3 + len(_RECOVERY_STEPS)))

    results = []
    for w in windows:
        chunks = chunk_logs(
            rows,
            session_gap_sec=_SESSION_GAP_SEC,
            error_window_before=w,
            error_window_after=w,
        )
        incident_chunks = [c for c in chunks if c.error_codes]
        incident_log_ids = {lid for c in incident_chunks for lid in c.log_ids}

        covered = len(recovery_log_ids & incident_log_ids)
        avg_len = (
            sum(len(c.chunk_text) for c in incident_chunks) // len(incident_chunks)
            if incident_chunks
            else 0
        )
        results.append(
            WindowSweepResult(
                window=w,
                recovery_coverage=covered / len(recovery_log_ids),
                total_rows_in_incident_chunk=len(incident_log_ids),
                avg_chunk_text_chars=avg_len,
            )
        )
    return results


def recommend_window(sweep: list[WindowSweepResult]) -> WindowSweepResult:
    """복구 시퀀스를 100% 포함하는 가장 작은 윈도우를 추천한다 — 그 이상은 무관한
    정상 로그만 늘려 임베딩/프롬프트 비용을 키우는 낭비이기 때문이다."""
    fully_covered = [r for r in sweep if r.recovery_coverage >= 1.0]
    candidates = fully_covered or sweep
    return min(candidates, key=lambda r: r.window)


def _print_table(sweep: list[WindowSweepResult]) -> None:
    print("\n[error_window_before/after 스윕 - 합성 이상감지->복구 시나리오]")
    print(f"{'window':>6} {'복구 커버리지':>12} {'청크 내 행 수':>12} {'평균 chunk 길이(자)':>18}")
    for r in sweep:
        print(
            f"{r.window:>6} {r.recovery_coverage:>12.0%} "
            f"{r.total_rows_in_incident_chunk:>12} {r.avg_chunk_text_chars:>18}"
        )


def main() -> None:
    windows = [1, 2, 3, 5, 8, 10]
    sweep = sweep_error_windows(windows)
    best = recommend_window(sweep)
    _print_table(sweep)
    print(
        f"\n=> 추천 CHUNK_ERROR_WINDOW_BEFORE/AFTER = {best.window} "
        f"(복구 시퀀스 {best.recovery_coverage:.0%} 커버, 평균 청크 {best.avg_chunk_text_chars}자)"
    )


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if _stream.encoding and _stream.encoding.lower() != "utf-8":
            _stream.reconfigure(encoding="utf-8", errors="replace")
    main()
