"""Display descriptions derived from facts, never copied from announcement titles."""

from trader.download.domain.company_events import CompanyPositiveType, CompanyRiskType

_RISKS = {
    CompanyRiskType.REGULATORY_WARNING: "监管警示",
    CompanyRiskType.ADMINISTRATIVE_PENALTY: "行政处罚",
    CompanyRiskType.DISCIPLINARY_ACTION: "纪律处分",
    CompanyRiskType.FINANCIAL_FRAUD: "财务造假",
    CompanyRiskType.OFFICIAL_INVESTIGATION: "正式立案调查",
    CompanyRiskType.MAJOR_ILLEGAL: "重大违法",
    CompanyRiskType.FUND_OCCUPATION: "资金占用",
    CompanyRiskType.ILLEGAL_GUARANTEE: "违规担保",
    CompanyRiskType.FORCED_DELISTING: "强制退市",
    CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION: "大股东减持",
}
_POSITIVES = {
    CompanyPositiveType.SHARE_REPURCHASE: "股份回购",
    CompanyPositiveType.SHAREHOLDER_INCREASE: "股东增持",
    CompanyPositiveType.OPERATING_CONTRACT: "经营合同",
}
_REVIEWS = {
    "document_pending": "公告正文尚未完整提取",
    "official_source_unverified": "公告来源未核实",
    "subject_unverified": "正文主体无法确认",
    "conclusion_unverified": "正式结论尚未核实",
    "case_conclusion_ambiguous": "案件或肯否结论无法唯一绑定",
    "resolution_unlinked": "解除公告未关联原事件",
    "revision_pending": "更正结论待核实，保留已确认风险",
    "cancellation_unlinked": "取消公告未关联原事件",
    "progress_unlinked": "进展公告未关联原事件",
    "implementation_or_measurement_unverified": "利好实施或数值尚未核实",
    "necessary_data_missing": "必要资料缺失",
    "not_current": "当前期资料尚未核实",
    "source_stale": "来源资料已过期",
    "window_incomplete": "所需窗口覆盖不完整",
    "initial_risk_state_unverified": "持续事项的上市初始状态尚未核实",
    "source_task_pending": "来源采集尚未完成",
    "source_unavailable": "来源暂不可用",
    "access_denied": "来源拒绝访问",
    "rate_limited": "来源请求受限",
    "timeout": "来源请求或解析超时",
    "schema_invalid": "来源字段校验失败",
    "coverage_mismatch": "来源范围或数量证据不一致",
    "filter_ignored": "来源未遵守查询范围",
    "source_changed": "来源身份变化，需要重新核实",
    "extraction_unsupported": "当前解析器无法可靠提取",
}


def CompanyRiskDescription(kind: CompanyRiskType) -> str:
    return _RISKS[kind]


def CompanyPositiveDescription(kind: CompanyPositiveType) -> str:
    return _POSITIVES[kind]


def CompanyReviewDescription(reason: str) -> str:
    return _REVIEWS[reason]


__all__ = ["CompanyRiskDescription", "CompanyPositiveDescription", "CompanyReviewDescription"]
