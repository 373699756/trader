from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_candidate_recall_ledger_is_offline_and_uses_fixed_research_boundaries() -> None:
    domain = (ROOT / "src/trader/domain/research/candidate_recall_ledger.py").read_text(encoding="utf-8")
    application = (ROOT / "src/trader/application/research/candidate_recall_ledger.py").read_text(encoding="utf-8")

    assert "CANDIDATE_RECALL_TOP_K = (10, 20, 50)" in domain
    for boundary in (
        "permanent_eligibility",
        "dynamic_hard_filter",
        "field_eligibility",
        "candidate_threshold",
        "board_limit",
        "scoring",
        "risk",
        "action",
        "concentration",
    ):
        assert f'"{boundary}"' in domain
    assert "PointInTimeDatasetReport" in application
    assert "trader.infra" not in application
    assert "trader.web" not in application
    assert "DeepSeek" not in application


def test_candidate_recall_ledger_has_no_versioned_non_scoring_identity() -> None:
    paths = (
        ROOT / "src/trader/domain/research/candidate_recall_ledger.py",
        ROOT / "src/trader/application/research/candidate_recall_ledger.py",
    )

    for path in paths:
        source = path.read_text(encoding="utf-8").casefold()
        assert "ledger_v" not in source
        assert "ledger-v" not in source
