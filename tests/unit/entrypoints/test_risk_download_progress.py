from __future__ import annotations

from io import StringIO
from threading import Event

from trader.recommendation.application.risk_download import RiskDownloadProgress
from trader.recommendation.entrypoints.risk_download_progress import StderrRiskDownloadProgress


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class _HeartbeatStream(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.heartbeat = Event()

    def write(self, value: str) -> int:
        written = super().write(value)
        if "仍在运行" in value:
            self.heartbeat.set()
        return written


def test_risk_download_reports_startup_and_heartbeat_during_silent_recovery() -> None:
    stream = _HeartbeatStream()
    reporter = StderrRiskDownloadProgress(stream, heartbeat_seconds=0.01)

    reporter.start()
    try:
        reporter.publish(RiskDownloadProgress("local_snapshot", "started", 0, 0, 5027, 5027))
        assert stream.heartbeat.wait(timeout=1.0)
    finally:
        reporter.close()

    output = stream.getvalue()
    assert "启动准备 | 开始" in output
    assert "本地日库 | 开始" in output
    assert "本地日库 | 仍在运行 | 距上次状态更新" in output
    assert "总体      0/  5027" not in output


def test_risk_download_progress_uses_stderr_style_fields_and_checkpoint_counts() -> None:
    stream = StringIO()
    reporter = StderrRiskDownloadProgress(stream, monotonic=_Clock(10.0, 75.0))

    reporter.publish(
        RiskDownloadProgress(
            stage="wave_summary",
            state="completed",
            wave=2,
            completed_units=998,
            total_units=5027,
            requested_codes=5027,
            research_pending_codes=4029,
            official_pending_codes=4987,
            available_codes=998,
            covered_codes=12,
            deferred_codes=4029,
            changed_codes=500,
            official_pages_fetched=30,
            official_pages_checkpointed=28,
            official_announcements_saved=80,
            official_resumed_codes=2,
        )
    )

    output = stream.getvalue()
    assert output.startswith("00:01:05 | risk_download 下载进度 | 波次 2 | 波次汇总 | 完成")
    assert "总体    998/  5027（ 19.85%）" in output
    assert "研究累计    998/  5027（ 19.85%） | 巨潮近一年累计     40/  5027（  0.80%）" in output
    assert "请求   5027 | 研究待补   4029 | 巨潮待补   4987" in output
    assert "可用    998 | 四项有值     12 | 延后   4029 | 变更    500" in output
    assert "官方分页     30 | 断点分页     28 | 公告保存     80 | 断点续传      2" in output


def test_running_research_progress_is_bounded_without_hiding_new_checkpoint() -> None:
    stream = StringIO()
    reporter = StderrRiskDownloadProgress(stream, monotonic=_Clock(10.0, 11.0, 12.0, 16.0))

    for count in (1, 2, 3):
        reporter.publish(RiskDownloadProgress("research_retry", "running", 1, count, 5027, 5027))

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert "本波处理      1/  5027" in lines[0]
    assert "本波处理      3/  5027" in lines[1]


def test_risk_download_progress_keeps_cumulative_counts_across_sources_and_waves() -> None:
    stream = StringIO()
    reporter = StderrRiskDownloadProgress(stream, monotonic=_Clock(10.0, 20.0, 21.0, 22.0, 23.0))

    reporter.publish(
        RiskDownloadProgress(
            stage="research_retry",
            state="deferred",
            wave=1,
            completed_units=61,
            total_units=4294,
            requested_codes=5027,
            research_pending_codes=4233,
            official_pending_codes=4984,
            changed_codes=61,
        )
    )
    reporter.publish(
        RiskDownloadProgress(
            stage="official_announcements",
            state="started",
            wave=1,
            completed_units=0,
            total_units=4984,
            requested_codes=5027,
            research_pending_codes=4233,
            official_pending_codes=4984,
            changed_codes=61,
        )
    )
    reporter.publish(
        RiskDownloadProgress(
            stage="official_announcements",
            state="deferred",
            wave=1,
            completed_units=3,
            total_units=4984,
            requested_codes=5027,
            research_pending_codes=4233,
            official_pending_codes=4981,
            changed_codes=64,
        )
    )
    reporter.publish(
        RiskDownloadProgress(
            stage="research_retry",
            state="started",
            wave=2,
            completed_units=0,
            total_units=4233,
            requested_codes=5027,
            research_pending_codes=4233,
            official_pending_codes=4981,
            changed_codes=64,
        )
    )

    lines = stream.getvalue().splitlines()
    assert "研究累计    794/  5027（ 15.79%） | 巨潮近一年累计     43/  5027（  0.86%）" in lines[0]
    assert "本波处理     61/  4294（  1.42%）" in lines[0]
    assert "巨潮公告 | 开始 | 研究累计    794/  5027（ 15.79%）" in lines[1]
    assert "巨潮近一年累计     43/  5027（  0.86%） | 本波待处理   4984" in lines[1]
    assert "研究累计    794/  5027（ 15.79%） | 巨潮近一年累计     46/  5027（  0.92%）" in lines[2]
    assert "研究重试 | 开始 | 研究累计    794/  5027（ 15.79%）" in lines[3]
    assert "巨潮近一年累计     46/  5027（  0.92%） | 本波待处理   4233" in lines[3]
    assert "0/  4984（  0.00%）" not in lines[1]
    assert "0/  4233（  0.00%）" not in lines[3]


def test_risk_download_progress_labels_database_lock_wait_without_false_wave_completion() -> None:
    stream = StringIO()
    reporter = StderrRiskDownloadProgress(stream, monotonic=_Clock(10.0, 18.0))

    reporter.publish(
        RiskDownloadProgress(
            stage="official_announcements",
            state="waiting",
            wave=2,
            completed_units=0,
            total_units=4798,
            requested_codes=5027,
            research_pending_codes=0,
            official_pending_codes=4798,
            official_persistence_error="busy",
            official_sync_seconds=8.0,
        )
    )

    output = stream.getvalue()
    assert "巨潮公告 | 等待日库" in output
    assert "本波待处理   4798" in output
    assert "日库写入 等待写锁" in output
    assert "本波处理      0/  4798" not in output
