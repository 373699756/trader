from __future__ import annotations

import math
from datetime import date, datetime
from functools import lru_cache
from typing import Dict, Iterable, List

import pandas as pd

from . import config
from .factors import ALPHALITE_COLUMNS, ALPHALITE_META_COLUMNS
from .fundamentals import ALL_FUNDAMENTAL_COLUMNS
from .normalization import coerce_number, normalize_code, rename_known_columns
from .scoring_core.candidate_filters import (
    HARD_FILTER_LABELS,
    _candidate_base_frame,
    _candidate_filter_masks,
)


_ANNOUNCEMENT_KEYS = (
    "announcement_time",
    "announcement_date",
    "announce_time",
    "announce_date",
    "ann_date",
    "publish_time",
    "published_at",
    "公告发布时间",
    "公告日期",
)

_QUOTE_SOURCE_FIELDS = {
    "price",
    "pct_chg",
    "change",
    "volume",
    "turnover",
    "amplitude",
    "high",
    "low",
    "open",
    "prev_close",
    "volume_ratio",
    "turnover_rate",
    "speed",
    "five_min_pct",
    "sixty_day_pct",
    "ytd_pct",
    "float_market_cap",
    "market_cap",
    "pe_dynamic",
    "pb",
}
_ALPHALITE_FIELDS = set(ALPHALITE_COLUMNS) | set(ALPHALITE_META_COLUMNS)
_FUNDAMENTAL_FIELDS = set(ALL_FUNDAMENTAL_COLUMNS)
_FUNDAMENTAL_DERIVED_FIELDS = {
    "fundamental_quality_score",
    "fundamental_value_score",
    "earnings_surprise_score",
    "rating_revision_score",
}


def filter_point_in_time_fundamentals(payload: Dict[str, object], cutoff: str) -> Dict[str, object]:
    """Remove fundamentals whose public availability cannot be proven at cutoff."""
    result = dict(payload or {})
    items = (payload or {}).get("items") if isinstance(payload, dict) else {}
    kept: Dict[str, Dict[str, object]] = {}
    rejected_missing_time = 0
    rejected_future = 0
    for code, item in (items or {}).items():
        if not isinstance(item, dict):
            continue
        announcement_time = first_announcement_time(item)
        if not announcement_time:
            rejected_missing_time += 1
            continue
        if not timestamp_not_after(announcement_time, cutoff):
            rejected_future += 1
            continue
        kept[normalize_code(code)] = dict(item)
    result["items"] = kept
    result["point_in_time"] = {
        "cutoff": str(cutoff or ""),
        "kept": len(kept),
        "rejected_missing_announcement_time": rejected_missing_time,
        "rejected_future_announcement": rejected_future,
    }
    return result


def filter_point_in_time_events(payload: Dict[str, object], cutoff: str) -> Dict[str, object]:
    """Keep only event flags with a provable publication time at cutoff."""
    result = dict(payload or {})
    items = (payload or {}).get("items") if isinstance(payload, dict) else {}
    kept_items: Dict[str, Dict[str, object]] = {}
    rejected_missing_time = 0
    rejected_future = 0
    for code, item in (items or {}).items():
        if not isinstance(item, dict):
            continue
        kept_flags = []
        for flag in item.get("flags") or []:
            if not isinstance(flag, dict):
                continue
            announcement_time = first_announcement_time(flag)
            if not announcement_time:
                rejected_missing_time += 1
                continue
            if not timestamp_not_after(announcement_time, cutoff):
                rejected_future += 1
                continue
            kept_flags.append(dict(flag))
        if not kept_flags:
            continue
        penalty = min(
            coerce_number(getattr(config, "EVENT_RISK_MAX_PENALTY", 30.0), 30.0),
            sum(coerce_number(flag.get("penalty")) for flag in kept_flags),
        )
        high_count = sum(1 for flag in kept_flags if flag.get("level") == "high")
        hard_threshold = coerce_number(getattr(config, "EVENT_RISK_HARD_PENALTY", 24.0), 24.0)
        level = "high" if penalty >= hard_threshold or high_count >= 2 else "medium" if penalty >= 8 else "low"
        kept_items[normalize_code(code)] = {
            **item,
            "flags": kept_flags,
            "penalty": round(penalty, 2),
            "level": level,
            "hard_exclude": level == "high" and bool(getattr(config, "EVENT_RISK_HARD_FILTER", False)),
        }
    result["items"] = kept_items
    result["point_in_time"] = {
        "cutoff": str(cutoff or ""),
        "kept": len(kept_items),
        "rejected_missing_announcement_time": rejected_missing_time,
        "rejected_future_announcement": rejected_future,
    }
    return result


