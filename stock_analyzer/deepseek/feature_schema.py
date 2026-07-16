from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Dict, Iterable, List, Tuple
from zoneinfo import ZoneInfo

from ..normalization import coerce_number, normalize_code
from ..strategies.types import storage_strategy_name
from .evidence_validation import availability_invariant_error
from .research_policy import qualitative_evidence_hash


FEATURE_SCHEMA_VERSION = "deepseek_five_dimension_decision_v4"
PROMPT_VERSIONS = {
    "today_term": "today_five_dimension_decision_v4",
    "tomorrow_picks": "tomorrow_five_dimension_decision_v4",
    "swing_picks": "swing_2_5d_five_dimension_decision_v4",
    "long_term_watch": "long_term_five_dimension_decision_v4",
}
CONTRACTS = {
    "today_term": {
        "label": "今早执行",
        "horizon": "today",
        "focus": "09:36后执行窗口的追高、冲高回落、资金背离、封板失败与当天兑现风险",
    },
    "tomorrow_picks": {
        "label": "明日策略",
        "horizon": "next_day",
        "focus": "次日催化、隔夜与兑现风险",
    },
    "swing_picks": {
        "label": "2-5日策略",
        "horizon": "2_5d",
        "focus": "催化能否持续2至5个交易日",
    },
    "long_term_watch": {
        "label": "长期观察",
        "horizon": "long_term",
        "focus": "低估值、财务质量、国产替代、关键产业链、真实政策扶持与龙头地位",
    },
}
EVENT_TYPES = {
    "业绩",
    "订单",
    "政策",
    "并购",
    "重组",
    "涨价",
    "监管",
    "减持",
    "解禁",
    "诉讼",
    "传闻",
    "行业",
    "其他",
    "未知",
}
HORIZONS = {"today", "next_day", "2_5d", "long_term", "unknown"}
REQUIRED_FIELDS = {
    "code",
    "event_type",
    "event_direction",
    "event_strength",
    "event_reliability",
    "novelty",
    "priced_in",
    "time_horizon",
    "overnight_risk",
    "regulatory_risk",
    "theme_truth",
    "uncertainty",
    "abstain",
    "evidence_ids",
    "risk_flags",
    "reason",
    "strategy_fit",
    "horizon_fit",
    "deepseek_score",
    "confidence",
    "veto",
    "risk_penalty",
    "value_quality",
    "financial_health",
    "market_flow",
    "industry_policy",
    "risk_assessment",
    "horizon_support",
}
NUMERIC_RANGES = {
    "event_direction": (-2.0, 2.0),
    "event_strength": (0.0, 100.0),
    "event_reliability": (0.0, 100.0),
    "novelty": (0.0, 100.0),
    "priced_in": (0.0, 100.0),
    "overnight_risk": (0.0, 100.0),
    "regulatory_risk": (0.0, 100.0),
    "theme_truth": (0.0, 100.0),
    "uncertainty": (0.0, 100.0),
    "deepseek_score": (0.0, 100.0),
    "confidence": (0.0, 100.0),
    "risk_penalty": (0.0, 30.0),
}
ASSESSMENTS = {"positive", "neutral", "negative", "unknown"}
FINANCIAL_TRENDS = {"improving", "stable", "deteriorating", "unknown"}
FLOW_HEALTH = {"healthy", "neutral", "unhealthy", "unknown"}
INDUSTRY_OUTLOOKS = {"growing", "stable", "contracting", "unknown"}
POLICY_RELEVANCE = {"direct", "indirect", "none", "unknown"}
RISK_LEVELS = {"low", "medium", "high", "unknown"}


def prompt_version(strategy: str) -> str:
    normalized = storage_strategy_name(strategy)
    return PROMPT_VERSIONS.get(normalized, "unknown_event_features_v1")


def strategy_contract(strategy: str) -> Dict[str, str]:
    normalized = storage_strategy_name(strategy)
    return dict(
        CONTRACTS.get(
            normalized,
            {"label": str(strategy), "horizon": "unknown", "focus": "只分析输入证据"},
        )
    )


