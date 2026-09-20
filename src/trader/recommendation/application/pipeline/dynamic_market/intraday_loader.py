"""Stage-5 injected intraday load boundary."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from trader.recommendation.domain.market.models import FeatureSnapshot

IntradayLoad = Callable[[tuple[str, ...], datetime], Sequence[FeatureSnapshot]]


def load_intraday(
    codes: tuple[str, ...],
    as_of: datetime,
    loader: IntradayLoad,
) -> tuple[FeatureSnapshot, ...]:
    if len(codes) != len(set(codes)):
        raise ValueError("intraday request codes must be unique")
    loaded = tuple(loader(codes, as_of))
    loaded_codes = tuple(item.quote.code for item in loaded)
    if len(loaded_codes) != len(set(loaded_codes)) or not set(loaded_codes) <= set(codes):
        raise ValueError("intraday loader returned records outside its request")
    return loaded


__all__ = ["IntradayLoad", "load_intraday"]
