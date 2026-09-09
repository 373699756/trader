from __future__ import annotations

import ast
from pathlib import Path

from trader.domain.outcome.models import outcome_horizons
from trader.domain.recommendation.models import Strategy

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = ROOT / "src/trader/domain/outcome/evaluation.py"
MODELS = ROOT / "src/trader/domain/outcome/models.py"
APPLICATION = ROOT / "src/trader/application/outcomes/outcome_settlement.py"
PERSISTENCE = ROOT / "src/trader/infra/persistence/outcomes.py"
HISTORY = ROOT / "src/trader/infra/market_data/history/service_history.py"
WORK = ROOT / "docs/03_工程实施.md"


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


def test_outcome_contract_owns_explicit_price_basis_and_exit_tradability() -> None:
    models = MODELS.read_text(encoding="utf-8")
    domain = DOMAIN.read_text(encoding="utf-8")
    application = APPLICATION.read_text(encoding="utf-8")
    persistence = PERSISTENCE.read_text(encoding="utf-8")
    history = HISTORY.read_text(encoding="utf-8")

    assert "class OutcomePrice" in models
    assert "qfq: OutcomePrice" in models
    assert "raw: OutcomePrice" in models
    assert "anchor_raw_price" in models
    assert "anchor_qfq_price" in models
    assert "exit_status" in models
    assert "missing_carried_forward" in models
    assert "_anchor_qfq_price" in domain
    assert "_settlement_window" in domain
    assert "expected_trade_dates=expected_dates" in application
    assert '"exit_untradable": outcome.exit_untradable' in persistence
    assert "fetch_outcome_history" in history


def test_outcome_truth_and_v3_cost_remediation_have_separate_owners() -> None:
    models = MODELS.read_text(encoding="utf-8")
    work = WORK.read_text(encoding="utf-8")

    assert "class OutcomePrice" in models
    assert "V3 训练工件重建与整组发布" in work
    assert "成本只由选择和评价层按场景扣一次" in work
