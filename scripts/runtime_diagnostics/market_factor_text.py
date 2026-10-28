"""Human-readable terminal projection for the market factor comparison."""

from __future__ import annotations

import json
from collections.abc import Mapping

_MARKET_FIELD_LABELS: Mapping[str, str] = {
    "price": "现价",
    "previous_close": "昨收",
    "open_price": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "pct_change": "涨跌幅",
    "amount": "成交额",
    "volume": "累计成交股数",
    "turnover_rate": "换手率",
    "volume_ratio": "供应商量比",
    "market_cap": "总市值",
    "amplitude": "振幅",
    "speed": "涨速",
    "change_5m": "五分钟涨跌",
    "industry": "行情行业",
    "model_industry": "证监会模型行业",
}
_MODEL_REASON_LABELS: Mapping[str, str] = {
    "eligible": "合格",
    "qfq_window_missing": "无qfq窗口",
    "qfq_window_short": "qfq交易日不足",
    "quote_or_feature_missing": "报价或特征缺失",
    "model_industry_missing": "模型行业缺失",
    "model_fields_or_industry_invalid": "模型字段或行业无效",
}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _field_names(value: object) -> str:
    return "、".join(f"{_MARKET_FIELD_LABELS.get(str(item), str(item))}({item})" for item in _list(value)) or "无"


def _timing_text(value: object) -> str:
    timing = _mapping(value)

    def seconds(name: str) -> str:
        item = timing.get(name)
        return f"{float(item) / 1000:.3f}秒" if isinstance(item, (int, float)) else "无合格样本"

    return f"P50={seconds('p50_ms')}，P95={seconds('p95_ms')}，最大={seconds('maximum_ms')}"


def _count_text(value: object) -> str:
    counts = _mapping(value)
    median = counts.get("p50_ms")
    maximum = counts.get("maximum_ms")
    if not isinstance(median, (int, float)) or not isinstance(maximum, (int, float)):
        return "无合格样本"
    return f"中位={int(median)}，最多={int(maximum)}"


def _span_text(value: object) -> str:
    spans = _mapping(value)
    maximum = spans.get("maximum_ms")
    return f"最大={float(maximum):.3f}秒" if isinstance(maximum, (int, float)) else "来源未提供可验证时间"


def _milliseconds_text(value: object) -> str:
    return f"{float(value) / 1000:.3f}秒" if isinstance(value, (int, float)) else "无合格样本"


def _field_coverage_text(value: object) -> list[str]:
    fields = _mapping(value)
    partial = []
    for item in _list(fields.get("partial_fields")):
        if not isinstance(item, dict):
            continue
        name = str(item.get("field"))
        minimum = item.get("minimum_coverage")
        maximum = item.get("maximum_coverage")
        partial.append(
            f"{_MARKET_FIELD_LABELS.get(name, name)}({name}) {float(minimum) * 100:.2f}%~{float(maximum) * 100:.2f}%"
            if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float))
            else f"{_MARKET_FIELD_LABELS.get(name, name)}({name})"
        )
    return [
        f"    完整字段：{_field_names(fields.get('complete_fields'))}",
        f"    部分字段：{'、'.join(partial) or '无'}",
        f"    缺失字段：{_field_names(fields.get('unavailable_fields'))}",
        f"    未测字段：{_field_names(fields.get('unmeasured_fields'))}",
    ]


