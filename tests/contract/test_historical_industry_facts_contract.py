from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_industry_audit_is_separate_from_daily_archive_and_has_no_automatic_authority() -> None:
    domain = ROOT / "src/trader/training/domain/evaluation/historical_industry_facts.py"
    adapter = ROOT / "src/trader/training/infra/research/historical_industry_evidence.py"
    script = ROOT / "src/trader/training/entrypoints/research_evidence.py"

    assert domain.is_file()
    assert adapter.is_file()
    assert script.is_file()
    combined = domain.read_text(encoding="utf-8") + adapter.read_text(encoding="utf-8")
    assert "production_authority" in combined
    assert "daily_cells" not in script.read_text(encoding="utf-8")
    assert "query_history_k_data_plus" not in combined
    assert "stock_analyzer" not in combined
