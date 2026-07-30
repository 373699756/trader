"""Project stable security identity from free realtime observations."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace

from trader.application.cache import canonical_json_bytes
from trader.infra.market_data.merge_quote import source_name
from trader.infra.market_data.observations import SourceObservation

_SECURITY_REFERENCE_FIELDS = frozenset(
    {
        "board",
        "board_reliability",
        "exchange",
        "listing_date",
        "listing_age_sessions",
        "is_relisted_first_session",
        "is_delisting_period_first_session",
        "has_price_limit",
        "exchange_limit_pct",
        "strategy_hot_cap_pct",
        "rule_version",
        "rule_effective_date",
    }
)


def security_reference_observations(
    observations: Sequence[SourceObservation],
) -> tuple[SourceObservation, ...]:
    references: list[SourceObservation] = []
    for observation in observations:
        fields = {
            key: value
            for key, value in observation.fields.items()
            if key in _SECURITY_REFERENCE_FIELDS and value is not None
        }
        if "board" not in fields and "listing_date" not in fields:
            continue
        references.append(
            replace(
                observation,
                source=f"{source_name(observation.source)}_security_master",
                fields=fields,
                missing_reasons={},
                payload_hash=hashlib.sha256(canonical_json_bytes(fields)).hexdigest(),
            )
        )
    return tuple(references)


__all__ = ["security_reference_observations"]
