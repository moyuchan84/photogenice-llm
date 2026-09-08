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
