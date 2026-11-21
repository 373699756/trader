from decimal import Decimal, localcontext

import pytest

from trader.recommendation.domain.outcomes.tomorrow_target import EvaluateTomorrowCloseGain, TomorrowCloseGain


@pytest.mark.parametrize(
    ("next_close", "result"),
    (
        ("104.99", TomorrowCloseGain.MISS),
        ("104.9999", TomorrowCloseGain.MISS),
        ("105.00", TomorrowCloseGain.HIT),
        ("105.01", TomorrowCloseGain.HIT),
        ("NaN", TomorrowCloseGain.PENDING),
    ),
)
def test_tomorrow_label_uses_exact_prices(next_close: str, result: TomorrowCloseGain) -> None:
    with localcontext() as context:
        context.prec = 3
        assert EvaluateTomorrowCloseGain(Decimal("100"), Decimal(next_close)) is result


def test_confirmation_distinguishes_suspension_and_missing_prices() -> None:
    assert EvaluateTomorrowCloseGain(Decimal("100"), None) is TomorrowCloseGain.PENDING
    assert EvaluateTomorrowCloseGain(Decimal("100"), None, confirmed_suspended=True) is TomorrowCloseGain.MISS
    assert EvaluateTomorrowCloseGain(None, None, confirmed_suspended=True) is TomorrowCloseGain.PENDING


def test_conflicting_source_precision_leaves_the_label_pending() -> None:
    assert EvaluateTomorrowCloseGain(
        Decimal("100"), Decimal("105"), next_raw_reference=Decimal("100"), next_raw_close=Decimal("104.99")
    ) is TomorrowCloseGain.PENDING
