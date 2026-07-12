from __future__ import annotations

import json
import sqlite3
from typing import Dict, List

from .execution_policy import cost_scenarios
from .normalization import coerce_number
from .point_in_time import timestamp_not_after


def audit_point_in_time(store, strategy_name: str = "", sample_size: int = 30) -> Dict[str, object]:
    requested = max(1, int(sample_size or 30))
    where = "WHERE c.selected = 1"
    params: List[object] = []
    if strategy_name:
        where += " AND c.strategy_name = ?"
        params.append(strategy_name)
    params.append(requested)
    with store.repository.connect() as conn:
        conn.row_factory = sqlite3.Row
        samples = conn.execute(
            """
            SELECT c.*, s.id AS signal_id, s.turnover AS signal_turnover,
                   s.raw_json AS signal_raw_json,
                   e.label_status, e.promotion_eligible,
                   e.order_quantity, e.actual_filled_quantity, e.unfilled_quantity,
                   e.unfilled_entry_quantity, e.unfilled_exit_quantity,
                   e.actual_entry_price, e.actual_exit_quantity, e.actual_exit_price,
                   e.gross_return_pct, e.net_return_pct,
                   e.fee_pct, e.slippage_pct, e.impact_pct,
                   e.execution_policy_version, e.execution_policy_json,
                   e.cost_scenarios_json AS execution_cost_scenarios_json,
                   e.raw_prices_json AS execution_raw_prices_json,
                   e.delisting_status AS execution_delisting_status,
                   e.benchmark_json AS execution_benchmark_json,
                   o.next_trade_date, o.exit_date, o.primary_return_field,
                   o.validation_baseline_id, o.validation_baseline_json,
                   b.data_source_timestamp AS batch_data_source_timestamp,
                   b.execution_policy_version AS batch_execution_policy_version,
                   b.execution_policy_json AS batch_execution_policy_json
            FROM strategy_candidate_snapshots c
            LEFT JOIN strategy_signals s
              ON s.strategy_name = c.strategy_name
             AND s.strategy_version = c.strategy_version
             AND s.signal_date = c.signal_date
             AND s.code = c.code
            LEFT JOIN strategy_execution_records e ON e.signal_id = s.id
            LEFT JOIN strategy_outcomes o ON o.signal_id = s.id
            LEFT JOIN strategy_signal_batches b
              ON b.strategy_name = c.strategy_name
             AND b.strategy_version = c.strategy_version
             AND b.signal_date = c.signal_date
            {}
            ORDER BY c.signal_date DESC, c.strategy_name, c.rank
            LIMIT ?
            """.format(where),
            params,
        ).fetchall()
        batch_rows = conn.execute(
            """
            SELECT b.strategy_name, b.strategy_version, b.signal_date, b.saved_count,
                   b.candidate_count, b.selected_count AS batch_selected_count,
                   COUNT(c.id) AS actual_count,
                   COALESCE(SUM(c.eligible), 0) AS eligible_count,
                   COALESCE(SUM(CASE WHEN c.eligible = 0 THEN 1 ELSE 0 END), 0) AS ineligible_count,
                   COALESCE(SUM(c.selected), 0) AS selected_count,
                   COALESCE(SUM(CASE WHEN c.selected = 0 THEN 1 ELSE 0 END), 0) AS unselected_count,
                   COALESCE(SUM(CASE WHEN c.selected = 1 AND c.eligible = 0 THEN 1 ELSE 0 END), 0)
                     AS selected_ineligible_count,
                   (SELECT COUNT(*) FROM strategy_signals s
                    WHERE s.strategy_name = b.strategy_name AND s.strategy_version = b.strategy_version
                      AND s.signal_date = b.signal_date) AS signal_count,
                   (SELECT COUNT(*) FROM strategy_execution_records e
                    JOIN strategy_signals s ON s.id = e.signal_id
                    WHERE s.strategy_name = b.strategy_name AND s.strategy_version = b.strategy_version
                      AND s.signal_date = b.signal_date) AS execution_record_count,
                   (SELECT COUNT(*) FROM strategy_execution_records e
                    JOIN strategy_signals s ON s.id = e.signal_id
                    WHERE s.strategy_name = b.strategy_name AND s.strategy_version = b.strategy_version
                      AND s.signal_date = b.signal_date AND e.label_status = 'settled') AS settled_count,
                   (SELECT COUNT(*) FROM strategy_execution_records e
                    JOIN strategy_signals s ON s.id = e.signal_id
                    WHERE s.strategy_name = b.strategy_name AND s.strategy_version = b.strategy_version
                      AND s.signal_date = b.signal_date AND e.label_status = 'unfilled') AS unfilled_count,
                   (SELECT COUNT(*) FROM strategy_execution_records e
                    JOIN strategy_signals s ON s.id = e.signal_id
                    WHERE s.strategy_name = b.strategy_name AND s.strategy_version = b.strategy_version
                      AND s.signal_date = b.signal_date AND e.label_status = 'pending') AS pending_count,
                   (SELECT COUNT(*) FROM strategy_execution_records e
                    JOIN strategy_signals s ON s.id = e.signal_id
                    WHERE s.strategy_name = b.strategy_name AND s.strategy_version = b.strategy_version
                      AND s.signal_date = b.signal_date AND e.label_status = 'unknown') AS unknown_count
            FROM strategy_signal_batches b
            JOIN strategy_candidate_snapshots c
              ON c.strategy_name = b.strategy_name
             AND c.strategy_version = b.strategy_version
             AND c.signal_date = b.signal_date
            {strategy_filter}
            GROUP BY b.strategy_name, b.strategy_version, b.signal_date
            """.format(strategy_filter="WHERE b.strategy_name = ?" if strategy_name else ""),
            [strategy_name] if strategy_name else [],
        ).fetchall()

    violations: List[Dict[str, object]] = []
    reproducible_count = 0
    cost_reproducible_count = 0
    execution_policy_valid_count = 0
    status_counts: Dict[str, int] = {}
    missing_feature_count = 0
    for row in samples:
        item = dict(row)
        code = item.get("code")
        source_timestamps = _load_json(item.get("source_timestamps_json"), {})
        feature_values = _load_json(item.get("feature_values_json"), {})
        missing_mask = _load_json(item.get("missing_mask_json"), {})
        eligibility_reasons = _load_json(item.get("eligibility_reasons_json"), [])
        cutoff = str(item.get("market_data_cutoff") or item.get("signal_time") or "")
        required_times = {
            "quote_observed_at": source_timestamps.get("quote_observed_at"),
            "market_data_cutoff": source_timestamps.get("market_data_cutoff"),
        }
        for key, value in required_times.items():
            if not value or not timestamp_not_after(value, cutoff):
                violations.append({"code": code, "type": "timestamp_not_visible", "field": key, "value": value})
        for key in ("event_loaded_at", "fundamentals_loaded_at"):
            value = source_timestamps.get(key)
            if value and not timestamp_not_after(value, cutoff):
                violations.append({"code": code, "type": "timestamp_not_visible", "field": key, "value": value})
        batch_data_source_timestamp = str(item.get("batch_data_source_timestamp") or "")
        if not batch_data_source_timestamp or not timestamp_not_after(batch_data_source_timestamp, cutoff):
            violations.append(
                {
                    "code": code,
                    "type": "timestamp_not_visible",
                    "field": "batch_data_source_timestamp",
                    "value": batch_data_source_timestamp,
                }
            )
        for value in source_timestamps.get("announcement_times") or []:
            if not timestamp_not_after(value, cutoff):
                violations.append({"code": code, "type": "future_announcement", "value": value})
        if not bool(item.get("point_in_time_valid")):
            violations.append({"code": code, "type": "point_in_time_invalid"})
        if not eligibility_reasons:
            violations.append({"code": code, "type": "eligibility_reason_missing"})
        for namespace in ("raw_source", "model_input"):
            values = feature_values.get(namespace) or {}
            for key, value in values.items():
                mask_key = "{}.{}".format(namespace, key)
                if mask_key not in missing_mask:
                    violations.append({"code": code, "type": "missing_mask_absent", "field": mask_key})
                elif missing_mask.get(mask_key):
                    missing_feature_count += 1
                if not missing_mask.get(mask_key):
                    observed_at = (source_timestamps.get("feature_observed_at") or {}).get(mask_key)
                    if not observed_at or not timestamp_not_after(observed_at, cutoff):
                        violations.append(
                            {
                                "code": code,
                                "type": "feature_timestamp_not_visible",
                                "field": mask_key,
                                "value": observed_at,
                            }
                        )

        label_status = str(item.get("label_status") or "missing")
        status_counts[label_status] = status_counts.get(label_status, 0) + 1
        if not item.get("signal_id"):
            violations.append({"code": code, "type": "selected_candidate_without_signal"})
            continue

        batch_policy_version = str(item.get("batch_execution_policy_version") or "")
        record_policy_version = str(item.get("execution_policy_version") or "")
        batch_policy = _load_json(item.get("batch_execution_policy_json"), {})
        record_policy = _load_json(item.get("execution_policy_json"), {})
        policy_versions = {
            "batch": batch_policy_version,
            "record": record_policy_version,
            "batch_json": str(batch_policy.get("policy_version") or ""),
            "record_json": str(record_policy.get("policy_version") or ""),
        }
        policy_valid = bool(batch_policy_version) and all(
            value == batch_policy_version for value in policy_versions.values()
        )
        if not policy_valid:
            violations.append(
                {"code": code, "type": "execution_policy_version_mismatch", "versions": policy_versions}
            )
        if batch_policy != record_policy:
            policy_valid = False
            violations.append({"code": code, "type": "execution_policy_payload_mismatch"})
        if label_status == "settled":
            validation_baseline = _load_json(item.get("validation_baseline_json"), {})
            baseline_policy_version = str(validation_baseline.get("execution_policy_version") or "")
            baseline_policy = validation_baseline.get("execution_policy") or {}
            policy_suffix = batch_policy_version.rsplit(".", 1)[-1] if batch_policy_version else ""
            if (
                baseline_policy_version != batch_policy_version
                or baseline_policy != batch_policy
                or not policy_suffix
                or "policy_{}".format(policy_suffix) not in str(item.get("validation_baseline_id") or "")
            ):
                policy_valid = False
                violations.append({"code": code, "type": "validation_baseline_policy_mismatch"})
        if policy_valid:
            execution_policy_valid_count += 1

        stored_scenarios = _load_json(item.get("execution_cost_scenarios_json"), {})
        cost_input = _load_json(item.get("signal_raw_json"), {})
        cost_input.update(
            {
                "strategy_name": item.get("strategy_name"),
                "turnover": item.get("signal_turnover"),
                "suggested_weight": item.get("target_weight_pct"),
            }
        )
        expected_scenarios = cost_scenarios(cost_input, record_policy) if record_policy else {}
        cost_valid = bool(record_policy)
        for scenario_name in ("low", "base", "high"):
            stored_scenario = stored_scenarios.get(scenario_name) or {}
            expected_scenario = expected_scenarios.get(scenario_name) or {}
            for field in ("fee_pct", "slippage_pct", "impact_pct", "total_pct"):
                stored_value = coerce_number(stored_scenario.get(field), None)
                expected_value = coerce_number(expected_scenario.get(field), None)
                if stored_value is None or expected_value is None or abs(stored_value - expected_value) > 0.0001:
                    cost_valid = False
        base_scenario = stored_scenarios.get("medium") or stored_scenarios.get("base") or {}
        for record_field in ("fee_pct", "slippage_pct", "impact_pct"):
            if abs(coerce_number(item.get(record_field)) - coerce_number(base_scenario.get(record_field))) > 0.0001:
                cost_valid = False
        if cost_valid:
            cost_reproducible_count += 1
        else:
            violations.append({"code": code, "type": "execution_cost_not_reproducible"})

        if label_status == "settled":
            entry = coerce_number(item.get("actual_entry_price"), None)
            exit_price = coerce_number(item.get("actual_exit_price"), None)
            gross = coerce_number(item.get("gross_return_pct"), None)
            scenarios = _load_json(item.get("execution_cost_scenarios_json"), {})
            base_cost = coerce_number((scenarios.get("base") or {}).get("total_pct"), None)
            net = coerce_number(item.get("net_return_pct"), None)
            if None in (entry, exit_price, gross, base_cost, net) or entry <= 0:
                violations.append({"code": code, "type": "return_inputs_missing"})
            else:
                expected_gross = (exit_price / entry - 1.0) * 100.0
                expected_net = expected_gross - base_cost
                if abs(expected_gross - gross) > 0.001 or abs(expected_net - net) > 0.0015:
                    violations.append(
                        {
                            "code": code,
                            "type": "return_not_reproducible",
                            "stored_gross": gross,
                            "expected_gross": round(expected_gross, 4),
                        }
                    )
                else:
                    reproducible_count += 1
            raw_prices = _load_json(item.get("execution_raw_prices_json"), [])
            prices_by_date = {_date_key(price.get("trade_date")): price for price in raw_prices if isinstance(price, dict)}
            primary_field = str(item.get("primary_return_field") or "")
            entry_date = _date_key(item.get("next_trade_date"))
            entry_bar = prices_by_date.get(entry_date)
            if primary_field.startswith("signal_"):
                signal_price = coerce_number((feature_values.get("raw_source") or {}).get("price"), None)
                if signal_price is None or abs(signal_price - coerce_number(entry, 0.0)) > 0.001:
                    violations.append({"code": code, "type": "entry_fill_not_in_signal_features"})
            else:
                raw_entry = coerce_number((entry_bar or {}).get("open"), None)
                if entry_bar is None or raw_entry is None or abs(raw_entry - coerce_number(entry, 0.0)) > 0.001:
                    violations.append({"code": code, "type": "entry_fill_not_in_raw_prices"})
            if (
                abs(coerce_number(item.get("order_quantity")) - coerce_number(item.get("actual_filled_quantity")))
                > 1e-8
                or abs(coerce_number(item.get("order_quantity")) - coerce_number(item.get("actual_exit_quantity")))
                > 1e-8
                or abs(coerce_number(item.get("unfilled_quantity"))) > 1e-8
            ):
                violations.append({"code": code, "type": "settled_quantity_not_conserved"})
            exit_date = (
                entry_date
                if primary_field in {"next_close_return", "signal_next_close_return"}
                else _date_key(item.get("exit_date"))
            )
            exit_bar = prices_by_date.get(exit_date)
            if primary_field in {"next_close_return", "signal_next_close_return"}:
                raw_exit = coerce_number((exit_bar or {}).get("close"), None)
                if exit_bar is None or raw_exit is None or abs(raw_exit - coerce_number(exit_price, 0.0)) > 0.001:
                    violations.append({"code": code, "type": "exit_fill_not_in_raw_prices"})
            elif primary_field == "exit_return":
                raw_low = coerce_number((exit_bar or {}).get("low"), None)
                raw_high = coerce_number((exit_bar or {}).get("high"), None)
                if (
                    exit_bar is None
                    or raw_low is None
                    or raw_high is None
                    or not raw_low - 0.001 <= coerce_number(exit_price, 0.0) <= raw_high + 0.001
                ):
                    violations.append({"code": code, "type": "exit_fill_not_in_raw_prices"})
            benchmark = _load_json(item.get("execution_benchmark_json"), {})
            for key in ("market", "industry", "style"):
                if key not in benchmark:
                    violations.append({"code": code, "type": "benchmark_missing", "field": key})
                elif (benchmark.get(key) or {}).get("return_pct") is None:
                    violations.append({"code": code, "type": "benchmark_unavailable", "field": key})
                else:
                    benchmark_item = benchmark.get(key) or {}
                    constituent_returns = [
                        coerce_number(value.get("return_pct"), None)
                        for value in benchmark_item.get("constituents") or []
                        if isinstance(value, dict)
                    ]
                    constituent_returns = [value for value in constituent_returns if value is not None]
                    expected_benchmark = (
                        sum(constituent_returns) / len(constituent_returns) if constituent_returns else None
                    )
                    if expected_benchmark is None or abs(
                        expected_benchmark - coerce_number(benchmark_item.get("return_pct"), 0.0)
                    ) > 0.001:
                        violations.append({"code": code, "type": "benchmark_not_reproducible", "field": key})
        elif label_status == "unfilled":
            order_quantity = coerce_number(item.get("order_quantity"))
            filled = coerce_number(item.get("actual_filled_quantity"))
            unfilled = coerce_number(item.get("unfilled_quantity"))
            unfilled_entry = coerce_number(item.get("unfilled_entry_quantity"))
            unfilled_exit = coerce_number(item.get("unfilled_exit_quantity"))
            quantities_conserved = (
                abs(unfilled - unfilled_entry - unfilled_exit) <= 1e-8
                and abs(order_quantity - unfilled) <= 1e-8
                and (
                    (filled == 0 and unfilled_entry == order_quantity and unfilled_exit == 0)
                    or (filled == order_quantity and unfilled_entry == 0 and unfilled_exit == order_quantity)
                )
            )
            if not quantities_conserved:
                violations.append({"code": code, "type": "unfilled_quantity_not_conserved"})
        elif label_status == "unknown":
            if item.get("gross_return_pct") is not None or bool(item.get("promotion_eligible")):
                violations.append({"code": code, "type": "unknown_has_fabricated_return_or_promotion"})
        elif label_status not in {"pending"}:
            violations.append({"code": code, "type": "label_status_missing"})
        delisting_status = str(item.get("execution_delisting_status") or "not_applicable")
        if delisting_status == "liquidated_last_tradable" and label_status != "settled":
            violations.append({"code": code, "type": "delisting_liquidation_not_settled"})
        if delisting_status == "unpriced_delisting" and label_status != "unknown":
            violations.append({"code": code, "type": "unpriced_delisting_not_unknown"})

    batch_conservation = []
    for row in batch_rows:
        item = dict(row)
        conserved = (
            int(item["candidate_count"] or 0) == int(item["actual_count"] or 0)
            and int(item["saved_count"] or 0) == int(item["signal_count"] or 0)
            and int(item["batch_selected_count"] or 0) == int(item["signal_count"] or 0)
            and int(item["actual_count"] or 0)
            == int(item["eligible_count"] or 0) + int(item["ineligible_count"] or 0)
            == int(item["selected_count"] or 0) + int(item["unselected_count"] or 0)
            and int(item["selected_ineligible_count"] or 0) == 0
            and int(item["selected_count"] or 0) == int(item["signal_count"] or 0)
            and int(item["signal_count"] or 0) == int(item["execution_record_count"] or 0)
            and int(item["execution_record_count"] or 0)
            == int(item["settled_count"] or 0)
            + int(item["unfilled_count"] or 0)
            + int(item["pending_count"] or 0)
            + int(item["unknown_count"] or 0)
        )
        batch_conservation.append({**item, "conserved": conserved})
        if not conserved:
            violations.append(
                {
                    "type": "candidate_count_not_conserved",
                    "strategy_name": item["strategy_name"],
                    "signal_date": item["signal_date"],
                }
            )

    enough_samples = len(samples) >= requested
    return {
        "ok": enough_samples and not violations,
        "requested_sample_count": requested,
        "sample_count": len(samples),
        "enough_samples": enough_samples,
        "point_in_time_valid_count": sum(1 for row in samples if bool(row["point_in_time_valid"])),
        "return_reproducible_count": reproducible_count,
        "cost_reproducible_count": cost_reproducible_count,
        "execution_policy_valid_count": execution_policy_valid_count,
        "missing_feature_count": missing_feature_count,
        "label_status_counts": status_counts,
        "batch_conservation": batch_conservation,
        "violation_count": len(violations),
        "violations": violations[:100],
    }


def _load_json(value, fallback):
    if isinstance(value, type(fallback)):
        return value
    try:
        loaded = json.loads(value or ("[]" if isinstance(fallback, list) else "{}"))
    except Exception:
        return fallback
    return loaded if isinstance(loaded, type(fallback)) else fallback


def _date_key(value: object) -> str:
    return str(value or "").strip()[:10].replace("-", "")
