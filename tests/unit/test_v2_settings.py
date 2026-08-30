from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trader.infra.market_data.features import FEATURE_SCHEMA_NAMES, FEATURE_SCHEMA_VERSION
from trader.infra.settings import (
    ConfigurationError,
    load_long_watchlist,
    load_runtime_settings,
    load_strategy_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG = PROJECT_ROOT / "config" / "v2" / "runtime.json"


def test_v2_configuration_contract_is_valid() -> None:
    runtime = load_runtime_settings(RUNTIME_CONFIG)
    strategy = load_strategy_settings(runtime.strategy_config_path)
    watchlist = load_long_watchlist(runtime.long_watchlist_path)

    assert runtime.schema_version == 10
    assert strategy.schema_version == 15
    assert strategy.tomorrow_scoring_profile == "v1"
    assert runtime.config_version == "runtime_v39_tushare_120_daily_audit_2026_08_26"
    assert runtime.market_data.source_contract_versions["eastmoney"] == ("eastmoney_quote_v17_security_master")
    assert runtime.api.default_top_n == 12
    assert runtime.api.maximum_top_n == 12
    assert runtime.runtime_dir == PROJECT_ROOT / ".runtime" / "v2"
    assert runtime.market_data.research_timeout_seconds == 8
    assert runtime.market_data.eastmoney_timeout_seconds == 8
    assert runtime.market_data.sina_timeout_seconds == 8
    assert runtime.market_data.full_market_hedge_delay_seconds == 1
    assert runtime.pipeline.market_workers == 5
    assert runtime.api.web_snapshot_retention_seconds == 35
    assert runtime.market_data.tushare.timeout_seconds == 8
    assert runtime.market_data.tushare.points == 120
    assert runtime.market_data.source_contract_versions["tushare"] == "tushare_sdk_v18_120_point_daily_audit"
    assert runtime.market_data.tushare.token_file == PROJECT_ROOT / ".token_key"
    assert set(runtime.market_data.cache_policy.datasets) == {
        "full_market_quotes",
        "candidate_quotes",
        "intraday_minutes",
        "research_success",
        "research_failure",
        "daily_history",
        "security_master_calendar",
        "daily_valuation_financials",
        "history_summary",
        "canonical_market_snapshot",
        "canonical_candidate_snapshot",
        "current_quote_index",
        "candidate_feature_batch",
        "hard_filter_batch",
        "board_cross_section",
        "candidate_preselection",
        "board_score_batch",
        "global_local_draft",
        "competition_group_mapping",
        "raw_deepseek_review",
        "strategy_deepseek_review",
        "deepseek_seen_codes",
        "published_recommendation_view",
        "published_date_index",
    }
    assert runtime.market_data.cache_policy.schema_version == 6
    assert runtime.market_data.cache_policy.total_bytes == 248 * 1024 * 1024
    assert runtime.market_data.cache_policy.runtime_reserve_bytes == 8 * 1024 * 1024
    assert runtime.market_data.cache_policy.pool_total_bytes == 256 * 1024 * 1024
    assert {name: policy.persisted for name, policy in runtime.market_data.cache_policy.datasets.items()} == {
        "full_market_quotes": False,
        "candidate_quotes": False,
        "intraday_minutes": False,
        "research_success": True,
        "research_failure": False,
        "daily_history": False,
        "security_master_calendar": False,
        "daily_valuation_financials": False,
        "history_summary": False,
        "canonical_market_snapshot": False,
        "canonical_candidate_snapshot": False,
        "current_quote_index": False,
        "candidate_feature_batch": False,
        "hard_filter_batch": False,
        "board_cross_section": False,
        "candidate_preselection": False,
        "board_score_batch": False,
        "global_local_draft": False,
        "competition_group_mapping": False,
        "raw_deepseek_review": False,
        "strategy_deepseek_review": False,
        "deepseek_seen_codes": False,
        "published_recommendation_view": False,
        "published_date_index": False,
    }
    assert runtime.performance_budgets.workload.market_rows == 5500
    assert runtime.performance_budgets.workload.candidate_rows == 360
    assert runtime.performance_budgets.schema_version == 2
    assert runtime.performance_budgets.rounds.warmup == 1
    assert runtime.performance_budgets.rounds.measurement == 5
    assert runtime.performance_budgets.latency_p95_ms["market_normalization"] == 250
    assert runtime.performance_budgets.latency_p95_ms["market_merge"] == 600
    assert runtime.performance_budgets.latency_p95_ms["canonical_snapshot"] == 900
    assert runtime.performance_budgets.latency_p95_ms["targeted_overlay_commit"] == 100
    assert runtime.performance_budgets.latency_p95_ms["quote_to_draft"] == 5000
    assert runtime.performance_budgets.latency_p95_ms["browser_patch_to_paint"] == 100
    assert runtime.performance_budgets.memory.cache_logical_bytes == 260046848
    assert runtime.performance_budgets.memory.process_peak_rss_bytes == 402653184
    assert set(runtime.pipeline.cadence_seconds["full_market"].values()) == {10.0}
    assert runtime.pipeline.cadence_seconds["candidate_quotes"]["today_main"] == 1
    assert runtime.pipeline.cadence_seconds["candidate_quotes"]["final_window"] == 1
    assert runtime.pipeline.cadence_seconds["topk_quotes"]["today_main"] == 1
    assert runtime.performance_budgets.data_age_p95_seconds["full_market_main"] == 10
    assert runtime.market_data.circuit_breaker_seconds == 30
    assert runtime.deepseek.daily_hard_limit == 168
    assert runtime.deepseek.strategy_limits == {
        "today": 8,
        "tomorrow": 38,
        "d25": 16,
        "shared_preheat": 4,
        "emergency": 5,
    }
    assert sum(runtime.deepseek.strategy_limits.values()) == 71
    assert sum(runtime.deepseek.stage_targets.values()) == 36
    assert sum(runtime.deepseek.stage_limits.values()) == 71
    assert sum(limit for stage, limit in runtime.deepseek.stage_limits.items() if stage != "emergency") == 66
    assert runtime.deepseek.timeout_seconds == 20
    assert runtime.deepseek.batch_size == 4
    assert runtime.deepseek.model == "deepseek-v4-flash"
    assert runtime.deepseek.challenger_model == "deepseek-v4-pro"
    assert runtime.deepseek.challenger_limits == {"today": 0, "tomorrow": 2, "d25": 0}
    assert runtime.deepseek.challenger_daily_limit == 2
    assert runtime.deepseek.adaptive.cooldown_seconds == 900
    assert runtime.deepseek.adaptive.minimum_application_ratio == pytest.approx(0.4)
    assert strategy.hard_filters.blacklist_codes == ()
    assert strategy.hard_filters.structured_risk_thresholds == {
        "major_shareholder_reduction": 0.0,
        "financial_fraud_history": 0.0,
        "official_investigation_history": 0.0,
        "major_illegal_history": 0.0,
        "fund_occupation_history": 0.0,
        "illegal_guarantee_history": 0.0,
        "forced_delisting_risk": 0.0,
        "unlock_risk": 0.0,
        "pledge_risk": 0.0,
        "financial_deterioration": 0.5,
    }
    assert strategy.fusion.local_weight == pytest.approx(0.68)
    assert strategy.fusion.deepseek_weight == pytest.approx(0.32)
    regulatory_rule = next(rule for rule in strategy.risk_rules if rule.risk_code == "regulatory_risk")
    assert regulatory_rule.veto is False
    assert regulatory_rule.allowed_evidence_types == ("announcement", "regulatory_filing")
    assert regulatory_rule.trigger_factor == "negative_announcement_level"
    assert regulatory_rule.trigger_thresholds == (3.0,)
    assert regulatory_rule.combination_mode == "exclusive"
    assert strategy.today_news_signal.lookback_hours == 72.0
    assert strategy.today_news_signal.freshness_full_score_hours == 1.0
    assert strategy.today_news_signal.positive_score == 75.0
    assert "回购" in strategy.today_news_signal.positive_keywords
    assert "减持" in strategy.today_news_signal.negative_keywords
    assert strategy.tomorrow_tail_signal.lookback_minutes == 30
    assert strategy.tomorrow_tail_signal.minimum_baseline_minutes == 30
    assert strategy.tomorrow_tail_signal.return_score_points_per_pct == 25.0
    assert strategy.tomorrow_tail_signal.volume_score_points_per_ratio == 50.0
    assert strategy.market_regime.risk_on_breadth_min == 60.0
    assert strategy.market_regime.risk_off_breadth_max == 40.0
    assert strategy.selection.review_candidate_limit == 28
    assert strategy.selection.default_top_k == 6
    assert strategy.selection.maximum_top_k == 12
    assert set(strategy.dimension_weights) == {"today", "tomorrow", "d25"}
    assert strategy.long_research.financial_max_age_days == 550
    assert strategy.long_research.pledge_thresholds == (10.0, 20.0, 35.0)
    assert "监管函" in strategy.long_research.negative_medium_keywords
    assert watchlist.schema_version == 2
    assert watchlist.watchlist_version == "long_watchlist_strategic_merge_2026_07"
    assert len(watchlist.items) == 224
    assert len(watchlist.groups) == 50
    assert max(len(group.codes) for group in watchlist.groups if group.category == "chokepoint") <= 5
    groups_by_key = {(group.category, group.name): group for group in watchlist.groups}
    chokepoint_groups = tuple(group for group in watchlist.groups if group.category == "chokepoint")
    assert len(chokepoint_groups) == 37
    document_sections = tuple(
        section
        for group in chokepoint_groups
        for section in group.sections
        if section.source_section == "document_scan"
    )
    current_leader_sections = tuple(
        section
        for group in chokepoint_groups
        for section in group.sections
        if section.source_section == "current_leaders"
    )
    assert len(document_sections) == 31
    assert sum(len(section.codes) for section in document_sections) == 93
    assert len(current_leader_sections) == 27
    liquid = groups_by_key[("chokepoint", "液冷")]
    assert liquid.codes == ("002837", "300499", "300990")
    assert [(section.source_section, section.codes) for section in liquid.sections] == [
        ("document_scan", ("002837",)),
        ("current_leaders", ("300499", "300990")),
    ]
    power = groups_by_key[("chokepoint", "数据中心电源")]
    assert power.codes == ("002518", "002335", "002364")
    assert [(section.source_section, section.codes) for section in power.sections] == [
        ("document_scan", ("002518", "002335")),
        ("current_leaders", ("002364",)),
    ]
    assert groups_by_key[("chokepoint", "固态电池")].sections[0].codes == (
        "002074",
        "300073",
        "300014",
        "002812",
    )
    assert groups_by_key[("chokepoint", "脑机接口")].codes == ("688626", "688273")
    assert groups_by_key[("chokepoint", "AI算力")].codes == (
        "603019",
        "601138",
        "000977",
        "000938",
    )
    assert ("chokepoint", "科学仪器/高端医疗设备") not in groups_by_key
    assert ("chokepoint", "精密零部件") not in groups_by_key
    assert groups_by_key[("chokepoint", "生命科学/高端医疗装备")].codes == (
        "688271",
        "300760",
        "688114",
        "688139",
    )
    assert groups_by_key[("chokepoint", "高端科学仪器")].codes == (
        "603100",
        "300203",
        "688337",
        "688112",
        "688200",
    )
    assert groups_by_key[("chokepoint", "高端传感器/精密测量")].codes == (
        "603662",
        "688322",
        "300007",
        "688539",
    )
    assert groups_by_key[("chokepoint", "航空发动机/燃气轮机")].codes == (
        "603308",
        "600893",
        "600765",
        "000738",
        "600391",
    )
    assert groups_by_key[("chokepoint", "新型电力系统/储能")].codes == (
        "600406",
        "300274",
        "000400",
        "600312",
        "688248",
    )
    assert groups_by_key[("chokepoint", "可控核聚变关键材料/装备")].codes == (
        "000969",
        "600105",
    )
    future_growth_groups = tuple(group for group in watchlist.groups if group.category == "future_growth")
    assert len(future_growth_groups) == 8
    assert max(len(group.codes) for group in future_growth_groups) <= 5
    assert ("future_growth", "新型储能/固态电池") not in groups_by_key
    assert groups_by_key[("future_growth", "光模块")].codes == (
        "300548",
        "300570",
        "301205",
        "688498",
        "300620",
    )
    low_price_groups = tuple(group for group in watchlist.groups if group.category == "low_price_potential")
    assert tuple(group.name for group in low_price_groups) == (
        "芯片与电子",
        "智能制造与软件",
        "算力与卫星",
        "材料与资源",
        "种业与生物育种",
    )
    assert sum(len(group.codes) for group in low_price_groups) == 24
    assert len({code for group in low_price_groups for code in group.codes}) == 24
    grouped_codes = tuple(code for group in watchlist.groups for code in group.codes)
    assert len(grouped_codes) == len(set(grouped_codes))
    assert set(grouped_codes) == {item.code for item in watchlist.items}


@pytest.mark.parametrize("profile", ("v1", "v2"))
def test_tomorrow_scoring_profile_is_an_explicit_versioned_switch(tmp_path, profile: str) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["tomorrow_scoring_profile"] = profile
    strategy_path = tmp_path / "strategy.json"
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    settings = load_strategy_settings(strategy_path)

    assert settings.tomorrow_scoring_profile == profile
    if profile == "v2":
        assert settings.strategy_version != load_strategy_settings(source).strategy_version


def test_tomorrow_scoring_profile_override_changes_the_effective_version_without_writing_config() -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    original = source.read_bytes()

    default = load_strategy_settings(source)
    overridden = load_strategy_settings(source, tomorrow_scoring_profile="v2")

    assert default.tomorrow_scoring_profile == "v1"
    assert overridden.tomorrow_scoring_profile == "v2"
    assert overridden.strategy_version != default.strategy_version
    assert source.read_bytes() == original


def test_unknown_tomorrow_scoring_profile_is_rejected(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["tomorrow_scoring_profile"] = "latest"
    strategy_path = tmp_path / "strategy.json"
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="tomorrow_scoring_profile"):
        load_strategy_settings(strategy_path)


@pytest.mark.parametrize("retired_profile", ("p1", "p2"))
def test_retired_tomorrow_scoring_profile_names_are_rejected(tmp_path, retired_profile: str) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["tomorrow_scoring_profile"] = retired_profile
    strategy_path = tmp_path / "strategy.json"
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="tomorrow_scoring_profile"):
        load_strategy_settings(strategy_path)


