from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_point_in_time_dataset_is_production_isolated_and_uses_canonical_outcomes() -> None:
    application = (ROOT / "src/trader/application/research/point_in_time_dataset.py").read_text(encoding="utf-8")
    domain = (ROOT / "src/trader/domain/research/point_in_time_dataset.py").read_text(encoding="utf-8")

    assert "CanonicalOutcomeEvaluator" in application
    assert "select_scored" in application
    assert 'POINT_IN_TIME_BENCHMARK_ID = "point_in_time_local_only_equal_weight"' in domain
    assert "terminal_holdout_opened: bool = False" in domain
    assert "production_authority: bool = False" in domain
    assert "trader.infra" not in application
    assert "trader.web" not in application
    assert "DeepSeek" not in application


def test_point_in_time_dataset_has_no_versioned_non_scoring_identity() -> None:
    paths = (
        ROOT / "src/trader/domain/research/point_in_time_dataset.py",
        ROOT / "src/trader/application/research/point_in_time_dataset.py",
        ROOT / "src/trader/infra/research/point_in_time_dataset_artifacts.py",
    )

    for path in paths:
        source = path.read_text(encoding="utf-8").casefold()
        assert "dataset_v" not in source
        assert "dataset-v" not in source
