from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_batch_three_contract_freezes_walk_forward_models_and_calibration() -> None:
    strategy = " ".join((ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8").split())
    design = " ".join((ROOT / "docs/software-business-design.md").read_text(encoding="utf-8").split())
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for token in (
        "score_tomorrow_shadow_report",
        "expanding",
        "rolling_252",
        "Tomorrow embargo=1",
        "D25 embargo=25",
        "max_depth=3",
        "num_leaves=7",
        "min_data_in_leaf=20",
        "仿射校准",
        "Platt 校准",
        "完整逐日逐股预测",
        "production_authority=false",
    ):
        assert token in strategy
    assert "ScoreTomorrowShadowModels" in design
    assert "ShadowModelArtifactStore" in design
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