@pytest.mark.parametrize("schema_version", (5, 6, 7, 8, 9))
def test_runtime_rejects_every_pre_release_schema(tmp_path, schema_version: int) -> None:
    raw = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    raw["schema_version"] = schema_version
    changed_path = tmp_path / f"runtime-v{schema_version}.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="runtime schema_version must be 10"):
        load_runtime_settings(changed_path)


def test_runtime_schema_v10_rejects_paid_full_market_sources(tmp_path) -> None:
    raw = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    raw["market_data"]["full_market_sources"] = ["eastmoney", "paid_vendor"]
    changed_path = tmp_path / "runtime-v10-paid.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unknown keys"):
        load_runtime_settings(changed_path)


def test_runtime_rejects_non_positive_web_snapshot_retention(tmp_path) -> None:
    raw = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    raw["api"]["web_snapshot_retention_seconds"] = 0
    changed_path = tmp_path / "runtime.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="web_snapshot_retention_seconds"):
        load_runtime_settings(changed_path)


def test_long_watchlist_group_limits_are_enforced(tmp_path) -> None:
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "long_watchlist.json").read_text(encoding="utf-8"))
    raw["groups"][-1]["codes"] = [item["code"] for item in raw["items"][:9]]
    changed_path = tmp_path / "long_watchlist.json"
    changed_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="cannot contain more than 8 codes"):
        load_long_watchlist(changed_path)


