from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_historical_industry_section_has_one_fail_closed_checkpoint_owner() -> None:
    design = (ROOT / "docs/04_下载训练.md").read_text(encoding="utf-8")

    for token in (
        "historical_data_insufficient",
        "至少300只分层样本",
        "查询时间",
        "PointInTimeDataQualificationReport",
        "terminal_holdout_opened=false",
        "production_authority=false",
        "effective_from <= trade_date < effective_to",
    ):
        assert token in design


def test_industry_audit_is_separate_from_daily_archive_and_has_no_automatic_authority() -> None:
    domain = ROOT / "src/trader/training/domain/evaluation/historical_industry_facts.py"
    adapter = ROOT / "src/trader/training/infra/research/historical_industry_archive.py"
    script = ROOT / "src/trader/training/entrypoints/research_evidence.py"

    assert domain.is_file()
    assert adapter.is_file()
    assert script.is_file()
    combined = domain.read_text(encoding="utf-8") + adapter.read_text(encoding="utf-8")
    assert "production_authority" in combined
    assert "daily_cells" not in script.read_text(encoding="utf-8")
    assert "query_history_k_data_plus" not in combined
    assert "stock_analyzer" not in combined
