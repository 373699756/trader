"""Small relational fixtures for source-bounded queries, not full publication qualification."""

from contextlib import contextmanager
from datetime import date
import os
import sys
from pathlib import Path

import pytest

from scripts.core.catalog import SelectionArguments
from scripts.download.history import history_publication_audit as entry
from scripts.download.history import price_percentage_units as audit
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments
from trader.download.infra import history_prices, qfq_sqlite
from trader.download.infra.history_control_rows import HISTORY_CONTROL_SCHEMA
from trader.download.infra.price_repair_attempts import PRICE_REPAIR_ATTEMPT_SQL
from trader.infra.sqlite_codec import EncodeDate
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

_DAY = date(2026, 9, 9)
_ENCODED = EncodeDate(_DAY)


def _seed(root: Path, *, history_codes=("600001",)):
    history = root / "history"
    history.mkdir()
    with SqliteSession(history / "catalog.sqlite3") as connection, SqliteTransaction(connection):
        for statement in (*HISTORY_CONTROL_SCHEMA.statements, PRICE_REPAIR_ATTEMPT_SQL):
            connection.run(statement)
        connection.run("INSERT INTO history_snapshot VALUES(2,?,?,?,?,?,?)", (b"a" * 32, _ENCODED, _ENCODED,
                                                                             b"b" * 32, b"c" * 32, b"d" * 32))
        connection.run("INSERT INTO published_snapshot VALUES(1,?,2)", (b"a" * 32,))
        connection.run("INSERT INTO snapshot_partition VALUES(2,2026,9,'prices_2026.sqlite3',?,1,1)", (b"e" * 32,))
        for code in history_codes:
            connection.run("INSERT INTO price_repair_attempt VALUES(?,?,'fixture','resolved','repaired','baostock')",
                           (code, _ENCODED))
    with SqliteSession(history / "prices_2026.sqlite3") as connection, SqliteTransaction(connection):
        for statement in history_prices._SCHEMA.statements:
            connection.run(statement)
        for code in history_codes:
            for sequence, pct in ((1, 0.016), (2, 1.6)):
                revision = f"fixture-{sequence}"
                connection.run(
                    "INSERT INTO daily_price_revision(trade_date,code,revision_id,first_seen_sequence,board,"
                    "status,raw_present,raw_close,raw_preclose,raw_change_pct,raw_turnover_pct,qfq_present,number_flags,content_hash) "
                    "VALUES(?,?,?,?,'main','qfq_missing',1,10.16,10.0,?,0.0123,0,0,?)",
                    (_ENCODED, code, revision, sequence, pct, b"f" * 32),
                )
                connection.run("INSERT INTO daily_price_observation VALUES(?,?,?,?)", (_ENCODED, code, sequence, revision))
    market = root / "daily_window.sqlite3"
    with SqliteSession(market) as connection, SqliteTransaction(connection):
        for statement in qfq_sqlite._SCHEMA.statements:
            connection.run(statement)
        connection.run("INSERT INTO window_publication VALUES('v2','fixture-v2')")
    return history, market


def _market_price(path, code, source, *, pct=0.02, close=10.2, preclose=10.0, receipt=False):
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO window_binding VALUES('v2',?,?,?,?,?)", (code, b"g" * 32, source, _ENCODED, _ENCODED))
        connection.run("INSERT INTO window_population VALUES('v2',?)", (code,))
        connection.run(
            "INSERT INTO daily_price(code,trade_date,raw_close,raw_preclose,raw_change_pct,raw_turnover_pct,number_flags) "
            "VALUES(?,?,?,?,?,0.027,0)", (code, _ENCODED, close, preclose, pct),
        )
        if receipt:
            connection.run("INSERT INTO price_repair_attempt VALUES(?,?,'fixture','resolved','repaired','baostock')",
                           (code, _ENCODED))


def _run(history, market, *, budget=None):
    return audit.AuditPricePercentageUnits(
        history, market_path=market, sqlite_policy=SqlitePolicyFixture(), budget=budget or entry.PriceAuditBudget(15),
    )


def _files(root):
    return {
        path: ((stamp.st_dev, stamp.st_ino, stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns), path.read_bytes())
        for path in root.rglob("*") if path.is_file() for stamp in (path.stat(),)
    }