def test_long_watchlist_low_price_groups_reject_duplicate_codes(tmp_path) -> None:
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "long_watchlist.json").read_text(encoding="utf-8"))
    low_price_groups = [group for group in raw["groups"] if group["category"] == "low_price_potential"]
    low_price_groups[1]["codes"][0] = low_price_groups[0]["codes"][0]
    low_price_groups[1]["sections"][0]["codes"][0] = low_price_groups[0]["codes"][0]
    changed_path = tmp_path / "long_watchlist.json"
    changed_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="cannot contain duplicate codes"):
        load_long_watchlist(changed_path)


def test_long_watchlist_rejects_codes_repeated_across_any_groups(tmp_path) -> None:
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "long_watchlist.json").read_text(encoding="utf-8"))
    raw["groups"][1]["codes"][0] = raw["groups"][0]["codes"][0]
    raw["groups"][1]["sections"][0]["codes"][0] = raw["groups"][0]["codes"][0]
    changed_path = tmp_path / "long_watchlist.json"
    changed_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="cannot repeat across groups"):
        load_long_watchlist(changed_path)


def test_long_watchlist_rejects_unknown_source_section(tmp_path) -> None:
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "long_watchlist.json").read_text(encoding="utf-8"))
    raw["groups"][0]["source_section"] = "ad_hoc"
    changed_path = tmp_path / "long_watchlist.json"
    changed_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="source_section"):
        load_long_watchlist(changed_path)


