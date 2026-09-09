from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_historical_industry_section_has_one_fail_closed_checkpoint_owner() -> None:
    report = (ROOT / "docs/reports/historical-industry-facts-2026-09-09.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")

    for token in (
        "historical_data_insufficient",
        "按用户明确要求暂不恢复正式归档",
        "daily_archive_invalid",
        "最终报告 schema 实物复跑：未验证",
        "5453",
        "查询时间",
        "逐事实 hash",
    ):
        assert token in report
    assert "historical_industry_dataset" in design
    assert "scripts/audit_historical_industry_facts.py" in design


def test_industry_audit_is_separate_from_daily_archive_and_has_no_automatic_authority() -> None:
    domain = ROOT / "src/trader/domain/research/historical_industry_facts.py"
    adapter = ROOT / "src/trader/infra/research/historical_industry_archive.py"
    script = ROOT / "scripts/audit_historical_industry_facts.py"

    assert domain.is_file()
    assert adapter.is_file()
    assert script.is_file()
    combined = domain.read_text(encoding="utf-8") + adapter.read_text(encoding="utf-8")
    assert "production_authority" in combined
    assert "daily_cells" not in script.read_text(encoding="utf-8")
    assert "query_history_k_data_plus" not in combined
    assert "stock_analyzer" not in combined
