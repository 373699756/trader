from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


RETIRED_HISTORY_PATHS = (
    "scripts/runtime_diagnostics/history_archive_plan.py",
    "src/trader/domain/research/baostock_active_archive.py",
    "src/trader/application/research/baostock_daily.py",
    "src/trader/application/research/baostock_history_runtime.py",
    "src/trader/domain/research/historical_effective_facts.py",
    "src/trader/infra/research/baostock_active_archive.py",
    "src/trader/infra/research/baostock_active_training.py",
    "src/trader/infra/research/baostock_archive_plan.py",
    "src/trader/infra/research/baostock_catalog.py",
    "src/trader/infra/research/baostock_daily.py",
    "src/trader/infra/research/baostock_daily_codec.py",
    "src/trader/infra/research/baostock_daily_serialization.py",
    "src/trader/infra/research/baostock_history_legacy.py",
    "src/trader/infra/research/baostock_history_messages.py",
    "src/trader/infra/research/baostock_history_runtime.py",
    "src/trader/infra/research/baostock_history_status.py",
    "src/trader/infra/research/baostock_increment_runtime.py",
    "src/trader/infra/research/baostock_partition_archive.py",
    "src/trader/infra/research/baostock_training_dataset.py",
    "src/trader/infra/research/historical_effective_facts.py",
)


def test_stage_g_physically_removes_the_parent_increment_history_chain() -> None:
    for relative in RETIRED_HISTORY_PATHS:
        assert not (ROOT / relative).exists(), relative


def test_current_docs_and_tools_only_name_the_monthly_history_owner() -> None:
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    replay = (ROOT / "docs/04_策略回溯.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    diagnostic = (ROOT / "scripts/diagnose_runtime.py").read_text(encoding="utf-8")

    active_text = "\n".join((design, plan, replay, readme, makefile, diagnostic))
    for retired in (
        "history-plan",
        "history_archive_plan",
        "父加增量",
    ):
        assert retired not in active_text
    for current in (
        "data/history/baostock/control.sqlite3",
        "partitions/YYYY/MM.sqlite3",
        "SQLiteHistoryControlRepository",
        "SQLiteHistoryMonthlyArchive",
    ):
        assert current in active_text
    assert "scripts/convert_baostock_history.py" in active_text


def test_retained_converter_is_isolated_from_the_retired_download_chain() -> None:
    converter = (ROOT / "scripts/convert_baostock_history.py").read_text(encoding="utf-8")
    supplier = (ROOT / "src/trader/infra/research/baostock_gap_supplier.py").read_text(encoding="utf-8")

    assert "baostock_gap_supplier" in converter
    for retired_import in (
        "baostock_active_archive",
        "baostock_history_runtime",
        "baostock_increment_runtime",
        "baostock_history_messages",
    ):
        assert retired_import not in supplier


def test_shared_baostock_daily_contract_has_no_retired_archive_codec() -> None:
    source = (ROOT / "src/trader/domain/research/baostock_daily.py").read_text(encoding="utf-8")

    for retired in (
        "BAOSTOCK_LEGACY_",
        "BaoStockDailyManifest",
        "BaoStockPartitionRef",
        "BaoStockCoverageAudit",
        "BaoStockTrainingDatasetManifest",
        "build_baostock_training_dataset_manifest",
        "validate_baostock_archive_window",
    ):
        assert retired not in source
