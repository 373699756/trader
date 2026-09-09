from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_batch_three_contract_freezes_walk_forward_models_and_calibration() -> None:
    strategy = " ".join((ROOT / "docs/01_评分逻辑.md").read_text(encoding="utf-8").split())
    models = (ROOT / "src/trader/application/research/shadow_model_models.py").read_text(encoding="utf-8")
    scoring = (ROOT / "src/trader/application/research/shadow_models.py").read_text(encoding="utf-8")
    artifacts = (ROOT / "src/trader/infra/research/shadow_model_artifacts.py").read_text(encoding="utf-8")
    combined = models + scoring + artifacts
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "production_authority=false" in strategy
    for token in (
        "score_tomorrow_shadow_report",
        "expanding",
        "rolling_252",
        "max_depth=3",
        "num_leaves=7",
        "min_data_in_leaf=20",
        "affine",
        "platt",
    ):
        assert token in combined.replace('"', "").replace(": ", "=")
    assert '(("tomorrow", 1), ("d25", 25))' in models
    assert "ScoreTomorrowShadowModels" in combined
    assert "ShadowModelArtifactStore" in combined
    assert '"lightgbm>=4.7,<5"' in pyproject


def test_shadow_model_modules_remain_outside_production_and_io_boundaries() -> None:
    bootstrap = (ROOT / "src/trader/bootstrap.py").read_text(encoding="utf-8").lower()
    application = (ROOT / "src/trader/application/research/shadow_models.py").read_text(encoding="utf-8").lower()
    domain = (ROOT / "src/trader/domain/research/shadow_calibration.py").read_text(encoding="utf-8").lower()
    lightgbm = (ROOT / "src/trader/infra/research/lightgbm_shadow.py").read_text(encoding="utf-8").lower()

    assert "scoretomorrowshadowmodels" not in bootstrap
    for forbidden in ("trader.infra", "trader.web", "flask", "deepseek", "requests", "sqlite3"):
        assert forbidden not in application
        assert forbidden not in domain
    assert "import lightgbm" in lightgbm
