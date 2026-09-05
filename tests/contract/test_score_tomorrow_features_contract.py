from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_batch_two_contract_freezes_five_point_in_time_feature_families() -> None:
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    for token in (
        "score_tomorrow_point_in_time_features",
        "residual_reversal",
        "residual_momentum",
        "overnight",
        "intraday",
        "tail",
        "published_at",
        "industry_effective_at",
        "production_authority=false",
    ):
        assert token in strategy
    assert "ScoreTomorrowPointInTimeFeatures" in design
    assert "不接入 `bootstrap.py`、HTTP、调度、活动运行库、正式决策或 DeepSeek" in design


def test_tomorrow_feature_modules_do_not_import_production_or_io_boundaries() -> None:
    domain = (ROOT / "src/trader/domain/research/tomorrow_features.py").read_text(encoding="utf-8")
    application = (ROOT / "src/trader/application/research/tomorrow_features.py").read_text(encoding="utf-8")

    for forbidden in ("trader.infra", "trader.web", "flask", "deepseek", "requests", "sqlite3"):
        assert forbidden not in domain.lower()
        assert forbidden not in application.lower()
