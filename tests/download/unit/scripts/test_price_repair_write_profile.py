from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from scripts.core.catalog import SelectionArguments
from scripts.download.history import price_repair_audit
from tests.helpers.download.configuration import HistorySyncConfigurationFixture
from tests.helpers.download.history.infra.history_sync import FakeSupplier, NOW
from tests.helpers.download.history_fixtures import RunHistoryFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments
from trader.download.domain.published_history import ProjectHistoryCell
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.published_history_codec import EncodePublishedHistoryCell
from trader.infra.sqlite_codec import EncodeDate
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


@pytest.mark.parametrize("changed", [True, False])
def test_repair_write_profile_keeps_source_and_measures_existing_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool,
) -> None:
    days = (date(2026, 9, 9), date(2026, 9, 10))
    root = tmp_path / "source" / "history"
    configuration = HistorySyncConfigurationFixture(root, reference_root=root.parent / "reference", sessions=2,
                                                     reread_sessions=1)
    assert RunHistoryFixture(configuration, FakeSupplier(days), clock=lambda: NOW).state == "completed"
    policy = SqlitePolicyFixture()
    state = HistoryControlIndex(root / "catalog.sqlite3", sqlite_policy=policy).load_published_state()
    prices = HistoryDirectPrices(root, state.snapshot.sequence, state.snapshot,
                                 HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=policy)
    cells = tuple(ProjectHistoryCell(cell) for cell in prices.read("600001", days))
    if changed:
        cells = tuple(replace(cell, qfq=replace(cell.qfq, open_price=cell.qfq.open_price + 1,
                                             high_price=cell.qfq.high_price + 1,
                                             low_price=cell.qfq.low_price + 1,
                                             close_price=cell.qfq.close_price + 1)) for cell in cells)
    with SqliteSession(root / "catalog.sqlite3", sqlite_policy=policy) as connection, SqliteTransaction(connection):
        connection.run_many("INSERT INTO price_replacement VALUES(?,?,?,?,?,?)", (
            ("600001", EncodeDate(cell.trade_date), state.snapshot.sequence + 1, "main", "baostock", EncodePublishedHistoryCell(cell))
            for cell in cells
        ))
    before = {path.name: path.read_bytes() for path in root.glob("*.sqlite3")}
    monkeypatch.setattr(price_repair_audit, "BaoStockHistorySupplier", lambda *args, **kwargs: pytest.fail("network"))
    work = tmp_path / "profile"
    result = price_repair_audit._profile_repair_year(root, work, 2026, "600001", sqlite_policy=policy)
    assert result["status"] == ("passed" if changed else "empty")
    summary = result["summary"]
    assert summary["candidate_year_rows"] == 2
    assert summary["changed_rows"] == (2 if changed else 0)
    assert summary["network_requests"] == 0 and summary["source_stat_unchanged"]
    assert summary["database_measurement"]["connections"] >= 1
    phases = {phase["phase"] for phase in summary["publication_measurement"]["phases"]}
    assert {"source_backup", "repair_changed_revisions", "repair_replace_total", "repair_close"} <= phases
    if changed:
        assert {"repair_write", "repair_commit"} <= phases
    assert before == {path.name: path.read_bytes() for path in root.glob("*.sqlite3")}
    with pytest.raises(ValueError, match="new_external"):
        price_repair_audit._profile_repair_year(root, work, 2026, "600001", sqlite_policy=policy)


def test_repair_profile_requires_new_external_directory_and_forwards_scope(tmp_path: Path) -> None:
    args = [*SelectionArguments("price-repair-audit"), "--history-root", str(tmp_path / "history"),
            "--repair-profile-year", "2026", "--repair-profile-code", "600039"]
    with pytest.raises(ValueError, match="database-probe-directory"):
        ParseBusinessOptions(ParseToolArguments(args))
    options = ParseBusinessOptions(ParseToolArguments([*args, "--database-probe-directory", str(tmp_path / "new")]))
    argv = BuildToolCommands(options)[0].argv
    assert argv[argv.index("--repair-profile-year") + 1] == "2026"
    assert argv[argv.index("--repair-profile-code") + 1] == "600039"
    assert "--work-root" in argv


def test_default_audit_help_does_not_import_optional_measurement(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import importlib
    import sys

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {
            "scripts.download.history.history_publication_measurement",
            "scripts.infra.database.database_measurement",
        }:
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    importlib.reload(price_repair_audit)
    monkeypatch.setattr(sys, "argv", ["price_repair_audit", "--help"])
    with pytest.raises(SystemExit) as result:
        price_repair_audit.Main()
    assert result.value.code == 0
