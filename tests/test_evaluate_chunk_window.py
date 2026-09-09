"""scripts/evaluate_chunk_window.py 유닛테스트 — DB/LLM 불필요, 순수 계산만 검증한다."""

from scripts.evaluate_chunk_window import recommend_window, sweep_error_windows


def test_small_window_truncates_recovery_sequence():
    sweep = sweep_error_windows([1, 2])
    by_window = {r.window: r for r in sweep}
    assert by_window[1].recovery_coverage < 1.0
    assert by_window[2].recovery_coverage < 1.0


def test_window_five_fully_covers_recovery_sequence():
    sweep = sweep_error_windows([5])
    assert sweep[0].recovery_coverage == 1.0


def test_larger_window_does_not_improve_coverage_but_grows_chunk_size():
    sweep = sweep_error_windows([5, 10])
    by_window = {r.window: r for r in sweep}
    assert by_window[5].recovery_coverage == 1.0
    assert by_window[10].recovery_coverage == 1.0
    assert by_window[10].avg_chunk_text_chars > by_window[5].avg_chunk_text_chars


def test_recommend_window_picks_smallest_fully_covering_window():
    sweep = sweep_error_windows([1, 2, 3, 5, 8, 10])
    assert recommend_window(sweep).window == 5
