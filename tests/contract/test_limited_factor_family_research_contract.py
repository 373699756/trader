from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_limited_factor_family_research_is_offline_and_point_in_time_bound() -> None:
    domain = (ROOT / "src/trader/domain/research/limited_factor_family.py").read_text(encoding="utf-8")
    application = (ROOT / "src/trader/application/research/limited_factor_family.py").read_text(encoding="utf-8")

    assert "PointInTimeDatasetReport" in application
    assert "CandidateRecallReport" in application
    assert "paired_moving_block_statistics" in domain
    assert "fixed_family_holm" in domain
    assert "terminal_holdout_opened" in domain
    assert "production_authority" in domain
    assert "trader.infra" not in application
    assert "trader.web" not in application
    assert "DeepSeek" not in application


def test_limited_factor_family_has_no_non_scoring_version_identity() -> None:
    paths = (
        ROOT / "src/trader/domain/research/limited_factor_family.py",
        ROOT / "src/trader/application/research/limited_factor_family.py",
    )

    for path in paths:
        source = path.read_text(encoding="utf-8").casefold()
        assert "factor_family_v" not in source
        assert "factor-family-v" not in source