def _parse_point_in_time(value: object):
    text = str(value or "").strip().replace("Z", "+00:00").replace("/", "-")
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace(" ", "T", 1))
        if result.tzinfo is None:
            result = result.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def build_candidate_evidence(
    row: Dict[str, object],
    cutoff_at: str,
    limit: int = 6,
) -> List[Dict[str, str]]:
    cutoff = _parse_point_in_time(cutoff_at)
    if cutoff is None:
        return []
    result: List[Dict[str, str]] = []
    seen = set()
    for item in row.get("recent_news") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("content") or "").strip()
        published = str(item.get("publish_time") or item.get("time") or "").strip()
        published_time = _parse_point_in_time(published)
        if not title or published_time is None or published_time > cutoff:
            continue
        source = str(item.get("source") or "")
        identity = "|".join((normalize_code(row.get("code")), source, published, title))
        evidence_id = str(
            item.get("evidence_id") or "e_{}".format(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24])
        )
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        result.append(
            {
                "evidence_id": evidence_id,
                "title": title[:160],
                "source": source[:40],
                "published_at": published[:32],
            }
        )
        if len(result) >= max(1, int(limit)):
            break
    return result


def candidate_feature_input(row: Dict[str, object], cutoff_at: str) -> Dict[str, object]:
    fundamentals = {
        key: round(coerce_number(row.get(key)), 2)
        for key in (
            "roe",
            "gross_margin",
            "debt_ratio",
            "pe_dynamic",
            "pb",
            "earnings_surprise",
            "rating_revision",
            "revenue_yoy",
            "net_profit_yoy",
            "operating_cashflow",
            "operating_cashflow_yoy",
            "free_cashflow",
            "current_ratio",
            "receivables_yoy",
            "inventory_yoy",
            "goodwill_ratio",
            "interest_bearing_debt_ratio",
            "fundamental_quality_score",
            "fundamental_value_score",
        )
    }
    market_context = {
        key: round(coerce_number(row.get(key)), 2)
        for key in (
            "pct_chg",
            "speed",
            "volume_ratio",
            "turnover_rate",
            "amplitude",
            "ret_5d",
            "ret_10d",
            "ret_20d",
            "turnover_20d",
            "volatility_20d",
            "vol_amount_5d",
            "close_vs_vwap",
            "upper_wick_ratio",
            "main_net_flow_1d",
            "main_net_flow_5d",
            "main_net_flow_10d",
            "main_net_flow_20d",
            "order_imbalance",
        )
    }
    market_state = _material_market_state(market_context)
    financial_available = not bool(row.get("fundamental_degraded")) and any(
        abs(coerce_number(fundamentals.get(key))) > 1e-12
        for key in ("roe", "gross_margin", "debt_ratio", "pe_dynamic", "pb", "revenue_yoy", "net_profit_yoy")
    )
    cashflow_available = any(
        abs(coerce_number(fundamentals.get(key))) > 1e-12
        for key in ("operating_cashflow", "operating_cashflow_yoy", "free_cashflow")
    )
    main_flow_available = any(
        abs(coerce_number(market_context.get(key))) > 1e-12
        for key in ("main_net_flow_1d", "main_net_flow_5d", "main_net_flow_10d", "main_net_flow_20d")
    )
    evidence = build_candidate_evidence(row, cutoff_at)
    evidence.extend(
        _structured_evidence(
            row,
            cutoff_at,
            fundamentals,
            market_state,
            financial_available=financial_available,
        )
    )
    research_input = _build_research_input(
        row,
        fundamentals,
        market_context,
        market_state,
        financial_available=financial_available,
        cashflow_available=cashflow_available,
        main_flow_available=main_flow_available,
        evidence=evidence,
    )
    return {
        "code": normalize_code(row.get("code")),
        "name": str(row.get("name") or "")[:40],
        "local_score": round(coerce_number(row.get("local_score", row.get("score"))), 2),
        "market": str(row.get("market") or "")[:20],
        "industry": str(row.get("industry") or row.get("theme") or "")[:60],
        "fundamentals": fundamentals,
        "fundamental_as_of": {
            "announcement_time": str(row.get("announcement_time") or "")[:32],
            "report_period": str(row.get("report_period") or "")[:20],
            "source_timestamp": str(row.get("source_timestamp") or "")[:32],
        },
        "market_context": market_context,
        "market_regime": str(row.get("market_regime") or "unknown")[:20],
        "research_input": research_input,
        "research_input_version": "deepseek_research_input_v1",
        "data_availability": {
            "financial": financial_available,
            "cashflow": cashflow_available,
            "main_fund_flow": main_flow_available,
            "order_imbalance": abs(coerce_number(market_context.get("order_imbalance"))) > 1e-12,
        },
        "verified_risk_flags": _verified_risk_flags(row),
        "evidence": evidence,
        "evidence_hash": qualitative_evidence_hash(evidence),
        "market_state_hash": hashlib.sha256(
            json.dumps(market_state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _build_research_input(
    row: Dict[str, object],
    fundamentals: Dict[str, object],
    market_context: Dict[str, object],
    market_state: Dict[str, object],
    *,
    financial_available: bool,
    cashflow_available: bool,
    main_flow_available: bool,
    evidence: List[Dict[str, str]],
) -> Dict[str, object]:
    policy_evidence_count = 0
    for item in evidence:
        title = str(item.get("title") or "")
        source = str(item.get("source") or "")
        if ("政策" in title) or ("policy" in title.lower()) or ("政策" in source) or ("policy" in source.lower()):
            policy_evidence_count += 1
    value_score = coerce_number(fundamentals.get("fundamental_value_score"))
    quality_score = coerce_number(fundamentals.get("fundamental_quality_score"))
    month_flow_accel = coerce_number(market_context.get("main_net_flow_20d")) - coerce_number(
        market_context.get("main_net_flow_5d")
    )
    industry_growth_proxy = coerce_number(row.get("industry_revenue_growth"))
    policy_support_proxy = coerce_number(row.get("policy_support_score"))
    execution_risk_proxy = coerce_number(row.get("execution_risk_score"))
    return {
        "value_quality_context": {
            "valuation": {
                "roe": fundamentals.get("roe", 0.0),
                "gross_margin": fundamentals.get("gross_margin", 0.0),
                "pe_dynamic": fundamentals.get("pe_dynamic", 0.0),
                "pb": fundamentals.get("pb", 0.0),
                "debt_ratio": fundamentals.get("debt_ratio", 0.0),
                "fundamental_value_score": round(value_score, 2),
            },
            "quality_score_combo": {
                "fundamental_value_score": round(value_score, 2),
                "fundamental_quality_score": round(quality_score, 2),
                "earning_quality_ratio": coerce_number(fundamentals.get("gross_margin", 0.0)),
                "industry_growth_proxy": industry_growth_proxy,
            },
        },
        "financial_health_context": {
            "profitability": {
                "revenue_yoy": fundamentals.get("revenue_yoy", 0.0),
                "net_profit_yoy": fundamentals.get("net_profit_yoy", 0.0),
                "earnings_surprise": fundamentals.get("earnings_surprise", 0.0),
                "rating_revision": fundamentals.get("rating_revision", 0.0),
            },
            "cashflow": {
                "operating_cashflow": coerce_number(fundamentals.get("operating_cashflow")),
                "operating_cashflow_yoy": coerce_number(fundamentals.get("operating_cashflow_yoy")),
                "free_cashflow": coerce_number(fundamentals.get("free_cashflow")),
                "current_ratio": coerce_number(fundamentals.get("current_ratio")),
                "receivables_yoy": coerce_number(fundamentals.get("receivables_yoy")),
                "inventory_yoy": coerce_number(fundamentals.get("inventory_yoy")),
                "goodwill_ratio": coerce_number(fundamentals.get("goodwill_ratio")),
                "interest_bearing_debt_ratio": coerce_number(fundamentals.get("interest_bearing_debt_ratio")),
                "financial_data_available": int(financial_available),
                "cashflow_data_available": int(cashflow_available),
            },
        },
        "market_and_flow_context": {
            "context_1m": {
                "pct_chg": coerce_number(market_context.get("pct_chg")),
                "speed": coerce_number(market_context.get("speed")),
                "amplitude": coerce_number(market_context.get("amplitude")),
                "turnover_rate": coerce_number(market_context.get("turnover_rate")),
                "main_net_flow_1d": coerce_number(market_context.get("main_net_flow_1d")),
                "main_net_flow_5d": coerce_number(market_context.get("main_net_flow_5d")),
                "main_net_flow_10d": coerce_number(market_context.get("main_net_flow_10d")),
                "main_net_flow_20d": coerce_number(market_context.get("main_net_flow_20d")),
                "main_flow_acceleration_20d_vs_5d": round(month_flow_accel, 2),
                "main_fund_flow_available": int(main_flow_available),
            },
            "execution_context": {
                "market_state": market_state,
                "order_imbalance": coerce_number(market_context.get("order_imbalance")),
                "volume_ratio": coerce_number(market_context.get("volume_ratio")),
                "turnover_rate": coerce_number(market_context.get("turnover_rate")),
                "volatility_20d": coerce_number(market_context.get("volatility_20d")),
                "vol_amount_5d": coerce_number(market_context.get("vol_amount_5d")),
                "execution_risk_score": execution_risk_proxy,
            },
        },
        "industry_policy_context": {
            "industry": str(row.get("industry") or row.get("theme") or ""),
            "industry_regime": str(row.get("market_regime") or "unknown"),
            "industry_growth_proxy": round(industry_growth_proxy, 2),
            "policy_signal_count": policy_evidence_count,
            "policy_support_score": round(policy_support_proxy, 2),
            "policy_support_hint": _policy_support_hint(policy_support_proxy),
            "recent_policy_titles": _strings(
                [
                    item.get("title")
                    for item in evidence
                    if ("政策" in str(item.get("title") or "")) or ("policy" in str(item.get("title") or "").lower())
                ],
                3,
            ),
        },
        "risk_context": {
            "verified_risk_flags": _verified_risk_flags(row),
            "evidence_count": len(evidence),
            "risk_pressure_score": execution_risk_proxy,
            "market_state": str(row.get("market_regime") or "unknown"),
            "policy_dependency": "direct" if policy_evidence_count > 0 else "none",
        },
    }


def _policy_support_hint(value: float) -> str:
    if value >= 80:
        return "strong"
    if value >= 50:
        return "moderate"
    if value >= 20:
        return "weak"
    return "none"


def _material_market_state(values: Dict[str, object]) -> Dict[str, float]:
    steps = {
        "pct_chg": 0.5,
        "speed": 0.2,
        "volume_ratio": 0.25,
        "turnover_rate": 0.5,
        "amplitude": 0.5,
        "ret_5d": 1.0,
        "ret_10d": 1.0,
        "ret_20d": 1.0,
        "turnover_20d": 0.5,
        "volatility_20d": 0.5,
        "vol_amount_5d": 0.25,
        "close_vs_vwap": 0.5,
        "upper_wick_ratio": 0.05,
        "order_imbalance": 5.0,
    }
    result = {}
    for key, step in steps.items():
        value = coerce_number(values.get(key))
        result[key] = round(round(value / step) * step, 4)
    for key in ("main_net_flow_1d", "main_net_flow_5d", "main_net_flow_10d", "main_net_flow_20d"):
        value = coerce_number(values.get(key))
        result[key] = round(value, -5) if abs(value) >= 100000 else round(value, 2)
    return result


def _structured_evidence(
    row: Dict[str, object],
    cutoff_at: str,
    fundamentals: Dict[str, object],
    market_state: Dict[str, object],
    *,
    financial_available: bool,
) -> List[Dict[str, str]]:
    code = normalize_code(row.get("code"))
    result = []
    if financial_available:
        identity = json.dumps(fundamentals, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result.append(
            {
                "evidence_id": "f_{}".format(hashlib.sha256((code + identity).encode("utf-8")).hexdigest()[:24]),
                "title": "点时财务指标快照，报告期{}".format(str(row.get("report_period") or "未知"))[:160],
                "source": "point_in_time_fundamentals",
                "published_at": str(row.get("announcement_time") or row.get("source_timestamp") or cutoff_at)[:32],
            }
        )
    market_identity = json.dumps(market_state, sort_keys=True, separators=(",", ":"))
    result.append(
        {
            "evidence_id": "m_{}".format(hashlib.sha256((code + market_identity).encode("utf-8")).hexdigest()[:24]),
            "title": "点时行情、近20日量价及可用资金指标快照",
            "source": "point_in_time_market_data",
            "published_at": str(cutoff_at)[:32],
        }
    )
    return result


def validate_feature_response(
    parsed,
    *,
    strategy_name: str,
    candidates: Iterable[Dict[str, object]],
) -> Tuple[List[Dict[str, object]], List[Dict[str, str]]]:
    strategy = storage_strategy_name(strategy_name)
    source = {normalize_code(item.get("code")): item for item in candidates or [] if isinstance(item, dict)}
    results = parsed.get("results") if isinstance(parsed, dict) else None
    if not isinstance(results, list):
        return [], [{"code": "", "reason": "missing_results_array"}]

    valid: List[Dict[str, object]] = []
    errors: List[Dict[str, str]] = []
    seen = set()
    for raw in results:
        code = normalize_code(raw.get("code")) if isinstance(raw, dict) else ""
        candidate = source.get(code)
        if not isinstance(raw, dict) or not candidate or code in seen:
            errors.append({"code": code, "reason": "unknown_or_duplicate_candidate"})
            continue
        seen.add(code)

        missing_fields = sorted(REQUIRED_FIELDS - set(raw))
        if missing_fields:
            errors.append({"code": code, "reason": "missing_fields:{}".format(",".join(missing_fields))})
            continue
        if not _valid_array_fields(raw):
            errors.append({"code": code, "reason": "invalid_array_field"})
            continue
        research_error = _research_section_error(raw)
        if research_error:
            errors.append({"code": code, "reason": research_error})
            continue
        invariant_error = availability_invariant_error(raw, candidate)
        if invariant_error:
            errors.append({"code": code, "reason": invariant_error})
            continue
        if any(not isinstance(raw.get(key), bool) for key in ("abstain", "strategy_fit", "horizon_fit", "veto")):
            errors.append({"code": code, "reason": "decision_flags_must_be_boolean"})
            continue
        invalid_numeric = _invalid_numeric_field(raw)
        if invalid_numeric:
            errors.append({"code": code, "reason": "invalid_numeric_range:{}".format(invalid_numeric)})
            continue
        if str(raw.get("event_type")) not in EVENT_TYPES:
            errors.append({"code": code, "reason": "invalid_event_type"})
            continue
        horizon = str(raw.get("time_horizon") or "").replace("-", "_")
        if horizon not in HORIZONS:
            errors.append({"code": code, "reason": "invalid_time_horizon"})
            continue

        allowed_evidence = {str(item.get("evidence_id")) for item in candidate.get("evidence") or []}
        cited_evidence = _strings(raw.get("evidence_ids") or [], 8)
        if any(item not in allowed_evidence for item in cited_evidence):
            errors.append({"code": code, "reason": "evidence_out_of_scope"})
            continue
        abstain = bool(raw.get("abstain") or not allowed_evidence)
        if not abstain and not cited_evidence:
            errors.append({"code": code, "reason": "missing_evidence_for_opinion"})
            continue

        feature = {
            "code": code,
            "strategy": strategy,
            "schema_version": FEATURE_SCHEMA_VERSION,
            "event_type": str(raw.get("event_type") or "未知"),
            "event_direction": int(round(_clamp(raw.get("event_direction"), -2, 2))),
            "event_strength": _clamp(raw.get("event_strength"), 0, 100),
            "event_reliability": _clamp(raw.get("event_reliability"), 0, 100),
            "novelty": _clamp(raw.get("novelty"), 0, 100),
            "priced_in": _clamp(raw.get("priced_in"), 0, 100),
            "time_horizon": horizon,
            "horizon_match": horizon == strategy_contract(strategy)["horizon"],
            "overnight_risk": _clamp(raw.get("overnight_risk"), 0, 100),
            "regulatory_risk": _clamp(raw.get("regulatory_risk"), 0, 100),
            "theme_truth": _clamp(raw.get("theme_truth"), 0, 100),
            "uncertainty": _clamp(raw.get("uncertainty"), 0, 100),
            "strategy_fit": bool(raw.get("strategy_fit")),
            "horizon_fit": bool(raw.get("horizon_fit")),
            "deepseek_score": _clamp(raw.get("deepseek_score"), 0, 100),
            "confidence": _clamp(raw.get("confidence"), 0, 100),
            "veto": bool(raw.get("veto")),
            "risk_penalty": _clamp(raw.get("risk_penalty"), 0, 30),
            "abstain": abstain,
            "evidence_ids": [] if abstain else cited_evidence,
            "evidence_hash": str(candidate.get("evidence_hash") or ""),
            "risk_flags": _strings(raw.get("risk_flags") or [], 5),
            "reason": str(raw.get("reason") or "")[:240],
            "value_quality": {
                "assessment": str(raw["value_quality"].get("assessment")),
                "confidence": _clamp(raw["value_quality"].get("confidence"), 0, 100),
                "flags": _strings(raw["value_quality"].get("flags"), 5),
            },
            "financial_health": {
                "profit_trend": str(raw["financial_health"].get("profit_trend")),
                "cashflow_trend": str(raw["financial_health"].get("cashflow_trend")),
                "confidence": _clamp(raw["financial_health"].get("confidence"), 0, 100),
                "flags": _strings(raw["financial_health"].get("flags"), 5),
            },
            "market_flow": {
                "flow_health": str(raw["market_flow"].get("flow_health")),
                "price_flow_divergence": bool(raw["market_flow"].get("price_flow_divergence")),
                "confidence": _clamp(raw["market_flow"].get("confidence"), 0, 100),
                "flags": _strings(raw["market_flow"].get("flags"), 5),
            },
            "industry_policy": {
                "industry_outlook": str(raw["industry_policy"].get("industry_outlook")),
                "policy_relevance": str(raw["industry_policy"].get("policy_relevance")),
                "confidence": _clamp(raw["industry_policy"].get("confidence"), 0, 100),
                "flags": _strings(raw["industry_policy"].get("flags"), 5),
            },
            "risk_assessment": {
                "risk_level": str(raw["risk_assessment"].get("risk_level")),
                "confidence": _clamp(raw["risk_assessment"].get("confidence"), 0, 100),
                "flags": _strings(raw["risk_assessment"].get("flags"), 5),
            },
            "horizon_support": {
                key: _clamp(raw["horizon_support"].get(key), 0, 100)
                for key in ("today", "next_day", "2_5d", "long_term")
            },
            "valid": True,
        }
        valid.append(_neutral_feature(feature) if abstain else adapt_feature_to_strategy(feature, strategy))
    return valid, errors


def abstain_feature(
    candidate: Dict[str, object],
    strategy: str,
    reason: str,
) -> Dict[str, object]:
    return _neutral_feature(
        {
            "code": normalize_code(candidate.get("code")),
            "strategy": storage_strategy_name(strategy),
            "schema_version": FEATURE_SCHEMA_VERSION,
            "event_type": "未知",
            "time_horizon": "unknown",
            "horizon_match": False,
            "abstain": True,
            "evidence_ids": [],
            "evidence_hash": str(candidate.get("evidence_hash") or ""),
            "risk_flags": [],
            "reason": str(reason)[:240],
            "valid": True,
        }
    )


def _verified_risk_flags(row: Dict[str, object]) -> List[str]:
    values = []
    for key in ("announcement_flags", "event_risk_flags", "risk_words"):
        current = row.get(key)
        if isinstance(current, (list, tuple, set)):
            values.extend(current)
        elif current:
            values.append(current)
    return _strings(values, 8)


def _valid_array_fields(raw: Dict[str, object]) -> bool:
    for key, limit in (("evidence_ids", 8), ("risk_flags", 5)):
        value = raw.get(key)
        if not isinstance(value, list) or len(value) > limit or any(not isinstance(item, str) for item in value):
            return False
    return isinstance(raw.get("reason"), str)


def _research_section_error(raw: Dict[str, object]) -> str:
    sections = {
        "value_quality": (("assessment", "confidence", "flags"), "assessment", ASSESSMENTS),
        "financial_health": (
            ("profit_trend", "cashflow_trend", "confidence", "flags"),
            "profit_trend",
            FINANCIAL_TRENDS,
        ),
        "market_flow": (("flow_health", "price_flow_divergence", "confidence", "flags"), "flow_health", FLOW_HEALTH),
        "industry_policy": (
            ("industry_outlook", "policy_relevance", "confidence", "flags"),
            "industry_outlook",
            INDUSTRY_OUTLOOKS,
        ),
        "risk_assessment": (("risk_level", "confidence", "flags"), "risk_level", RISK_LEVELS),
    }
    for name, (required, enum_key, enum_values) in sections.items():
        section = raw.get(name)
        if not isinstance(section, dict) or any(key not in section for key in required):
            return "invalid_research_section:{}".format(name)
        if str(section.get(enum_key)) not in enum_values:
            return "invalid_research_enum:{}".format(name)
        confidence = section.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 100
        ):
            return "invalid_research_confidence:{}".format(name)
        flags = section.get("flags")
        if not isinstance(flags, list) or len(flags) > 5 or any(not isinstance(item, str) for item in flags):
            return "invalid_research_flags:{}".format(name)
    financial = raw["financial_health"]
    if str(financial.get("cashflow_trend")) not in FINANCIAL_TRENDS:
        return "invalid_cashflow_trend"
    market_flow = raw["market_flow"]
    if not isinstance(market_flow.get("price_flow_divergence"), bool):
        return "invalid_price_flow_divergence"
    industry = raw["industry_policy"]
    if str(industry.get("policy_relevance")) not in POLICY_RELEVANCE:
        return "invalid_policy_relevance"
    support = raw.get("horizon_support")
    if not isinstance(support, dict) or set(support) != {"today", "next_day", "2_5d", "long_term"}:
        return "invalid_horizon_support"
    for value in support.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 100:
            return "invalid_horizon_support_value"
    return ""


def _invalid_numeric_field(raw: Dict[str, object]) -> str:
    for name, (low, high) in NUMERIC_RANGES.items():
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return name
        if not low <= float(value) <= high:
            return name
    return ""


def _neutral_feature(row: Dict[str, object]) -> Dict[str, object]:
    row.update(
        event_direction=0,
        event_strength=0.0,
        event_reliability=0.0,
        novelty=0.0,
        priced_in=50.0,
        overnight_risk=50.0,
        regulatory_risk=50.0,
        theme_truth=50.0,
        uncertainty=100.0,
        strategy_fit=False,
        horizon_fit=False,
        deepseek_score=0.0,
        confidence=0.0,
        veto=False,
        risk_penalty=0.0,
        horizon_match=False,
        evidence_ids=[],
        risk_flags=[],
        value_quality={"assessment": "unknown", "confidence": 0.0, "flags": []},
        financial_health={"profit_trend": "unknown", "cashflow_trend": "unknown", "confidence": 0.0, "flags": []},
        market_flow={"flow_health": "unknown", "price_flow_divergence": False, "confidence": 0.0, "flags": []},
        industry_policy={"industry_outlook": "unknown", "policy_relevance": "unknown", "confidence": 0.0, "flags": []},
        risk_assessment={"risk_level": "unknown", "confidence": 0.0, "flags": []},
        horizon_support={"today": 0.0, "next_day": 0.0, "2_5d": 0.0, "long_term": 0.0},
    )
    return row


def adapt_feature_to_strategy(feature: Dict[str, object], strategy: str) -> Dict[str, object]:
    result = dict(feature or {})
    normalized = storage_strategy_name(strategy)
    support = result.get("horizon_support") if isinstance(result.get("horizon_support"), dict) else {}
    today_support = coerce_number(support.get("today"))
    next_day_support = coerce_number(support.get("next_day"))
    swing_support = coerce_number(support.get("2_5d"))
    long_term_support = coerce_number(support.get("long_term"))
    if normalized == "today_term":
        matched = today_support >= 60.0 and next_day_support >= 60.0
    elif normalized == "tomorrow_picks":
        matched = next_day_support >= 60.0
    elif normalized == "swing_picks":
        matched = swing_support >= 60.0
    elif normalized == "long_term_watch":
        matched = long_term_support >= 60.0
    else:
        matched = False
    result["strategy"] = normalized
    result["horizon_fit"] = bool(result.get("horizon_fit", True) and matched)
    result["horizon_match"] = result["horizon_fit"]
    return result


def _clamp(value: object, low: float, high: float) -> float:
    return round(max(low, min(high, coerce_number(value, low))), 2)


def _strings(values, limit: int) -> List[str]:
    if isinstance(values, str):
        values = [values]
    result = []
    for value in values if isinstance(values, (list, tuple, set)) else []:
        if isinstance(value, dict):
            text = str(value.get("label") or value.get("title") or "")
        else:
            text = str(value or "")
        text = text.strip()
        if text and text not in result:
            result.append(text[:80])
        if len(result) >= max(1, int(limit)):
            break
    return result