def RenderMarketFactorText(report: Mapping[str, object]) -> str:
    """Render the compact comparison report without exposing its JSON envelope."""
    checks = _list(report.get("checks"))
    check = _mapping(checks[0]) if checks else {}
    summary = _mapping(check.get("summary"))
    if "error" in summary:
        return f"行情字段与耗时实测失败：{summary['error']}\n"
    lines = [
        "行情字段与耗时实测",
        f"总体：官方{summary.get('official_population', 0)}只，一级过滤后"
        f"{summary.get('level_one_eligible_population', 0)}只，排除{summary.get('level_one_excluded_population', 0)}只",
    ]
    official = _mapping(summary.get("official_reference"))
    if official:
        lines.append(
            "证券身份："
            + (
                "本轮交易所校验"
                if official.get("mode") == "fresh_exchange"
                else "交易所失败，复用本地已验证身份（降级）"
            )
        )
    eligibility = _mapping(summary.get("level_one_eligibility"))
    lines.append(
        f"一级资格：状态={eligibility.get('refresh_state')}，事实={eligibility.get('fact_count')}，"
        f"完整性={'通过' if eligibility.get('integrity_ok') is True else '失败'}"
    )
    owners = _mapping(summary.get("input_owners"))
    lines.extend(
        (
            f"实时必需字段：{_field_names(owners.get('required_realtime_fields'))}",
            f"本地行业库字段：{_field_names(owners.get('local_industry_fields'))}",
            f"可由本地历史或其它字段推导、不触发补源：{_field_names(owners.get('derivable_realtime_fields'))}",
            f"仅在主派生缺失时备用、不触发补源：{_field_names(owners.get('optional_fallback_realtime_fields'))}",
            f"仅作结构校验、不触发补源：{_field_names(owners.get('validation_realtime_fields'))}",
            f"当前评分链忽略且不请求：{_field_names(owners.get('ignored_realtime_fields'))}",
            "qfq：读取本地SQLite，不计入四源缺失与网络耗时",
        )
    )
    budget = _mapping(summary.get("test_budget"))
    lines.append(
        f"测试预算：每源循环最多{budget.get('source_loop_limit_seconds')}秒，"
        f"单任务最多{budget.get('task_limit_seconds')}秒"
    )
    for exclusion in _list(budget.get("excluded_sources")):
        if isinstance(exclusion, dict):
            lines.append(
                f"  已排除 {exclusion.get('source')}：{exclusion.get('reason')}，"
                f"累计{_milliseconds_text(exclusion.get('elapsed_ms'))}"
            )
    for item in _list(summary.get("scoring_fields")):
        if not isinstance(item, dict):
            continue
        lines.append(f"评分派生字段（{item.get('profile')}）：")
        for owner, fields in _mapping(item.get("freeze_fields_by_owner")).items():
            lines.append(f"  {owner}：{_field_names(fields)}")
        lines.append(f"  model_gate：{_field_names(item.get('model_gate_features'))}")
    lines.extend(("", "一、单源结果"))
    for source in _list(summary.get("source_results")):
        if not isinstance(source, dict):
            continue
        if source.get("kind") == "local_reference":
            lines.append(
                f"  baostock：类型=本地证监会行业参考，范围={source.get('scope')}，"
                f"网络={source.get('network_status')}，模式={source.get('mode')}，"
                f"逻辑覆盖={source.get('returned_rows')}/{source.get('requested_rows')}"
                f"({float(source.get('coverage', 0)) * 100:.2f}%)，物理返回={source.get('physical_rows')}"
            )
            lines.append(f"    本地读取耗时：{_timing_text(source.get('request_timings_ms'))}")
            lines.append(
                f"    网络采集={source.get('network_fetch_rounds')}轮，缓存复用={source.get('cache_reuse_rounds')}轮，"
                f"无效行={source.get('invalid_rows')}"
            )
            lines.append(f"    可提供参考字段：{_field_names(source.get('supported_reference_fields'))}")
            lines.append(f"    不支持瞬时行情字段：{_field_names(source.get('unsupported_instant_quote_fields'))}")
            if source.get("error"):
                lines.append(f"    失败：{source.get('error')}（存在有效缓存时仍单独标明网络失败）")
            continue
        lines.append(
            f"  {source.get('source')}：范围={source.get('scope')}，成功={source.get('successful_rounds')}/"
            f"{source.get('rounds')}，核心完整={source.get('core_complete_rounds')}轮，"
            f"返回行 {_count_text(source.get('returned_rows'))}，物理行 {_count_text(source.get('physical_rows'))}"
        )
        lines.append(f"    网络采集耗时：{_timing_text(source.get('request_timings_ms'))}")
        lines.append(f"    采集至合并校验完成：{_timing_text(source.get('data_ready_timings_ms'))}")
        lines.append(f"    来源时间跨度：{_span_text(source.get('source_time_span_seconds'))}")
        errors = _mapping(source.get("errors"))
        if errors:
            lines.append(f"    失败：{json.dumps(errors, ensure_ascii=False, sort_keys=True)}")
        lines.extend(_field_coverage_text(source.get("fields")))
    lines.extend(("", "二、组合合并后的评分输入（含官方与本地参考字段）"))
    for route in _list(summary.get("route_results")):
        if not isinstance(route, dict):
            continue
        timings = _mapping(route.get("timings_ms"))
        lines.append(
            f"  {route.get('route')}：调度={route.get('scheduling')}，"
            f"来源={','.join(str(item) for item in _list(route.get('sources')))}，"
            f"成功={route.get('measured_rounds')}/{route.get('rounds')}，核心完整={route.get('core_complete_rounds')}轮，"
            f"评分行情字段完整={route.get('scoring_market_fields_complete_rounds')}轮，"
            f"选股行情字段完整={route.get('selection_market_fields_complete_rounds')}轮，"
            f"全部行情字段完整={route.get('all_market_fields_complete_rounds')}轮，"
            f"模型行业完整={route.get('model_industry_complete_rounds')}轮，"
            f"模型行业最好覆盖={float(route.get('model_industry_coverage_best', 0)) * 100:.2f}%，"
            f"来源输入完整={route.get('source_owned_inputs_complete_rounds')}轮，"
            f"采集至合并校验完成 {_timing_text(timings.get('market_data_ready_ms'))}"
        )
        lines.append(f"    纯网络调度耗时：{_timing_text(timings.get('acquisition_ms'))}")
        lines.append(
            f"    输入封口耗时：{_timing_text(timings.get('input_seal_ms'))}；"
            f"本地评分诊断：{_timing_text(timings.get('scoring_ms'))}；"
            f"诊断总耗时：{_timing_text(timings.get('total_ms'))}"
        )
        lines.append(
            f"    连续交易={route.get('continuous_trading_rounds')}/{route.get('measured_rounds')}轮；"
            f"完整评分输入={route.get('complete_scoring_input_rounds')}/{route.get('measured_rounds')}轮，"
            f"合格输入封口耗时 {_timing_text(route.get('complete_scoring_input_timings_ms'))}"
        )
        lines.append(f"    来源时间跨度：{_span_text(route.get('source_time_span_seconds'))}")
        lines.extend(_field_coverage_text(route.get("fields")))
    recommended = _mapping(summary.get("recommended_plan"))
    lines.extend(
        (
            "",
            "三、推荐方案",
            f"  路线：{recommended.get('selected_route')}（{recommended.get('scheduling')}）",
            f"  行业：启动读取本地SQLite，耗时="
            f"{_milliseconds_text(recommended.get('daily_reference_prepare_ms'))}；BaoStock每七天独立刷新。",
            f"  尚缺来源输入：{_field_names(recommended.get('unresolved_source_inputs'))}",
            "  选择规则：腾讯承担全市场主路，失败才试新浪；按完整度、成功轮次和P95采集耗时选择。",
        )
    )
    scoring = _list(summary.get("scoring_readiness"))
    if scoring:
        lines.extend(("", "四、候选增强与本地输入"))
    for item in scoring:
        if not isinstance(item, dict):
            continue
        missing = _mapping(item.get("independent_missing_fields"))
        qfq = _mapping(item.get("qfq"))
        lines.append(
            f"  {item.get('profile')}：本地qfq完整窗最好={qfq.get('complete_rows_best')}只，"
            f"无窗口={qfq.get('missing_rows')}只，仅少1日={qfq.get('one_session_short_rows')}只；"
            f"分钟缺失={_field_names(missing.get('intraday_minutes'))}；"
            f"研究缺失={_field_names(missing.get('research_reference'))}"
        )
        missing_codes = _list(qfq.get("missing_codes"))
        short_windows = _mapping(qfq.get("short_windows"))
        if missing_codes or short_windows:
            lines.append(
                f"    qfq缺窗代码：{','.join(str(code) for code in missing_codes[:20]) or '无'}"
                f"{'…' if len(missing_codes) > 20 else ''}；"
                f"短窗代码/根数：{','.join(f'{code}/{length}' for code, length in list(short_windows.items())[:20]) or '无'}"
                f"{'…' if len(short_windows) > 20 else ''}"
            )
        for strategy, counts in _mapping(item.get("model_input_reason_counts")).items():
            details = "，".join(
                f"{_MODEL_REASON_LABELS.get(reason, reason)}={count}只"
                for reason, count in _mapping(counts).items()
                if isinstance(count, int) and count > 0
            )
            lines.append(f"    {strategy}模型输入原因：{details or '无样本'}")
    lines.append("注：诊断总耗时不含真实服务的原子发布、SSE与浏览器渲染，不能当作完整T1到Web周期。")
    return "\n".join(lines) + "\n"


__all__ = ["RenderMarketFactorText"]
