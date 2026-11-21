"""The price event predicted by Tomorrow and verified at settlement."""

from decimal import Decimal
from enum import Enum


TOMORROW_CLOSE_GAIN_TARGET = "next_close_gain_at_least_5_percent"


class TomorrowCloseGain(str, Enum):
    HIT = "hit"
    MISS = "miss"
    PENDING = "pending"


def EvaluateTomorrowCloseGain(
    signal_close: Decimal | None,
    next_close: Decimal | None,
    *,
    confirmed_suspended: bool = False,
    next_raw_reference: Decimal | None = None,
    next_raw_close: Decimal | None = None,
) -> TomorrowCloseGain:
    """Classify comparable closes without rounding either price or the return.

    The caller supplies prices on a verified common basis and confirms any
    suspension from the target session. Missing evidence remains pending.
    """
    if signal_close is None or not signal_close.is_finite() or signal_close <= 0:
        return TomorrowCloseGain.PENDING
    if confirmed_suspended:
        return TomorrowCloseGain.MISS
    if next_close is None or not next_close.is_finite() or next_close <= 0:
        return TomorrowCloseGain.PENDING

    hit = _meets_threshold(signal_close, next_close)
    if next_raw_reference is not None and next_raw_close is not None and (
        not next_raw_reference.is_finite()
        or next_raw_reference <= 0
        or not next_raw_close.is_finite()
        or next_raw_close <= 0
        or _meets_threshold(next_raw_reference, next_raw_close) != hit
    ):
        return TomorrowCloseGain.PENDING
    return TomorrowCloseGain.HIT if hit else TomorrowCloseGain.MISS


def _meets_threshold(signal_close: Decimal, next_close: Decimal) -> bool:
    signal_numerator, signal_denominator = signal_close.as_integer_ratio()
    next_numerator, next_denominator = next_close.as_integer_ratio()
    return 100 * next_numerator * signal_denominator >= 105 * signal_numerator * next_denominator