def test_named_published_revision_and_only_explicit_market_sources_are_read_without_mutation(tmp_path, monkeypatch):
    history, market = _seed(tmp_path)
    _market_price(market, "600002", "baostock:fixture")
    _market_price(market, "600003", "tencent")
    _market_price(market, "600004", "tencent_then_baostock_repair")
    _market_price(market, "600005", "tencent", receipt=True)
    before = _files(tmp_path)
    statements = []
    original = audit.SqliteSession

    @contextmanager
    def traced(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            connection.observe_statements(statements.append)
            yield connection

    monkeypatch.setattr(audit, "SqliteSession", traced)
    monkeypatch.setattr(entry.PriceAuditBudget, "connection", lambda *_: pytest.fail("no raw SQLite audit connection"))
    monkeypatch.setattr(entry, "_hash_file", lambda *_: pytest.fail("no file hashing"))
    report = _run(history, market)
    summary = report["summary"]
    result = summary["percentage_unit_audit"]
    assert summary["source_data_writes"] == summary["network_requests"] == 0
    assert result["source_stat_unchanged"] and not result["full_population_verified"]
    samples = result["samples"]
    assert {sample["identity"]["code"] for sample in samples} == {"600001", "600002", "600005"}
    first = next(sample for sample in samples if sample["identity"]["dataset"] == "history")
    assert first["identity"]["sequence"] == first["identity"]["observation_sequence"] == 1
    assert first["identity"]["revision"] == "fixture-1"
    assert first["identity"]["year"] == 2026 and first["identity"]["day"] == _DAY.isoformat()
    assert first["stored_pct"] == 0.016 and first["stored_turnover"] == 0.0123
    assert first["assessment"] == "suspected_shrunk_100x"
    assert all(sample["assessment"] == "suspected_shrunk_100x" for sample in samples)
    assert result["turnover_assessment"] == "source_evidence_only_no_numeric_rescaling_basis"
    assert _files(tmp_path) == before
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
                   or "INTEGRITY_CHECK" in sql.upper() or "QUICK_CHECK" in sql.upper() for sql in statements)


@pytest.mark.parametrize(("pct", "close", "preclose", "assessment", "reason"), (
    (2.0, 10.2, 10.0, "consistent", "raw_return_comparison"),
    (0.03, 10.2, 10.0, "other_mismatch", "raw_return_comparison"),
    (0.0, 10.0, 10.0, "unassessable", "zero_change_cannot_identify_unit"),
    (0.02, 10.2, None, "unassessable", "preclose_missing_or_invalid"),
    (None, 10.2, 10.0, "unassessable", "stored_pct_missing_or_invalid"),
))
def test_percentage_evidence_never_treats_zero_missing_or_other_mismatch_as_a_rescaling_instruction(
    tmp_path, pct, close, preclose, assessment, reason,
):
    history, market = _seed(tmp_path, history_codes=())
    _market_price(market, "600002", "baostock:fixture", pct=pct, close=close, preclose=preclose)
    sample, = _run(history, market)["summary"]["percentage_unit_audit"]["samples"]
    assert sample["assessment"] == assessment and sample["reason"] == reason
    assert sample["stored_pct"] == pct and sample["stored_turnover"] == 0.027


def test_fixed_cap_explicitly_retains_unvisited_evidence_and_never_claims_full_population(tmp_path):
    history, market = _seed(tmp_path, history_codes=tuple(f"{600000 + offset:06d}" for offset in range(513)))
    report = _run(history, market)
    result = report["summary"]["percentage_unit_audit"]
    assert report["status"] == "degraded" and result["examined_prices"] == 512
    assert result["fixed_price_limit"] == 512 and not result["full_population_verified"]
    assert "history_baostock_receipts_beyond_fixed_limit" in result["unvisited"]
    assert "market_not_visited_price_limit" in result["unvisited"]


def test_budget_expiry_keeps_collected_evidence_and_read_only_stat_proof(tmp_path, monkeypatch):
    history, market = _seed(tmp_path, history_codes=("600001", "600002"))
    clock = [0.0]
    budget = entry.PriceAuditBudget(15, clock=lambda: clock[0])
    original = audit._classify

    def expire_after_first(*args):
        result = original(*args)
        clock[0] = 16.0
        return result

    monkeypatch.setattr(audit, "_classify", expire_after_first)
    result = _run(history, market, budget=budget)["summary"]["percentage_unit_audit"]
    assert result["interrupted"] == "budget_exhausted" and result["examined_prices"] == 1
    assert result["source_stat_unchanged"] and not result["failures"]


