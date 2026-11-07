from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from scripts.download.history import price_repair_audit
from tests.helpers.download.configuration import HistorySyncConfigurationFixture
from tests.helpers.download.history.infra.history_sync import FakeSupplier, NOW
from tests.helpers.download.history_fixtures import RunHistoryFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_price_repair import HistoryPriceRepair

_DAYS = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))


class _GapSupplier(FakeSupplier):
    def fetch_tencent_window(
        self, security: BaoStockSecurity, dates: tuple[date, ...],
    ) -> PublishedHistoryWindow:
        window = super().fetch_tencent_window(security, dates)
        if security.code != "600001":
            return window
        return PublishedHistoryWindow(
            security.code,
            (replace(window.cells[0], status="qfq_missing", qfq=None), *window.cells[1:]),
        )


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def test_new_audit_repairs_current_schema_and_keeps_source_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "source" / "history"
    configuration = HistorySyncConfigurationFixture(
        root, reference_root=root.parent / "reference", sessions=len(_DAYS),
        reread_sessions=1, minimum_free_bytes=0,
    )
    result = RunHistoryFixture(configuration, _GapSupplier(_DAYS), clock=lambda: NOW)
    assert result.state == "completed" and result.unresolved_price_cells == 1
    source_before = _file_hashes(root)
    baseline = FakeSupplier(_DAYS)
    monkeypatch.setattr(
        price_repair_audit, "BaoStockHistorySupplier", lambda *args, **kwargs: nullcontext(baseline),
    )
    monkeypatch.setattr(price_repair_audit, "LoadHistorySyncConfiguration", lambda *args: configuration)
    work = tmp_path / "audit"
    monkeypatch.setattr(
        sys, "argv", ["price-repair-audit", "--history-root", str(root), "--work-root", str(work)],
    )

    assert price_repair_audit.Main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "passed"
    assert report["summary"]["gap_days_before"] == 1
    assert report["summary"]["gap_days_after"] == 0
    assert report["summary"]["old_snapshot_unchanged"] is True
    assert report["summary"]["active_data_written"] is False
    assert len(baseline.calls) == 1
    assert _file_hashes(root) == source_before

    policy = SqlitePolicyFixture()
    control = HistoryControlIndex(work / "history" / "catalog.sqlite3", sqlite_policy=policy)
    control.initialize()
    published = control.load_published_state()
    assert published is not None
    assert HistoryPriceRepair(work / "history", sqlite_policy=policy).source(
        "600001", published.snapshot.sequence,
    ) == "baostock"

    assert price_repair_audit.Main() == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["status"] == "passed"
    assert repeated["summary"]["supplier_stock_requests"] == 0
    assert len(baseline.calls) == 1
    assert _file_hashes(root) == source_before

    source_reference = HistoryControlIndex(root / "catalog.sqlite3", sqlite_policy=policy).reference_path()
    for alias in ("history", "reference", "catalog"):
        alias_work = tmp_path / f"alias-{alias}"
        alias_work.mkdir()
        if alias == "catalog":
            (alias_work / "history").mkdir()
            os.link(root / "catalog.sqlite3", alias_work / "history" / "catalog.sqlite3")
        else:
            target = root if alias == "history" else source_reference.parent
            (alias_work / alias).symlink_to(target, target_is_directory=True)
        monkeypatch.setattr(
            sys, "argv", ["price-repair-audit", "--history-root", str(root), "--work-root", str(alias_work)],
        )
        with pytest.raises(ValueError, match="audit_work_root_(overlaps_source|contains_alias)"):
            price_repair_audit.Main()
        assert len(baseline.calls) == 1
        assert _file_hashes(root) == source_before


@pytest.mark.parametrize("relation", ("parent", "same", "child"))
def test_audit_rejects_work_roots_overlapping_source_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], relation: str,
) -> None:
    root = tmp_path / "history"
    work = root.parent if relation == "parent" else root if relation == "same" else root / "nested"
    monkeypatch.setattr(
        sys, "argv", ["price-repair-audit", "--history-root", str(root), "--work-root", str(work)],
    )
    with pytest.raises(SystemExit) as captured:
        price_repair_audit.Main()
    assert captured.value.code == 2
    assert "work root must not overlap source history" in capsys.readouterr().err
    assert not root.exists()
