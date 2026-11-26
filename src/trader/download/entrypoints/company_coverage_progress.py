"""Project formal company coverage and accepted financial source observations for the download terminal."""

from trader.download.application.company_round_progress import CompanyEventFieldCoverage, CompanyFormalCoverage
from trader.download.application.progress_message import ProgressField, ProgressRow
from trader.download.domain.company_field_results import CompanyFieldName
from trader.download.domain.company_source_records import CompanyFinancialBatchCoverage


_FINANCIAL_OBSERVATION_LABELS = {
    "basic_eps": "EPS",
    "book_value_per_share": "每股净资产",
    "revenue_growth_pct": "营收同比",
    "net_profit_growth_pct": "净利同比",
    "core_profit_growth_pct": "扣非同比",
    "roe_pct": "ROE",
    "parent_net_profit": "归母利润",
    "core_net_profit": "扣非利润",
}
_EVENT_FIELD_LABELS: dict[CompanyFieldName, str] = {
    "start_date": "减持开始日期",
    "end_date": "减持结束日期",
    "planned_pct": "计划减持比例",
    "completion_disclosed_at": "减持完成披露时间",
    "actual_pct": "回购或增持实际比例",
    "repurchase_purpose": "回购用途",
    "contract_amount": "合同金额",
    "annual_revenue": "年度营业收入",
    "revenue_report_date": "营收报告期",
    "revenue_disclosed_at": "营收披露时间",
    "contract_revenue_pct": "合同金额占营收比例",
    "amount_basis": "合同金额口径",
}


def CompanyProgressQuantity(value: int | None) -> str:
    return "未核对" if value is None else str(value)


def CompanyCoverageRate(count: int, total: int) -> str:
    return f"{count}/{total}（{count / total:.2%}）" if total else "无适用项"


def _event_field_counts(coverage: CompanyEventFieldCoverage) -> tuple[ProgressField, ...]:
    if not coverage.applicable:
        return (ProgressField(f"无适用项，不适用{coverage.not_applicable}项"),)
    return (
        ProgressField(f"适用{coverage.applicable}项"),
        ProgressField(f"已有值{CompanyCoverageRate(coverage.applicable - coverage.missing_values, coverage.applicable)}"),
        ProgressField(f"已核有值{CompanyCoverageRate(coverage.verified, coverage.applicable)}"),
        ProgressField(f"缺值{CompanyCoverageRate(coverage.missing_values, coverage.applicable)}"),
        ProgressField(f"待补证{CompanyCoverageRate(coverage.present_unverified + coverage.pending, coverage.applicable)}"),
        ProgressField(f"已核有值{coverage.verified}项，有值待核{coverage.present_unverified}项，"
            f"其他待核{coverage.pending}项，已证未披露{coverage.not_disclosed}项，不适用{coverage.not_applicable}项"),
    )


def CompanyFormalCoverageRows(coverage: CompanyFormalCoverage) -> tuple[ProgressRow, ...]:
    stocks = coverage.stocks
    financial = tuple(
        ProgressRow(_FINANCIAL_OBSERVATION_LABELS[field],
            (ProgressField(f"完成{CompanyCoverageRate(count, stocks)}股，正式缺{CompanyCoverageRate(stocks - count, stocks)}股"),))
        for field, count in coverage.financial_fields
    )
    pledge_missing = stocks - coverage.pledge_with_value - coverage.pledge_without_event
    unlock_missing = stocks - coverage.unlock_with_event - coverage.unlock_without_event
    scoring = coverage.financial_scoring_stocks
    scoring_rows = () if scoring is None else (ProgressRow(
        "D25财务评分输入", (ProgressField(f"已发布来源观测{CompanyCoverageRate(scoring, stocks)}股"),),
    ),)
    return (
        *financial,
        *scoring_rows,
        ProgressRow("质押比例", (
            ProgressField(f"已核覆盖{CompanyCoverageRate(stocks - pledge_missing, stocks)}股，待核{CompanyCoverageRate(pledge_missing, stocks)}，"
                f"有质押{coverage.pledge_with_value}股，已核无质押{coverage.pledge_without_event}股"),
        )),
        ProgressRow("未来解禁", (
            ProgressField(f"完整覆盖{CompanyCoverageRate(stocks - unlock_missing, stocks)}股，覆盖待核{CompanyCoverageRate(unlock_missing, stocks)}，"
                f"完整窗有解禁{coverage.unlock_with_event}股，已核无解禁{coverage.unlock_without_event}股，"
                f"部分覆盖已核有解禁{coverage.unlock_partial_with_event}股"),
        )),
        ProgressRow("公告风险覆盖", (
            ProgressField(f"完整覆盖{CompanyCoverageRate(coverage.announcement_complete, stocks)}股，"
                f"缺{CompanyCoverageRate(stocks - coverage.announcement_complete, stocks)}，"
                f"其中部分覆盖{CompanyCoverageRate(coverage.announcement_partial, stocks)}"),
        )),
        ProgressRow("减持事件字段", _event_field_counts(coverage.reduction_fields)),
        *(ProgressRow(_EVENT_FIELD_LABELS[field], _event_field_counts(counts))
            for field, counts in coverage.reduction_field_counts),
        ProgressRow("其他公告字段", _event_field_counts(coverage.other_fields)),
        *(ProgressRow(_EVENT_FIELD_LABELS[field], _event_field_counts(counts))
            for field, counts in coverage.other_field_counts),
    )


