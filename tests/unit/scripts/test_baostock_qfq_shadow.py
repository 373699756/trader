from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from scripts.runtime_diagnostics import baostock_qfq_shadow as shadow
from trader.download.infra.history_archive_status import HistoryArchiveError


def _archive(*, cutoff: date = date(2026, 1, 5)) -> SimpleNamespace:
    sessions = tuple(date(2026, 1, day) for day in range(1, 6))
    security = SimpleNamespace(code="600001", listed_on=date(2020, 1, 1), delisted_on=None)
    snapshot = SimpleNamespace(
        sequence=3,
        content_hash="a" * 64,
        data_cutoff=cutoff,
        label_cutoff=date(2026, 1, 4),
    )
    calendar = SimpleNamespace(open_dates=sessions)
    universe = SimpleNamespace(securities=(security,))
    return SimpleNamespace(root=None, snapshot=snapshot, calendar=calendar, universe=universe)


def _fact(day: date, *, qfq: bool = True) -> tuple[str, date, bool, bool]:
    return "600001", day, True, qfq


def test_complete_pairing_reports_candidate_reuse_but_never_production_eligibility(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _archive()
    monkeypatch.setattr(shadow, "load_active_history_archive", lambda _root: archive)
    monkeypatch.setattr(
        shadow,
        "SQLiteHistoryArchiveReader",
        lambda _root: SimpleNamespace(
            iter_shadow_facts=lambda *_args: iter(_fact(day) for day in archive.calendar.open_dates)
        ),
    )

    report = shadow.build_shadow_report(archive.root)

    assert report["status"] == "degraded"
    assert report["production_eligible"] is False
    assert report["raw_qfq_integrity"]["qfq_missing_rows"] == 0
    assert report["request_baseline"]["recent_refresh_raw_qfq_queries"] == 2
    assert report["request_baseline"]["theoretical_skippable_queries"] == 1


def test_missing_qfq_is_visible_in_shadow_report(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _archive()
    monkeypatch.setattr(shadow, "load_active_history_archive", lambda _root: archive)
    monkeypatch.setattr(
        shadow,
        "SQLiteHistoryArchiveReader",
        lambda _root: SimpleNamespace(
            iter_shadow_facts=lambda *_args: iter(
                _fact(day, qfq=day != date(2026, 1, 5)) for day in archive.calendar.open_dates
            )
        ),
    )

    report = shadow.build_shadow_report(archive.root)

    assert report["raw_qfq_integrity"]["qfq_missing_rows"] == 1
    assert report["raw_qfq_integrity"]["pair_complete_rate"] == 0.0


def test_missing_active_snapshot_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_root: object) -> None:
        raise HistoryArchiveError("history_snapshot_unavailable")

    monkeypatch.setattr(shadow, "load_active_history_archive", unavailable)

    with pytest.raises(HistoryArchiveError):
        shadow.build_shadow_report(None)  # type: ignore[arg-type]
