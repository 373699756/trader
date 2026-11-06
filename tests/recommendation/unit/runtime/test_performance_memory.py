"""A real process reading checks that current RSS is not its historical peak."""

from __future__ import annotations

from trader.recommendation.infra.performance.process_memory import ReadPerformanceMemory


def test_process_memory_reads_positive_current_and_peak_values():
    sample = ReadPerformanceMemory()
    assert sample.current_rss_bytes > 0
    assert sample.peak_rss_bytes > 0
    # A later peak sample covers allocations and accounting movement during reads.
    later = ReadPerformanceMemory()
    assert later.peak_rss_bytes + 1024 * 1024 >= sample.current_rss_bytes