def test_runtime_rejects_removed_decision_execution_mode(tmp_path) -> None:
    raw = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    raw["pipeline"]["decision_execution_mode"] = "unsafe_parallel"
    changed_path = tmp_path / "runtime.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unknown keys"):
        load_runtime_settings(changed_path)


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner", "unknown-model"])
def test_runtime_settings_rejects_non_v4_primary_model(tmp_path, model: str) -> None:
    raw = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    raw["deepseek"]["model"] = model
    changed_path = tmp_path / "runtime.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="primary model"):
        load_runtime_settings(changed_path)


def test_runtime_settings_loads_deepseek_key_from_protected_file(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("DEEPSEEK_API_KEY=secret-from-file\n", encoding="utf-8")
    key_file.chmod(0o600)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(key_file))

    runtime = load_runtime_settings(RUNTIME_CONFIG)

    assert runtime.deepseek.api_key == "secret-from-file"


def test_runtime_settings_loads_both_credentials_from_one_protected_assignment_file(tmp_path, monkeypatch) -> None:
    credential_file = tmp_path / ".token_key"
    credential_file.write_text(
        "DEEPSEEK_API_KEY=deepseek-from-file\nTUSHARE_TOKEN=tushare-from-file\n",
        encoding="utf-8",
    )
    credential_file.chmod(0o600)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(credential_file))
    monkeypatch.setenv("TUSHARE_TOKEN_FILE", str(credential_file))

    runtime = load_runtime_settings(RUNTIME_CONFIG)

    assert runtime.deepseek.api_key == "deepseek-from-file"
    assert runtime.market_data.tushare.token == "tushare-from-file"