def build_candidate_snapshot_rows(
    quotes: pd.DataFrame,
    candidates: pd.DataFrame,
    selected_rows: Iterable[Dict[str, object]],
    signal_time: str,
    event_payload: Dict[str, object] = None,
    fundamental_payload: Dict[str, object] = None,
    provider_health: Dict[str, object] = None,
    scored_rows: Iterable[Dict[str, object]] = None,
    strategy_name: str = "",
    snapshot_id: str = "",
) -> List[Dict[str, object]]:
    if quotes is None or quotes.empty:
        return []
    raw_source_frame = quotes.copy()
    try:
        source_frame = rename_known_columns(raw_source_frame.copy())
    except Exception:
        source_frame = raw_source_frame.copy()
    base = _candidate_base_frame(source_frame)
    masks = _candidate_filter_masks(base)
    quote_timestamp = str(
        (quotes.attrs or {}).get("quote_timestamp")
        or (provider_health or {}).get("last_quote_refresh")
        or ""
    )
    market_cutoff = str(signal_time or quote_timestamp)
    event_timestamp = str((event_payload or {}).get("generated_at") or "")
    fundamental_timestamp = str((fundamental_payload or {}).get("generated_at") or "")
    event_items = _normalized_payload_items((event_payload or {}).get("items") or {})
    fundamental_items = _normalized_payload_items((fundamental_payload or {}).get("items") or {})

    source_rows = raw_source_frame.to_dict(orient="records")
    normalized_source_rows = [
        make_json_safe(row) for row in source_frame.to_dict(orient="records")
    ]
    candidate_lookup = _frame_lookup(candidates)
    selected_input = list(selected_rows or [])
    selected_lookup = _json_row_lookup(selected_input)
    strategy_pool_captured = scored_rows is not None
    scored_input = list(scored_rows) if scored_rows is not None else selected_input
    scored_lookup = _json_row_lookup(scored_input)
    selected_codes = set(selected_lookup)
    scored_codes = set(scored_lookup)
    candidate_codes = set(candidate_lookup)
    hard_filter_failures = _hard_filter_failures(masks, len(base))

    cutoff_boundary = _parse_timestamp(market_cutoff, end_of_day=True)
    quote_timestamp_valid = bool(
        quote_timestamp
        and _timestamp_not_after_boundary(quote_timestamp, cutoff_boundary)
    )
    common_point_in_time_violations = []
    if not quote_timestamp:
        common_point_in_time_violations.append("missing_quote_observed_at")
    elif not quote_timestamp_valid:
        common_point_in_time_violations.append(
            "future_quote_observed_at:{}".format(quote_timestamp)
        )
    if not market_cutoff:
        common_point_in_time_violations.append("missing_market_data_cutoff")
    recommendation_cutoff = str(
        getattr(config, "RECOMMENDATION_FREEZE_CUTOFF_TIME", "14:35")
    )
    if strategy_name in {"today_term", "tomorrow_picks", "swing_picks"} and _signal_at_or_after_cutoff(
        market_cutoff,
        recommendation_cutoff,
    ):
        common_point_in_time_violations.append(
            "signal_after_recommendation_cutoff:{}".format(recommendation_cutoff)
        )

    records: List[Dict[str, object]] = []
    seen = set()
    base_records = base.to_dict(orient="records")
    for position, base_row in enumerate(base_records):
        code = normalize_code(base_row.get("code"))
        if not code or code in seen:
            continue
        seen.add(code)
        raw = make_json_safe(source_rows[position] if position < len(source_rows) else base_row)
        raw_normalized = (
            normalized_source_rows[position]
            if position < len(normalized_source_rows)
            else _normalized_source_row(raw)
        )
        enriched = dict(candidate_lookup.get(code) or {})
        displayed = selected_lookup.get(code) or {}
        scored = dict(scored_lookup.get(code) or {})
        if displayed:
            frozen_rule_rank = int(scored.get("frozen_rule_rank") or scored.get("rank") or 0)
            scored.update(displayed)
            scored["frozen_rule_rank"] = frozen_rule_rank
        model_features = dict(enriched)
        model_features.update(scored)
        failed_keys = list(hard_filter_failures[position])
        event_item = event_items.get(code) if isinstance(event_items, dict) else {}
        if isinstance(event_item, dict) and event_item.get("hard_exclude"):
            failed_keys.append("event_risk_hard_exclude")
        eligible = (
            not failed_keys
            and code in candidate_codes
            and (not strategy_pool_captured or code in scored_codes)
        )
        selected = code in selected_codes
        reasons = [
            {"key": key, "label": HARD_FILTER_LABELS.get(key, "事件风险硬过滤")}
            for key in failed_keys
        ]
        if eligible and selected:
            reasons.append({"key": "selected", "label": "策略入选"})
        elif eligible:
            reasons.append({"key": "not_selected", "label": "合格但未入选"})
        elif not failed_keys and strategy_pool_captured:
            reasons.append({"key": "strategy_ineligible", "label": "未通过策略候选资格"})

        fundamental_item = fundamental_items.get(code) if isinstance(fundamental_items, dict) else {}
        announcement_times = _announcement_times(fundamental_item, event_item, scored)
        point_in_time_violations = list(common_point_in_time_violations)
        timestamp_validity = {
            market_cutoff: cutoff_boundary is not None,
        }
        if quote_timestamp:
            timestamp_validity[quote_timestamp] = quote_timestamp_valid
        for value in announcement_times:
            valid = _timestamp_not_after_boundary(value, cutoff_boundary)
            timestamp_validity[value] = valid
            if not valid:
                point_in_time_violations.append(
                    "future_announcement:{}".format(value)
                )
        source_timestamps = {
            "snapshot_id": str(snapshot_id or ""),
            "quote_observed_at": quote_timestamp,
            "market_data_cutoff": market_cutoff,
            "event_loaded_at": event_timestamp,
            "fundamentals_loaded_at": fundamental_timestamp,
            "announcement_times": announcement_times,
        }
        feature_values = {
            "raw_source": raw,
            "model_input": model_features,
        }
        missing_mask = {
            "raw_source.{}".format(key): is_missing(value) for key, value in raw.items()
        }
        alphalite_ready = bool(
            _finite_number(model_features.get("alphalite_factor_ready"))
        )
        fundamental_degraded = bool(model_features.get("fundamental_degraded"))
        event_risk_available = str(
            model_features.get("event_risk_status") or ""
        ) in {"ok", "cached"}
        for key, value in model_features.items():
            if key in _FUNDAMENTAL_FIELDS:
                source_value = (fundamental_item or {}).get(key) if isinstance(fundamental_item, dict) else None
                if is_missing(source_value):
                    source_value = raw_normalized.get(key)
                missing = is_missing(source_value)
            elif key in _FUNDAMENTAL_DERIVED_FIELDS:
                missing = fundamental_degraded
            elif str(key).startswith("event_risk_") and key != "event_risk_status":
                missing = not event_risk_available
            elif key in _QUOTE_SOURCE_FIELDS:
                missing = key not in raw_normalized or is_missing(raw_normalized.get(key))
            elif key in _ALPHALITE_FIELDS:
                missing = not alphalite_ready
            else:
                missing = is_missing(value)
            missing_mask["model_input.{}".format(key)] = missing
        feature_observed_at = {
            "raw_source.{}".format(key): quote_timestamp for key in raw
        }
        latest_announcement = max(announcement_times) if announcement_times else ""
        history_observed_at = (
            _market_data_timestamp(model_features.get("history_data_cutoff"))
            or market_cutoff
        )
        for key in model_features:
            if key in _FUNDAMENTAL_FIELDS or key in _FUNDAMENTAL_DERIVED_FIELDS or key in {
                "announcement_time",
                "report_period",
            }:
                observed_at = latest_announcement or fundamental_timestamp
            elif str(key).startswith("event_risk_"):
                observed_at = latest_announcement or event_timestamp or market_cutoff
            elif key in _ALPHALITE_FIELDS or key == "history_data_cutoff":
                observed_at = history_observed_at
            else:
                observed_at = market_cutoff
            feature_observed_at["model_input.{}".format(key)] = observed_at
        source_timestamps["feature_observed_at"] = feature_observed_at
        for feature_key, observed_at in feature_observed_at.items():
            if missing_mask.get(feature_key):
                continue
            if not observed_at:
                point_in_time_violations.append("missing_feature_timestamp:{}".format(feature_key))
                continue
            if observed_at not in timestamp_validity:
                timestamp_validity[observed_at] = _timestamp_not_after_boundary(
                    observed_at,
                    cutoff_boundary,
                )
            if not timestamp_validity[observed_at]:
                point_in_time_violations.append(
                    "future_feature_timestamp:{}:{}".format(feature_key, observed_at)
                )
        for key in ALL_FUNDAMENTAL_COLUMNS:
            missing_mask.setdefault(
                "fundamental.{}".format(key),
                is_missing((fundamental_item or {}).get(key)) if isinstance(fundamental_item, dict) else True,
            )
        records.append(
            {
                "code": code,
                "name": str(base_row.get("name") or ""),
                "market": str(base_row.get("market") or ""),
                "industry": str(base_row.get("industry") or ""),
                "style_bucket": style_bucket(base_row.get("market_cap") or base_row.get("float_market_cap")),
                "eligible": eligible,
                "selected": selected,
                "rank": int(scored.get("frozen_rule_rank") or scored.get("rank") or 0),
                "score": _finite_number(scored.get("score")),
                "eligibility_reasons": reasons,
                "feature_values": feature_values,
                "missing_mask": missing_mask,
                "source_timestamps": source_timestamps,
                "announcement_time": max(announcement_times) if announcement_times else "",
                "market_data_cutoff": market_cutoff,
                "point_in_time_valid": not point_in_time_violations,
                "point_in_time_violations": sorted(set(point_in_time_violations)),
                "snapshot_id": str(snapshot_id or ""),
                "raw": {
                    "quote": raw,
                    "candidate": enriched,
                    "scored": scored,
                    "selected": displayed,
                    "event": dict(event_item or {}),
                    "fundamental": dict(fundamental_item or {}),
                },
            }
        )
    return records


