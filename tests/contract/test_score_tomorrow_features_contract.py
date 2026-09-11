from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_batch_two_contract_freezes_five_point_in_time_feature_families() -> None:
    strategy = (ROOT / "docs/01_评分逻辑.md").read_text(encoding="utf-8")
    domain = (ROOT / "src/trader/domain/research/tomorrow_features.py").read_text(encoding="utf-8")
    application = (ROOT / "src/trader/application/research/tomorrow_features.py").read_text(encoding="utf-8")
    models = (ROOT / "src/trader/application/research/tomorrow_feature_contracts.py").read_text(encoding="utf-8")
    combined = domain + application + models

    for token in (
        "published_at",
        "industry_effective_at",
        "production_authority=false",
    ):
        assert token in strategy
    for token in (
        "score_tomorrow_point_in_time_features",
        "residual_reversal",
        "residual_momentum",
        "overnight",
        "intraday",
        "tail",
    ):
        assert token in combined
    assert "TomorrowPointInTimeFeatureBuilder" in combined
    for forbidden in ("trader.bootstrap", "trader.web", "requests", "sqlite3"):
        assert forbidden not in combined


def test_tomorrow_feature_modules_do_not_import_production_or_io_boundaries() -> None:
    domain = (ROOT / "src/trader/domain/research/tomorrow_features.py").read_text(encoding="utf-8")
    application = (ROOT / "src/trader/application/research/tomorrow_features.py").read_text(encoding="utf-8")

    for forbidden in ("trader.infra", "trader.web", "flask", "deepseek", "requests", "sqlite3"):
        assert forbidden not in domain.lower()
        assert forbidden not in application.lower()