def CompanyFinancialBatchTotals(coverage: CompanyFinancialBatchCoverage) -> tuple[ProgressField, ...]:
    status = "页账本已闭合" if coverage.pages_complete else "页账本待闭合"
    gaps = "、".join(
        f"{_FINANCIAL_OBSERVATION_LABELS[field]}缺{count}股"
        for field, count in coverage.missing_fields if count
    )
    fields: tuple[ProgressField, ...] = (
        ProgressField(
            f"{status}，财务批量页{coverage.accepted_pages}/{CompanyProgressQuantity(coverage.expected_pages)}，"
            f"原始行{coverage.accepted_raw_rows}/{CompanyProgressQuantity(coverage.expected_raw_rows)}"
        ),
        ProgressField(
            f"财务源观察{coverage.latest_observed_stocks}/{coverage.population}股，"
            f"最新报表八项有值{coverage.latest_complete_stocks}股，"
            f"归母与扣非同时有值{coverage.latest_profit_pair_stocks}股"
        ),
        ProgressField(
            f"已观察财报原始缺值{CompanyCoverageRate(sum(count for _, count in coverage.missing_fields), coverage.latest_observed_stocks * 8)}，"
            f"观测报告有缺项{CompanyCoverageRate(coverage.latest_observed_stocks - coverage.latest_complete_stocks, coverage.latest_observed_stocks)}"
        ),
    )
    if gaps:
        fields += (ProgressField(f"有观测的最新报表 {gaps}"),)
    if coverage.market_prefix is not None:
        market = coverage.market_prefix
        fields += (ProgressField(f"逐股最新财报查询，全市场实读{market.read_pages}/{CompanyProgressQuantity(market.declared_pages)}页，"
            f"实读{market.read_rows}/{CompanyProgressQuantity(market.declared_rows)}行，定向补查{market.exact_issuer_count}股"),)
    return fields


def CompanyFinancialBatchRows(coverage: CompanyFinancialBatchCoverage) -> tuple[ProgressRow, ...]:
    observed = coverage.latest_observed_stocks
    if observed == 0:
        return (ProgressRow("来源库·东财财报字段", (ProgressField("无已取得报告，字段有值率待核"),)),)
    rows = tuple(
        ProgressRow(
            f"来源库·{_FINANCIAL_OBSERVATION_LABELS[field]}",
            (ProgressField(f"已取得报告{CompanyCoverageRate(observed, coverage.population)}股，"
                f"有值{CompanyCoverageRate(observed - count, observed)}股，缺值{CompanyCoverageRate(count, observed)}股"),),
        )
        for field, count in coverage.missing_fields
    )
    report_scope = "已入库各股最新报告" if coverage.pages_complete else "已入库页暂计各股最新报告"
    update_dates = ProgressRow("来源库·东财供应商日期", (
        ProgressField(
            f"{report_scope}{observed}股，更新日与披露日同日{CompanyCoverageRate(coverage.supplier_updates_same_day, observed)}，"
            f"晚于{CompanyCoverageRate(coverage.supplier_updates_later, observed)}，"
            f"早于{CompanyCoverageRate(coverage.supplier_updates_earlier, observed)}，"
            f"更新日缺失{CompanyCoverageRate(coverage.supplier_updates_missing, observed)}"
        ),
        ProgressField("供应商日期元数据，发行人首次披露时间及修订链仍待核"),
    ))
    return (*rows, update_dates)


def CompanyFormalCoverageSummary(
    coverage: CompanyFormalCoverage, previous: CompanyFormalCoverage | None,
) -> tuple[ProgressField, ...]:
    totals = (
        ProgressField(f"正式完整财报{coverage.financial_complete}/{coverage.stocks}股"),
        ProgressField(f"最新披露双利润{coverage.latest_profit_values}股"),
        ProgressField(f"已核风险事件涉及{coverage.verified_risk_stocks}股"),
    )
    if previous is None:
        return totals
    pledge_delta = coverage.pledge_with_value + coverage.pledge_without_event - previous.pledge_with_value - previous.pledge_without_event
    unlock_delta = coverage.unlock_with_event + coverage.unlock_without_event - previous.unlock_with_event - previous.unlock_without_event
    return (*totals, ProgressField(f"同范围完整覆盖净增 财务{coverage.financial_complete - previous.financial_complete:+d}股／"
        f"质押{pledge_delta:+d}股／解禁{unlock_delta:+d}股／公告{coverage.announcement_complete - previous.announcement_complete:+d}股"))