def _normalized_payload_items(payload_items: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    if not isinstance(payload_items, dict):
        return {}
    normalized = {}
    for raw_code, item in payload_items.items():
        code = normalize_code(raw_code)
        if not code or not isinstance(item, dict):
            continue
        normalized[code] = make_json_safe(item)
    return normalized


def _json_row_lookup(rows: Iterable[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    result: Dict[str, Dict[str, object]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = normalize_code(row.get("code"))
        if not code:
            continue
        result[code] = make_json_safe(dict(row))
    return result


def _normalized_source_rows(raw_frame: pd.DataFrame) -> List[Dict[str, object]]:
    if raw_frame is None or raw_frame.empty:
        return []
    try:
        normalized = rename_known_columns(raw_frame.copy())
    except Exception:
        normalized = raw_frame.copy()
    return [make_json_safe(row) for row in normalized.to_dict(orient="records")]


def _hard_filter_failures(
    masks: Dict[str, pd.Series],
    row_count: int,
) -> List[List[str]]:
    failures: List[List[str]] = [[] for _ in range(max(0, int(row_count)))]
    for key in HARD_FILTER_LABELS:
        mask = masks.get(key)
        if mask is None:
            values = [False] * len(failures)
        else:
            values = mask.fillna(False).astype(bool).tolist()
        for position, passed in enumerate(values[: len(failures)]):
            if not passed:
                failures[position].append(key)
    return failures


def _signal_at_or_after_cutoff(signal_time: str, cutoff: str) -> bool:
    text = str(signal_time or "")
    clock = text.split("T", 1)[1][:5] if "T" in text else ""
    return bool(clock and cutoff and clock >= str(cutoff)[:5])


def first_announcement_time(item: Dict[str, object]) -> str:
    if not isinstance(item, dict):
        return ""
    for key in _ANNOUNCEMENT_KEYS:
        value = item.get(key)
        if not is_missing(value):
            return normalize_timestamp(value)
    return ""


def timestamp_not_after(value: object, cutoff: object) -> bool:
    boundary = _parse_timestamp(cutoff, end_of_day=True)
    return _timestamp_not_after_boundary(value, boundary)


def _timestamp_not_after_boundary(value: object, boundary) -> bool:
    observed = _parse_timestamp(value, end_of_day=True)
    return bool(observed is not None and boundary is not None and observed <= boundary)


def normalize_timestamp(value: object) -> str:
    parsed = _parse_timestamp(value, end_of_day=True)
    return parsed.isoformat(timespec="seconds") if parsed is not None else ""


def style_bucket(value: object) -> str:
    cap = _finite_number(value)
    if cap >= 100_000_000_000:
        return "large_cap"
    if cap >= 20_000_000_000:
        return "mid_cap"
    if cap > 0:
        return "small_cap"
    return "unknown"


def is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in {"", "-", "--", "None", "nan", "NaN", "null"}
    if isinstance(value, (dict, list, tuple, set)):
        return False
    try:
        return bool(pd.isna(value))
    except Exception:
        return False


def make_json_safe(value):
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(item) for item in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            return make_json_safe(value.item())
        except Exception:
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _frame_lookup(frame: pd.DataFrame) -> Dict[str, Dict[str, object]]:
    if frame is None or frame.empty or "code" not in frame.columns:
        return {}
    result: Dict[str, Dict[str, object]] = {}
    for row in frame.to_dict(orient="records"):
        code = normalize_code(row.get("code"))
        if code:
            result[code] = make_json_safe(row)
    return result


def _normalized_source_row(raw: Dict[str, object]) -> Dict[str, object]:
    if not raw:
        return {}
    try:
        frame = rename_known_columns(pd.DataFrame([raw]))
        return make_json_safe(frame.iloc[0].to_dict())
    except Exception:
        return dict(raw)


def _announcement_times(*items) -> List[str]:
    values: List[str] = []
    for item in items:
        _collect_announcement_times(item, values)
    return sorted(set(value for value in values if value))


def _collect_announcement_times(value, output: List[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(token in key_text for token in ("announce", "publish", "公告")) and not isinstance(
                item, (dict, list, tuple)
            ):
                normalized = normalize_timestamp(item)
                if normalized:
                    output.append(normalized)
            else:
                _collect_announcement_times(item, output)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_announcement_times(item, output)


def _parse_timestamp(value: object, end_of_day: bool = False):
    if is_missing(value):
        return None
    text = str(value).strip()
    return _parse_timestamp_text(text, bool(end_of_day))


@lru_cache(maxsize=16384)
def _parse_timestamp_text(text: str, end_of_day: bool = False):
    try:
        parsed = pd.to_datetime(text, errors="coerce")
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    stamp = parsed.to_pydatetime() if hasattr(parsed, "to_pydatetime") else parsed
    has_clock = any(token in text for token in (":", "T", " ")) and len(text) > 10
    if end_of_day and not has_clock:
        stamp = stamp.replace(hour=23, minute=59, second=59, microsecond=0)
    if stamp.tzinfo is not None:
        stamp = stamp.replace(tzinfo=None)
    return stamp


def _market_data_timestamp(value: object) -> str:
    parsed = _parse_timestamp(value, end_of_day=False)
    if parsed is None:
        return ""
    text = str(value or "")
    has_clock = any(token in text for token in (":", "T", " ")) and len(text) > 10
    if not has_clock:
        parsed = parsed.replace(hour=15, minute=0, second=0, microsecond=0)
    return parsed.isoformat(timespec="seconds")


def _finite_number(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0
