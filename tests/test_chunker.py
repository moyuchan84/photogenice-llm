from datetime import UTC, datetime, timedelta

from workers.chunker import RawLogRow, chunk_logs

BASE = datetime(2026, 9, 8, 10, 0, 0, tzinfo=UTC)


def _row(log_id, minute, level=None, error_code=None, message="msg", equipment_id="EQ-01"):
    return RawLogRow(
        log_id=log_id,
        equipment_id=equipment_id,
        event_time=BASE + timedelta(minutes=minute),
        error_code=error_code,
        log_level=level,
        message=message,
    )


DEFAULT_KW = {"session_gap_sec": 600, "error_window_before": 2, "error_window_after": 2}


def test_empty_input_returns_no_chunks():
    assert chunk_logs([], **DEFAULT_KW) == []


def test_single_session_no_error_becomes_one_chunk():
    rows = [_row(i, i) for i in range(5)]
    chunks = chunk_logs(rows, **DEFAULT_KW)
    assert len(chunks) == 1
    assert chunks[0].log_ids == [0, 1, 2, 3, 4]
    assert chunks[0].error_codes == []


def test_large_gap_splits_into_two_sessions():
    rows = [_row(0, 0), _row(1, 1), _row(2, 100), _row(3, 101)]
    chunks = chunk_logs(rows, session_gap_sec=60, error_window_before=1, error_window_after=1)
    assert len(chunks) == 2
    assert chunks[0].log_ids == [0, 1]
    assert chunks[1].log_ids == [2, 3]


def test_error_in_middle_creates_windowed_chunk():
    rows = [_row(i, i) for i in range(10)]
    rows[5] = _row(5, 5, level="ERROR", error_code="E123")
    chunks = chunk_logs(rows, session_gap_sec=600, error_window_before=2, error_window_after=2)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.log_ids == [3, 4, 5, 6, 7]
    assert chunk.error_codes == ["E123"]
    assert "E123" in chunk.chunk_text


def test_overlapping_error_windows_are_merged():
    rows = [_row(i, i) for i in range(10)]
    rows[3] = _row(3, 3, level="ERROR", error_code="E1")
    rows[5] = _row(5, 5, level="WARNING", error_code="E2")
    chunks = chunk_logs(rows, session_gap_sec=600, error_window_before=2, error_window_after=2)
    assert len(chunks) == 1
    assert chunks[0].log_ids == [1, 2, 3, 4, 5, 6, 7]
    assert chunks[0].error_codes == ["E1", "E2"]


def test_far_apart_errors_create_separate_chunks():
    rows = [_row(i, i) for i in range(20)]
    rows[2] = _row(2, 2, level="ERROR", error_code="E1")
    rows[15] = _row(15, 15, level="ERROR", error_code="E2")
    chunks = chunk_logs(rows, session_gap_sec=600, error_window_before=1, error_window_after=1)
    assert len(chunks) == 2
    assert chunks[0].error_codes == ["E1"]
    assert chunks[1].error_codes == ["E2"]


def test_error_at_session_boundary_clips_window_without_error():
    rows = [_row(i, i) for i in range(5)]
    rows[0] = _row(0, 0, level="ERROR", error_code="E1")
    chunks = chunk_logs(rows, session_gap_sec=600, error_window_before=3, error_window_after=1)
    assert len(chunks) == 1
    assert chunks[0].log_ids == [0, 1]


def test_multiple_equipment_ids_are_not_cross_contaminated():
    rows = [
        _row(0, 0, equipment_id="EQ-01"),
        _row(1, 1, equipment_id="EQ-02"),
        _row(2, 2, equipment_id="EQ-01"),
        _row(3, 3, equipment_id="EQ-02"),
    ]
    chunks = chunk_logs(rows, **DEFAULT_KW)
    equipment_ids = {c.equipment_id for c in chunks}
    assert equipment_ids == {"EQ-01", "EQ-02"}
    for c in chunks:
        assert all(log_id in (0, 2) for log_id in c.log_ids) or all(
            log_id in (1, 3) for log_id in c.log_ids
        )