def test_runtime_settings_prefers_deepseek_environment_key(tmp_path, monkeypatch) -> None:
    missing_file = tmp_path / "missing.key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-from-environment")
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(missing_file))

    runtime = load_runtime_settings(RUNTIME_CONFIG)

    assert runtime.deepseek.api_key == "secret-from-environment"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_runtime_settings_rejects_insecure_deepseek_key_file(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("secret-from-file\n", encoding="utf-8")
    key_file.chmod(0o644)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(key_file))

    with pytest.raises(ConfigurationError, match="must not be accessible by group or other users"):
        load_runtime_settings(RUNTIME_CONFIG)


def test_runtime_settings_loads_tushare_token_with_environment_priority(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "tushare.token"
    token_file.write_text("file-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("TUSHARE_TOKEN", "environment-token")
    monkeypatch.setenv("TUSHARE_TOKEN_FILE", str(token_file))

    runtime = load_runtime_settings(RUNTIME_CONFIG)

    assert runtime.market_data.tushare.token == "environment-token"


def test_runtime_settings_loads_tushare_token_from_configured_protected_file(tmp_path, monkeypatch) -> None:
    raw = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    token_file = tmp_path / "tushare.token"
    token_file.write_text("configured-file-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    raw["market_data"]["tushare"]["token_file"] = str(token_file)
    changed_path = tmp_path / "runtime.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TUSHARE_TOKEN_FILE", raising=False)

    runtime = load_runtime_settings(changed_path)

    assert runtime.market_data.tushare.token == "configured-file-token"
    assert runtime.market_data.tushare.token_file == token_file


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_runtime_settings_rejects_insecure_tushare_token_file(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "tushare.token"
    token_file.write_text("file-token\n", encoding="utf-8")
    token_file.chmod(0o644)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setenv("TUSHARE_TOKEN_FILE", str(token_file))

    with pytest.raises(ConfigurationError, match="Tushare token file must not be accessible"):
        load_runtime_settings(RUNTIME_CONFIG)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw["market_data"]["cache_policy"].pop("groups"), "cache_policy.groups"),
        (
            lambda raw: raw["market_data"]["cache_policy"].update({"unknown": True}),
            "cache_policy contains unknown keys",
        ),
        (
            lambda raw: raw["market_data"]["cache_policy"].update({"policy_version": "unknown"}),
            "policy_version must be market_cache_v2",
        ),
        (
            lambda raw: raw["market_data"]["cache_policy"].update({"estimator_version": "unknown"}),
            "estimator_version must be canonical_json_utf8_v1",
        ),
        (
            lambda raw: raw["performance_budgets"]["workload"].update({"market_rows": 5499}),
            "performance workload",
        ),
        (
            lambda raw: raw["performance_budgets"]["memory"].update({"cache_total_bytes": 268435456}),
            "performance_budgets.memory contains unknown keys",
        ),
        (
            lambda raw: raw["performance_budgets"]["memory"].update({"cache_logical_bytes": 268435456}),
            "fixed 248/384 MiB",
        ),
        (
            lambda raw: raw["market_data"].update({"single_flight": False}),
            "single_flight must remain enabled",
        ),
    ],
)
def test_current_cache_and_performance_configuration_rejects_missing_unknown_or_drift(
    tmp_path, mutate, message
) -> None:
    raw = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    mutate(raw)
    changed_path = tmp_path / "runtime.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_runtime_settings(changed_path)