def test_sqlite_budget_interrupt_does_not_claim_an_empty_baostock_population(tmp_path):
    history, market = _seed(tmp_path, history_codes=())
    with SqliteSession(history / "catalog.sqlite3") as connection, SqliteTransaction(connection):
        connection.run_many(
            "INSERT INTO price_repair_attempt VALUES(?,?,'fixture','resolved','repaired','tencent')",
            ((f"{600000 + index:06d}", _ENCODED) for index in range(1000)),
        )

    class InterruptingBudget(entry.PriceAuditBudget):
        def progress(self):
            self.interrupted = "budget_exhausted"
            return 1

    report = _run(history, market, budget=InterruptingBudget(15))
    result = report["summary"]["percentage_unit_audit"]
    assert report["status"] == "degraded" and result["interrupted"] == "budget_exhausted"
    assert result["examined_prices"] == 0 and result["source_stat_unchanged"]
    assert "remaining_evidence_not_visited:budget_exhausted" in result["unvisited"]


def test_changed_source_stat_invalidates_the_evidence_without_any_audit_data_write(tmp_path, monkeypatch):
    history, market = _seed(tmp_path)
    original = audit._classify

    def change_fixture_stamp(*args):
        result = original(*args)
        path = history / "prices_2026.sqlite3"
        stamp = path.stat()
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1))
        return result

    monkeypatch.setattr(audit, "_classify", change_fixture_stamp)
    report = _run(history, market)
    result = report["summary"]["percentage_unit_audit"]
    assert report["status"] == "failed" and not result["source_stat_unchanged"]
    assert "percentage_audit_source_changed" in result["failures"]
    assert report["summary"]["source_data_writes"] == 0


def test_unified_history_publication_cli_forwards_percentage_mode_and_enforces_its_bound(tmp_path, monkeypatch):
    from scripts.download.history import cli

    _seed(tmp_path)
    monkeypatch.setattr(cli.sqlite3, "connect", lambda *args, **kwargs: pytest.fail("percentage CLI must not open raw SQLite"))
    options = ParseBusinessOptions(ParseToolArguments([
        *SelectionArguments("history-publication"), "--percentage-unit-audit", "--history-root", str(tmp_path / "history"),
        "--market-path", str(tmp_path / "daily_window.sqlite3"),
    ]))
    command, = BuildToolCommands(options)
    assert "--percentage-unit-audit" in command.argv and "--price-field-audit" not in command.argv
    assert command.argv[command.argv.index("--audit-budget-seconds") + 1] == "15.0"
    assert command.argv[command.argv.index("--market-path") + 1] == str(tmp_path / "daily_window.sqlite3")
    ParseBusinessOptions(ParseToolArguments([
        *SelectionArguments("history-publication"), "--percentage-unit-audit", "--history-root", str(tmp_path / "history"),
        "--market-path", str(tmp_path / "daily_window.sqlite3"), "--report", str(tmp_path.parent / "percentage-report.json"),
    ]))
    with pytest.raises(SystemExit):
        ParseBusinessOptions(ParseToolArguments([
            *SelectionArguments("history-publication"), "--percentage-unit-audit", "--history-root", str(tmp_path / "history"),
            "--market-path", str(tmp_path / "daily_window.sqlite3"), "--report", str(tmp_path / "daily_window.sqlite3"),
        ]))
    for extra in (("--audit-budget-seconds", "16"), ("--price-field-audit",), ("--audit-checkpoint", str(tmp_path / "proof"))):
        with pytest.raises(ValueError, match="percentage unit audit"):
            ParseBusinessOptions(ParseToolArguments([
                *SelectionArguments("history-publication"), "--percentage-unit-audit", *extra,
            ]))


def test_module_entry_selects_percentage_mode_without_running_the_full_publication_or_field_scan(tmp_path, monkeypatch):
    from trader import bootstrap
    from trader.infra import settings

    history, market = _seed(tmp_path)
    reports = []
    monkeypatch.setattr(bootstrap, "BuildSqlitePolicy", lambda _: SqlitePolicyFixture())
    monkeypatch.setattr(settings, "LoadRuntimeSettings", lambda _: object())
    monkeypatch.setattr(entry, "emit_report", reports.append)
    monkeypatch.setattr(entry, "AuditHistoryPublication", lambda *args, **kwargs: pytest.fail("no full audit"))
    monkeypatch.setattr(entry, "AuditHistoryPriceFields", lambda *args, **kwargs: pytest.fail("no field scan"))
    monkeypatch.setattr(sys, "argv", [
        "history_publication_audit", "--history-root", str(history), "--runtime-config", str(tmp_path / "runtime.json"),
        "--percentage-unit-audit", "--market-path", str(market),
    ])
    assert entry._main() == 0
    report, = reports
    assert report["summary"]["percentage_unit_audit"]["budget_seconds"] == 15.0
    assert report["summary"]["source_data_writes"] == report["summary"]["network_requests"] == 0
