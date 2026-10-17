from __future__ import annotations

from io import StringIO

from trader.recommendation.application.risk_download import RiskDownloadProgress
from trader.recommendation.entrypoints.risk_download_progress import StderrRiskDownloadProgress


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


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
            available_codes=998,
            covered_codes=12,
            deferred_codes=4029,
            changed_codes=500,
            official_pages_fetched=30,
            official_announcements_saved=80,
        )
    )

    output = stream.getvalue()
    assert output.startswith("00:01:05 | risk_download 下载进度 | 波次 2 | 波次汇总 | 完成")
    assert "阶段    998/  5027（ 19.85%）" in output
    assert "请求   5027 | 可用    998 | 完整     12 | 延后   4029 | 变更    500" in output
    assert "官方分页     30 | 公告保存     80" in output