def test_log_level_case_insensitive():
    rows = [_row(i, i) for i in range(5)]
    rows[2] = _row(2, 2, level="error", error_code="E1")
    chunks = chunk_logs(rows, session_gap_sec=600, error_window_before=1, error_window_after=1)
    assert chunks[0].error_codes == ["E1"]


# --- feature_type 분류 (Phase 6 후속) -----------------------------------------
# 검색은 항상 feature_type으로 필터링하므로(rag/retriever.py), 적재 시 분류가 붙지
# 않으면 focal_curve/final_xy/id_dump 엔드포인트의 근거가 영구히 0건이 된다.

FEATURE_KW = {
    "focal_curve": ("focal", "focus", "dof"),
    "final_xy": ("overlay", "c2c", "정렬"),
    "id_dump": ("dump", "tdf", "detector"),
}


def test_no_keyword_map_keeps_default_feature_type():
    chunks = chunk_logs([_row(0, 0, message="아무 로그")], **DEFAULT_KW)
    assert chunks[0].feature_type == "log_general"


def test_unmatched_message_falls_back_to_log_general():
    chunks = chunk_logs(
        [_row(0, 0, message="wafer 반송 완료")], **DEFAULT_KW, feature_keywords=FEATURE_KW
    )
    assert chunks[0].feature_type == "log_general"


def test_message_keyword_selects_feature_type():
    chunks = chunk_logs(
        [_row(0, 0, message="focus offset drift 감지")], **DEFAULT_KW, feature_keywords=FEATURE_KW
    )
    assert chunks[0].feature_type == "focal_curve"


def test_matching_is_case_insensitive():
    chunks = chunk_logs(
        [_row(0, 0, message="OVERLAY 편차 초과")], **DEFAULT_KW, feature_keywords=FEATURE_KW
    )
    assert chunks[0].feature_type == "final_xy"


def test_error_code_also_contributes_to_classification():
    rows = [_row(0, 0, level="ERROR", error_code="DUMP_READ_FAIL", message="처리 실패")]
    chunks = chunk_logs(rows, **DEFAULT_KW, feature_keywords=FEATURE_KW)
    assert chunks[0].feature_type == "id_dump"


def test_highest_hit_count_wins_when_multiple_features_match():
    rows = [
        _row(0, 0, message="overlay 정렬 편차 발생"),  # final_xy 2히트
        _row(1, 1, message="focus 재조정"),  # focal_curve 1히트
    ]
    chunks = chunk_logs(rows, **DEFAULT_KW, feature_keywords=FEATURE_KW)
    assert chunks[0].feature_type == "final_xy"


def test_tie_is_broken_deterministically_by_feature_name():
    """동점이면 사전순으로 끊는다 — 재백필 시 같은 로그가 다른 feature_type으로
    분류되면 검색 결과가 조용히 달라지기 때문."""
    rows = [_row(0, 0, message="focus 측정 후 overlay 확인")]  # 각 1히트
    chunks = chunk_logs(rows, **DEFAULT_KW, feature_keywords=FEATURE_KW)
    assert chunks[0].feature_type == "final_xy"  # final_xy < focal_curve


def test_classification_is_per_chunk_not_per_batch():
    """한 배치 안에서도 청크마다 독립적으로 분류돼야 한다."""
    rows = [
        _row(0, 0, message="focus offset drift"),
        _row(1, 500, message="overlay 정렬 실패"),  # 세션 분리
    ]
    chunks = chunk_logs(
        rows,
        session_gap_sec=60,
        error_window_before=1,
        error_window_after=1,
        feature_keywords=FEATURE_KW,
    )
    assert [c.feature_type for c in chunks] == ["focal_curve", "final_xy"]
