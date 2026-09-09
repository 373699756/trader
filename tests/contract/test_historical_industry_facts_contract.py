from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_historical_industry_section_has_one_fail_closed_checkpoint_owner() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    section = work[work.index("#### `historical_industry_facts`") : work.index("#### `historical_minute")]

    assert "状态：`in_progress: checkpoint_pushed`" in section
    assert "下次必须先复跑最终报告" in section
    assert "5453" in section
    assert "baostock_archived_industry" in section
    assert "tushare_index_member_all" in section
    assert "queried_at" in section
    assert "逐事实 hash" in section
    assert "日线分片保持只读" in section
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