def test_feature_schema_contract_can_be_explicitly_reconciled_with_registered_schema(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["factor_contract"]["feature_names"] = list(FEATURE_SCHEMA_NAMES)
    raw["factor_contract"]["feature_schema_expected"] = len(FEATURE_SCHEMA_NAMES)
    strategy_path = tmp_path / "strategy.json"
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    strategy = load_strategy_settings(strategy_path)

    assert strategy.factor_contract["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert strategy.factor_contract["feature_schema_expected"] == len(FEATURE_SCHEMA_NAMES)


def test_feature_schema_contract_rejects_schema_contract_version_mismatch(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["factor_contract"]["feature_schema_version"] = "feature_schema_v0"
    strategy_path = tmp_path / "strategy.json"
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="feature_schema_version mismatch"):
        load_strategy_settings(strategy_path)


def test_research_timeout_cannot_exceed_point_in_time_source_limit(tmp_path) -> None:
    raw = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    raw["market_data"]["research_timeout_seconds"] = 8.01
    changed_path = tmp_path / "runtime.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="research_timeout_seconds must be at most 8.0"):
        load_runtime_settings(changed_path)


def test_priority_reserve_must_leave_capacity_for_normal_events(tmp_path) -> None:
    raw = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    raw["pipeline"]["priority_queue_size"] = raw["pipeline"]["event_queue_size"]
    changed_path = tmp_path / "runtime.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="priority_queue_size must be smaller"):
        load_runtime_settings(changed_path)


def test_invalid_strategy_weight_sum_is_rejected(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    source = (PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8")
    strategy_path.write_text(source.replace('"local_weight": 0.68', '"local_weight": 0.5'), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fusion weights"):
        load_strategy_settings(strategy_path)


def test_alternative_fusion_weights_are_rejected_even_when_they_sum_to_one(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["fusion"]["local_weight"] = 0.5
    raw["fusion"]["deepseek_weight"] = 0.5
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fixed at 0.68 and 0.32"):
        load_strategy_settings(strategy_path)


def test_non_finite_configuration_number_is_rejected(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["selection"]["observation_margin"] = float("nan")
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="finite"):
        load_strategy_settings(strategy_path)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    (
        ("fusion", "confidence_coverage_min", 0.1, "coverage and known-dimension"),
        ("fusion", "minimum_known_dimensions", 1, "coverage and known-dimension"),
        ("selection", "observation_margin", 6, "observation margin"),
    ),
)
def test_fixed_review_and_observation_gates_cannot_drift(
    tmp_path,
    section: str,
    field: str,
    value: float,
    message: str,
) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw[section][field] = value
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_strategy_settings(strategy_path)


@pytest.mark.parametrize("weight_family", ("candidate", "dimension", "board_candidate", "board_local"))
def test_fixed_strategy_weight_vectors_cannot_drift(tmp_path, weight_family: str) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    if weight_family == "candidate":
        weights = raw["candidate_weights"]
        first, second = "liquidity", "short_momentum"
    elif weight_family == "dimension":
        weights = raw["dimension_weights"]["tomorrow"]
        first, second = "value_quality", "financial_health"
    elif weight_family == "board_candidate":
        weights = raw["board_candidate_weights"]["tomorrow"]["main"]
        first, second = "liquidity", "trend"
    else:
        weights = raw["board_local_strategy_weights"]["tomorrow"]["main"]
        first, second = "trend", "stability"
    weights[first] += 0.01
    weights[second] -= 0.01
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fixed vector"):
        load_strategy_settings(strategy_path)


def test_deepseek_risk_mapping_version_is_required_and_fixed(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["deepseek_risk_mapping_version"] = "unsupported"
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="risk mapping version"):
        load_strategy_settings(strategy_path)


def test_incomplete_risk_trigger_contract_is_rejected(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    del raw["risk_rules"][0]["trigger"]
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="trigger"):
        load_strategy_settings(strategy_path)


def test_risk_rule_cannot_use_factor_outside_registered_strategy(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    near_limit = next(rule for rule in raw["risk_rules"] if rule["risk_code"] == "near_limit_crowding")
    near_limit["strategies"].append("long")
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="outside its registered strategies"):
        load_strategy_settings(strategy_path)


def test_risk_identity_fields_reject_non_string_values(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["risk_rules"][0]["risk_fact_id_fields"] = ["stock_code", {"invalid": True}]
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="stable identity fields"):
        load_strategy_settings(strategy_path)


def test_today_news_signal_rejects_overlapping_keyword_sets(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["today_news_signal"]["negative_keywords"].append(raw["today_news_signal"]["positive_keywords"][0])
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="must not overlap"):
        load_strategy_settings(strategy_path)


def test_today_news_signal_changes_strategy_version(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    baseline = load_strategy_settings(source)
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["today_news_signal"]["positive_keywords"].append("订单增长")
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    changed = load_strategy_settings(changed_path)

    assert changed.strategy_version != baseline.strategy_version


def test_today_news_signal_is_required(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    del raw["today_news_signal"]
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="today_news_signal must be an object"):
        load_strategy_settings(changed_path)


def test_today_news_signal_fixed_window_and_scores_cannot_drift(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["today_news_signal"]["lookback_hours"] = 48
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fixed at 72h/1h and 75/50/25"):
        load_strategy_settings(changed_path)


def test_tomorrow_tail_signal_is_required(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    del raw["tomorrow_tail_signal"]
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="tomorrow_tail_signal must be an object"):
        load_strategy_settings(changed_path)


def test_tomorrow_tail_signal_fixed_formula_cannot_drift(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["tomorrow_tail_signal"]["lookback_minutes"] = 20
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fixed at 30/30/25/50"):
        load_strategy_settings(changed_path)


def test_tomorrow_tail_signal_changes_strategy_version(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    baseline = load_strategy_settings(source)
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["tomorrow_tail_signal"]["volume_score_points_per_ratio"] = 49
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fixed at 30/30/25/50"):
        load_strategy_settings(changed_path)
    raw["tomorrow_tail_signal"]["volume_score_points_per_ratio"] = 50
    raw["factor_registry"]["tail_volume_ratio"]["version"] = "3"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    changed = load_strategy_settings(changed_path)
    assert changed.strategy_version != baseline.strategy_version


def test_tomorrow_tail_factor_registry_cannot_contradict_executable_formula(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["factor_registry"]["tail_return_30m"]["formula"] = "clamp(50+tail_return_30m_pct*10)"
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="tail_return_30m.formula"):
        load_strategy_settings(changed_path)


def test_d25_market_regime_policy_is_required_and_cannot_drift(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    del raw["market_regime"]["risk_on_breadth_min"]
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="risk_on_breadth_min"):
        load_strategy_settings(changed_path)

    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["market_regime"]["risk_on_breadth_min"] = 61
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="market regime boundaries are fixed"):
        load_strategy_settings(changed_path)


def test_long_research_contract_is_required_and_versions_keyword_changes(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    baseline = load_strategy_settings(source)
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["long_research"]["announcements"]["policy_positive_keywords"].append("设备更新")
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    changed = load_strategy_settings(changed_path)

    assert changed.strategy_version != baseline.strategy_version

    del raw["long_research"]
    changed_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="long_research must be an object"):
        load_strategy_settings(changed_path)


def test_long_research_severity_keyword_levels_cannot_overlap(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    duplicate = raw["long_research"]["announcements"]["negative_high_keywords"][0]
    raw["long_research"]["announcements"]["negative_medium_keywords"].append(duplicate)
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="severity keyword levels must not overlap"):
        load_strategy_settings(changed_path)


def test_long_factor_registry_cannot_hide_a_provider_placeholder(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["factor_registry"]["value_score"]["formula"] = "provider supplied 0-100"
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="value_score.formula"):
        load_strategy_settings(changed_path)
