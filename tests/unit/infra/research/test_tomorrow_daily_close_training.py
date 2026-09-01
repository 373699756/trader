from datetime import date, timedelta

import pytest

from trader.application.research.tomorrow_daily_close_training import DailyCloseFeatureRow
from trader.infra.research.tomorrow_daily_close_training import FixedC3BaseModelTrainer


def _rows(count: int = 80) -> tuple[DailyCloseFeatureRow, ...]:
    first = date(2024, 1, 1)
    return tuple(
        DailyCloseFeatureRow(
            trade_date=first + timedelta(days=index // 4),
            label_maturity_date=first + timedelta(days=index // 4 + 1),
            code=f"60{index % 4:04d}",
            board="main",
            feature_values=(index / 100.0, (index % 7) / 10.0),
            net_excess_returns=(0.001 + index / 100_000.0, -0.002 + index / 100_000.0, -0.007),
            filter_evidence_hash="a" * 64,
            source_row_hash=f"{index:064x}",
        )
        for index in range(count)
    )


def test_fixed_c3_trainer_is_deterministic_bounded_and_reloadable() -> None:
    trainer = FixedC3BaseModelTrainer()
    rows = _rows()

    first = trainer.fit(rows, feature_count=2)
    second = trainer.fit(rows, feature_count=2)
    first_predictions = trainer.predict(first, rows[:8])
    reloaded_predictions = trainer.predict(second, rows[:8])

    assert first == second
    assert first_predictions[0] == pytest.approx(reloaded_predictions[0])
    assert first_predictions[1] == pytest.approx(reloaded_predictions[1])
    assert first.lightgbm_best_iteration <= 80
    assert ("lightgbm", pytest.importorskip("lightgbm").__version__) in first.dependency_versions
