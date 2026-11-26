"""Physical column presence across a caller's stock population and published snapshot."""

import json
from dataclasses import dataclass

from trader.infra.sqlite_connection import SqliteConnection


@dataclass(frozen=True, slots=True)
class CompanyColumnCoverage:
    table: str
    field: str
    stocks_with_rows: int | None
    present: int | None
    note: str


_TABLE_NOTES = {
    "company_document_link": "逐股有公告元数据记录。body_checked 非空含 0 和 1。",
    "company_evidence_ref": "经风险、利好、利好里程碑或人工复核的正式证据引用反向关联到股票。",
    "company_exclusion": "已正式发布的排除证据。无记录不能证明已核清该股资格。",
    "company_facts": "正式汇总快照。状态、时间和计数列非空不等于对应业务输入有效。",
    "company_field_evidence": "仅计算确有逐字段证据记录的股票。",
    "company_field_result": "字段核验状态的非空统计包含待核验及不适用状态。",
    "company_financial_observation": "物理表保留多个报告期或版本，每股任一保存版本有值即计入。最新已选评分观测须另按有效批次及回执核对。",
    "company_input_receipt": "正式输入接收记录，非空计入所有已保存结果类型。",
    "company_latest_financial": "正式发布的每股最新财务记录，使用此表自身的字段值。",
    "company_positive_event": "每股任一已发布利好事件，包括历史状态。金额、比例、取消等字段依事件类型及状态适用。",
    "company_positive_evidence": "经已发布利好事件关联股票。",
    "company_positive_milestone": "经已发布利好事件关联股票。仅有分级里程碑的股票可以有值。",
    "company_reduction_plan": "经对应风险事件关联股票。计划字段依公告披露情况适用。",
    "company_review": "已记录的待复核事项。无记录不能证明该股票所有字段已核清。",
    "company_risk_event": "每股任一已发布风险事件，包括历史状态。结案时间等字段依事件状态适用。",
    "company_risk_evidence": "经已发布风险事件关联股票。",
    "company_unlock": "正式解禁明细。无明细与已核实没有解禁是两种状态。",
}
_GLOBAL_NOTES = {
    "company_field_proof_rule": "工程级字段证据规则指纹，逐股分母不适用。",
    "company_normalization_commit": "工程级标准化提交，逐股分母不适用。",
    "company_publication": "工程级正式发布清单，逐股分母不适用。",
}
_EVENT_PARENTS = {
    "company_positive_evidence": ("company_positive_event", "positive_id"),
    "company_positive_milestone": ("company_positive_event", "positive_id"),
    "company_reduction_plan": ("company_risk_event", "risk_id"),
    "company_risk_evidence": ("company_risk_event", "risk_id"),
}
_EVIDENCE_STOCKS = """
    SELECT re.evidence_id, r.code FROM company_risk_evidence re JOIN company_risk_event r ON r.id=re.risk_id
    UNION
    SELECT pe.evidence_id, p.code FROM company_positive_evidence pe JOIN company_positive_event p ON p.id=pe.positive_id
    UNION
    SELECT pm.evidence_id, p.code FROM company_positive_milestone pm JOIN company_positive_event p ON p.id=pm.positive_id
    UNION
    SELECT evidence_id, code FROM company_review WHERE evidence_id IS NOT NULL
"""


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def ReadCompanyColumnCoverage(connection: SqliteConnection, codes: tuple[str, ...]) -> tuple[CompanyColumnCoverage, ...]:
    """Count a stock once per column when any related row has a non-NULL value.

    The caller owns the read transaction so these counts can share the same
    publication snapshot as the rest of its report.
    """
    population = json.dumps(codes)
    tables = connection.run("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name")
    output: list[CompanyColumnCoverage] = []
    for (table,) in tables:
        fields = tuple(row[0] for row in connection.run("SELECT name FROM pragma_table_xinfo(?) ORDER BY cid", (table,)))
        relation = f"{_identifier(table)} AS a"
        code = "a.code"
        if table in _GLOBAL_NOTES or ("code" not in fields and table not in _EVENT_PARENTS and table != "company_evidence_ref"):
            note = _GLOBAL_NOTES.get(table, "该表尚无逐股关联定义，逐股覆盖无法统计。")
            output.extend(CompanyColumnCoverage(table, field, None, None, note) for field in fields)
            continue
        if table in _EVENT_PARENTS:
            parent, key = _EVENT_PARENTS[table]
            relation += f" JOIN {_identifier(parent)} AS p ON p.id=a.{_identifier(key)}"
            code = "p.code"
        elif table == "company_evidence_ref":
            relation += f" JOIN ({_EVIDENCE_STOCKS}) AS p ON p.evidence_id=a.id"
            code = "p.code"
        flags = ", ".join(f"MAX(a.{_identifier(field)} IS NOT NULL) AS c{i}" for i, field in enumerate(fields))
        totals = ", ".join(f"COALESCE(SUM(c{i}), 0)" for i in range(len(fields)))
        row = connection.run(
            f"SELECT COUNT(*), {totals} FROM (SELECT {flags} FROM {relation} "
            f"WHERE {code} IN (SELECT value FROM json_each(?)) GROUP BY {code})", (population,),
        ).require_one()
        note = _TABLE_NOTES.get(table, "按该表的股票代码统计，每股任一行有非空值即计入。")
        output.extend(CompanyColumnCoverage(table, field, row[0], row[i], note) for i, field in enumerate(fields, start=1))
    return tuple(output)
