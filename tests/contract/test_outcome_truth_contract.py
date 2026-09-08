from __future__ import annotations

import ast
from pathlib import Path

from trader.domain.outcome.models import outcome_horizons
from trader.domain.recommendation.models import Strategy

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = ROOT / "src/trader/domain/outcome/evaluation.py"
APPLICATION = ROOT / "src/trader/application/outcomes/outcome_settlement.py"
PERSISTENCE = ROOT / "src/trader/infra/persistence/outcomes.py"


def test_d25_horizons_have_one_domain_owner_and_include_t4() -> None:
    application = APPLICATION.read_text(encoding="utf-8")
    persistence = PERSISTENCE.read_text(encoding="utf-8")

    assert outcome_horizons(Strategy.D25) == (2, 3, 4, 5)
    assert "outcome_horizons(target.strategy)" in application
    assert "outcome_horizons(strategy)" in persistence
    assert "(2, 3, 5)" not in application
    assert "(2, 3, 5)" not in persistence


def test_canonical_evaluator_is_pure_domain_code() -> None:
    tree = ast.parse(DOMAIN.read_text(encoding="utf-8"), filename=str(DOMAIN))
    imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }

    assert "class CanonicalOutcomeEvaluator" in DOMAIN.read_text(encoding="utf-8")
    assert not any(
        name.startswith(("trader.application", "trader.infra", "trader.web", "flask", "requests")) for name in imports
    )


def test_persistence_returns_only_missing_horizons_for_restart_recovery() -> None:
    persistence = PERSISTENCE.read_text(encoding="utf-8")

    assert "pending_horizons = self._pending_horizons" in persistence
    assert "pending_horizons," in persistence
    assert "tuple(horizon for horizon in horizons if horizon not in settled)" in persistence
