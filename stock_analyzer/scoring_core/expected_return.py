from __future__ import annotations

from typing import Dict, Iterable, List

from ..expected_return_model import predict_expected_return
from ..normalization import coerce_number


__all__ = [
    "_attach_expected_return_prediction",
    "_expected_return_rank_active",
    "_ranking_gate_score",
]


def _attach_expected_return_prediction(
    strategy_name: str,
    rows: List[Dict[str, object]],
    samples: Iterable[Dict[str, object]] = None,
    use_ranking: bool = False,
) -> List[Dict[str, object]]:
    enriched = predict_expected_return(strategy_name, rows, samples=samples)
    if not use_ranking:
        return enriched
    if not enriched or any(str(row.get("model_confidence") or "") != "ready" for row in enriched):
        return enriched
    if any(coerce_number(row.get("predicted_net_return"), None) is None for row in enriched):
        return enriched
    for index, row in enumerate(enriched, start=1):
        row["legacy_score_rank"] = index
    enriched.sort(
        key=lambda item: (
            coerce_number(item.get("predicted_net_return"), float("-inf")),
            coerce_number(item.get("predicted_probability"), 0.0),
            coerce_number(item.get("score")),
        ),
        reverse=True,
    )
    for index, row in enumerate(enriched, start=1):
        row["expected_return_rank"] = index
        row["ranking_source"] = "expected_return_predicted_net_return"
    return enriched


def _expected_return_rank_active(row: Dict[str, object]) -> bool:
    return (
        str(row.get("model_confidence") or "").strip().lower() == "ready"
        and str(row.get("ranking_source") or "").strip() == "expected_return_predicted_net_return"
    )


def _ranking_gate_score(row: Dict[str, object]) -> float:
    return coerce_number(row.get("score"))
