"""세션/에러 윈도우 기반 청킹.

외부 의존성(DB, config) 없는 순수 함수로 유지한다 — 파라미터는 호출자
(embedding_sync_poller.py)가 config에서 읽어 명시적으로 전달한다.

알고리즘 (asml-rag-overview.html "청킹 전략" 참고):
1. equipment_id로 그룹핑, event_time 정렬
2. 인접 로그 간 시간 간격이 session_gap_sec 초과 시 세션(이벤트 버스트) 분리
3. 세션 내 ERROR/WARNING 로그가 있으면 그 전후로 error_window_before/after개 로그를 포함한
   윈도우를 만들고, 겹치는 윈도우는 병합. 에러가 없으면 세션 전체를 하나의 청크로.
4. 청크 텍스트 앞에 설비/기간/에러코드를 자연어로 녹인 프리픽스를 붙인다.
5. 호출자가 준 feature_keywords로 청크의 feature_type을 분류한다 — 검색이 항상
   feature_type으로 필터링하므로(rag/retriever.py) 이 분류가 없으면 기능별
   엔드포인트의 RAG 근거가 0건이 된다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise

_ERROR_LEVELS = frozenset({"ERROR", "WARNING"})
_DEFAULT_FEATURE_TYPE = "log_general"


@dataclass(frozen=True)
class RawLogRow:
    log_id: int
    equipment_id: str
    event_time: datetime
    error_code: str | None = None
    log_level: str | None = None
    message: str | None = None


@dataclass
class LogChunk:
    equipment_id: str
    chunk_text: str
    log_ids: list[int]
    period_start: datetime
    period_end: datetime
    error_codes: list[str] = field(default_factory=list)
    feature_type: str = _DEFAULT_FEATURE_TYPE


def chunk_logs(
    rows: Sequence[RawLogRow],
    *,
    session_gap_sec: int,
    error_window_before: int,
    error_window_after: int,
    error_levels: frozenset[str] = _ERROR_LEVELS,
    feature_keywords: Mapping[str, Sequence[str]] | None = None,
) -> list[LogChunk]:
    chunks: list[LogChunk] = []
    for equipment_id, group in _group_by_equipment(rows).items():
        ordered = sorted(group, key=lambda r: r.event_time)
        for session in _split_into_sessions(ordered, session_gap_sec):
            chunks.extend(
                _chunk_session(
                    equipment_id,
                    session,
                    error_window_before,
                    error_window_after,
                    error_levels,
                    feature_keywords or {},
                )
            )
    return chunks


def _group_by_equipment(rows: Sequence[RawLogRow]) -> dict[str, list[RawLogRow]]:
    groups: dict[str, list[RawLogRow]] = {}
    for r in rows:
        groups.setdefault(r.equipment_id, []).append(r)
    return groups


def _split_into_sessions(ordered: list[RawLogRow], session_gap_sec: int) -> list[list[RawLogRow]]:
    if not ordered:
        return []
    sessions: list[list[RawLogRow]] = [[ordered[0]]]
    for prev, cur in pairwise(ordered):
        gap = (cur.event_time - prev.event_time).total_seconds()
        if gap > session_gap_sec:
            sessions.append([cur])
        else:
            sessions[-1].append(cur)
    return sessions


def _merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not windows:
        return []
    windows = sorted(windows)
    merged = [windows[0]]
    for start, end in windows[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _chunk_session(
    equipment_id: str,
    session: list[RawLogRow],
    error_window_before: int,
    error_window_after: int,
    error_levels: frozenset[str],
    feature_keywords: Mapping[str, Sequence[str]],
) -> list[LogChunk]:
    error_indices = [
        i for i, r in enumerate(session) if r.log_level and r.log_level.upper() in error_levels
    ]

    if not error_indices:
        return [
            _build_chunk(equipment_id, session, error_codes=[], feature_keywords=feature_keywords)
        ]

    windows = [
        (max(0, i - error_window_before), min(len(session) - 1, i + error_window_after))
        for i in error_indices
    ]
    merged = _merge_windows(windows)

    result = []
    for start, end in merged:
        window_rows = session[start : end + 1]
        error_codes = sorted({r.error_code for r in window_rows if r.error_code})
        result.append(
            _build_chunk(
                equipment_id,
                window_rows,
                error_codes=error_codes,
                feature_keywords=feature_keywords,
            )
        )
    return result


def classify_feature_type(
    rows: Sequence[RawLogRow], feature_keywords: Mapping[str, Sequence[str]]
) -> str:
    """청크에 속한 로그의 message/error_code에서 키워드 히트 수가 가장 많은 feature_type을
    고른다. 히트가 0이면 UC1 기본값(log_general). 동점이면 feature_type 이름의 사전순으로
    끊어 같은 입력이 항상 같은 결과를 내도록 한다(재백필 시 분류가 흔들리면 안 됨)."""
    if not feature_keywords:
        return _DEFAULT_FEATURE_TYPE
    haystack = " ".join(part.lower() for r in rows for part in (r.message, r.error_code) if part)
    if not haystack:
        return _DEFAULT_FEATURE_TYPE
    ranked = sorted(
        (
            (-sum(1 for kw in keywords if kw.lower() in haystack), feature_type)
            for feature_type, keywords in feature_keywords.items()
        )
    )
    negative_hits, best_feature_type = ranked[0]
    return best_feature_type if negative_hits < 0 else _DEFAULT_FEATURE_TYPE


def _build_chunk(
    equipment_id: str,
    rows: list[RawLogRow],
    error_codes: list[str],
    feature_keywords: Mapping[str, Sequence[str]],
) -> LogChunk:
    period_start = rows[0].event_time
    period_end = rows[-1].event_time
    fmt = "%Y-%m-%d %H:%M:%S"
    if error_codes:
        prefix = (
            f"[설비 {equipment_id}, {period_start.strftime(fmt)}~{period_end.strftime(fmt)}, "
            f"에러코드 {','.join(error_codes)} 발생]"
        )
    else:
        prefix = f"[설비 {equipment_id}, {period_start.strftime(fmt)}~{period_end.strftime(fmt)}]"
    body = "\n".join(
        f"{r.event_time.strftime(fmt)} [{r.log_level or 'INFO'}"
        f"{':' + r.error_code if r.error_code else ''}] {r.message or '(메시지 없음)'}"
        for r in rows
    )
    return LogChunk(
        equipment_id=equipment_id,
        chunk_text=f"{prefix}\n{body}",
        log_ids=[r.log_id for r in rows],
        period_start=period_start,
        period_end=period_end,
        error_codes=error_codes,
        feature_type=classify_feature_type(rows, feature_keywords),
    )
