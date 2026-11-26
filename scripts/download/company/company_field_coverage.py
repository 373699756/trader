"""Report per-stock field coverage from local company databases and export an Excel workbook."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from scripts.core.contracts import ToolOutcome
from scripts.core.paths import PROJECT_ROOT
from scripts.download.company.company_column_coverage import CompanyColumnCoverage, ReadCompanyColumnCoverage
from scripts.download.company.company_field_workbook import WriteCompanyFieldWorkbook
from scripts.download.company.company_source_qualification import ReadCompanyFinancialPages
from trader.download.application.company_financial_producer import ResolveCompanyFinancialScoringQuery
from trader.download.domain.company_events import CompanyDataState, CompanyRiskType
from trader.download.domain.company_source_records import CompanyFinancialSourceValues
from trader.download.domain.company_sync import CompanySyncFamily, CompanySyncPhasePlan, CompanySyncRoundScope
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import DATA_STATES, RISK_TYPES
from trader.download.infra.company_financial_observations import ReadCompanyFinancialScoringObservations, ReadRoundLatestCompanyFinancialObservations
from trader.infra.serialization.sealing import PublishImmutableFile
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction


@dataclass(frozen=True, slots=True)
class CoverageMetric:
    category: str
    field: str
    present: int
    note: str


@dataclass(frozen=True, slots=True)
class EventFieldCoverage:
    field: str
    applicable: int
    present: int
    verified: int
    with_missing_event: int


@dataclass(frozen=True, slots=True)
class CompanyFieldReport:
    round_id: str
    cutoff: datetime
    observed_at: datetime
    publication: str
    population: int
    metrics: tuple[CoverageMetric, ...]
    events: tuple[EventFieldCoverage, ...]
    missing_financial: tuple[tuple[str, tuple[str, ...]], ...]
    columns: tuple[CompanyColumnCoverage, ...]


_FINANCIAL = {
    "basic_eps": "每股收益（EPS）", "book_value_per_share": "每股净资产（BPS）",
    "revenue_growth_pct": "营业收入同比", "net_profit_growth_pct": "归母净利润同比",
    "core_profit_growth_pct": "扣非净利润同比", "roe_pct": "净资产收益率（ROE）",
    "parent_net_profit": "归母净利润", "core_net_profit": "扣非净利润",
}
_EVENTS = {
    "planned_pct": "计划减持比例", "start_date": "减持开始日期", "end_date": "减持结束日期",
    "completion_disclosed_at": "减持完成披露时间", "actual_pct": "回购或增持实际比例", "repurchase_purpose": "回购用途",
    "contract_amount": "合同金额", "annual_revenue": "合同对照年度收入", "revenue_report_date": "合同对照收入报告期",
    "revenue_disclosed_at": "对照收入披露时间", "contract_revenue_pct": "合同金额占营收比例", "amount_basis": "合同金额口径",
}
_READ_ONLY = SqliteConnectionOptions(read_only=True, query_only=True)


def _events(connection: SqliteConnection, codes: set[str]) -> tuple[EventFieldCoverage, ...]:
    # One stock can have several applicable events, some with a value and others still missing.
    by_field: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for field, code, state in connection.run("SELECT field,code,state FROM company_field_result WHERE applicable=1"):
        if code in codes:
            by_field[field][code].add(state)
    output = []
    with_value = {"verified", "present_unverified"}
    for field, label in _EVENTS.items():
        stocks = by_field[field]
        output.append(EventFieldCoverage(label, len(stocks), sum(bool(s & with_value) for s in stocks.values()),
            sum("verified" in s for s in stocks.values()), sum(bool(s - with_value) for s in stocks.values())))
    return tuple(output)


def _formal_metrics(connection: SqliteConnection, population: set[str]) -> list[CoverageMetric]:
    states = [tuple(row)[1:] for row in connection.run(
        "SELECT code,finance_state,pledge_state,unlock_state,announcement_state FROM company_facts") if row[0] in population]
    metrics = []
    for index, label in enumerate(("完整财报", "质押比例", "未来解禁窗口", "近期公告风险窗口")):
        counts = Counter(row[index] for row in states)
        complete = counts[DATA_STATES[CompanyDataState.COMPLETE]]
        metrics.append(CoverageMetric("正式完整核验", label, complete, "完成全部核验的股票。"))
        if index in (2, 3):
            metrics.append(CoverageMetric("正式至少部分覆盖", label, complete + counts[DATA_STATES[CompanyDataState.PARTIAL]],
                "包含已取得部分证据和已完整核验的股票。"))
    profits = [(parent, core) for code, parent, core in connection.run(
        "SELECT code,parent_profit,core_profit FROM company_latest_financial") if code in population]
    metrics.extend((
        CoverageMetric("正式最新披露财报", "归母净利润", sum(parent is not None for parent, _ in profits),
                       "亏损硬过滤可读取的最新财报字段。"),
        CoverageMetric("正式最新披露财报", "扣非净利润", sum(core is not None for _, core in profits),
                       "任一项已确认利润为负即可触发亏损条件。"),
    ))
    for label, table in (("已登记公告风险", "company_risk_event"), ("已登记回购、增持或合同", "company_positive_event"),
                         ("至少一笔已核解禁", "company_unlock"), ("有正式公告链接", "company_document_link")):
        present = sum(r[0] in population for r in connection.run(f"SELECT DISTINCT code FROM {table}"))
        metrics.append(CoverageMetric("正式记录覆盖", label, present, "有此类正式记录。无记录的股票保留未知。"))
    for label, kind in (("已登记发行人警示", CompanyRiskType.REGULATORY_WARNING),
                        ("已登记立案调查", CompanyRiskType.OFFICIAL_INVESTIGATION),
                        ("已登记减持风险", CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION)):
        stocks = connection.run("SELECT DISTINCT code FROM company_risk_event WHERE risk_type=?", (RISK_TYPES[kind],))
        metrics.append(CoverageMetric("正式记录覆盖", label, sum(r[0] in population for r in stocks), "按已登记事件的证券去重。"))
    return metrics


def ReadCompanyFieldReport(paths: DataPaths, observed_at: datetime) -> CompanyFieldReport:
    """Capture a stable read of the latest journal population, supplier inputs and formal publication."""
    for path in (paths.company_sync, paths.company_sources, paths.company):
        if not path.is_file():
            raise FileNotFoundError(f"缺少公司同步数据库 {path.name}")
    with (SqliteSession(paths.company_sync, _READ_ONLY) as journal, SqliteSession(paths.company_sources, _READ_ONLY) as source,
          SqliteSession(paths.company, _READ_ONLY) as formal):
        connections = (journal, source, formal)
        versions = tuple(c.run("PRAGMA data_version").require_one()[0] for c in connections)
        with SqliteTransaction(journal):
            last = journal.run("SELECT round_id,cutoff FROM company_sync_round ORDER BY active DESC,cutoff DESC,round_id LIMIT 1").fetchone()
            if last is None:
                raise ValueError("公司库尚无同步轮次")
            round_id, cutoff = last
            plans = {CompanySyncFamily(row[0]): row[1] for row in journal.run(
                "SELECT family,request_hash FROM company_sync_plan WHERE round_id=? ORDER BY position", (round_id,))}
            codes = tuple(row[0] for row in journal.run("SELECT code FROM company_sync_code WHERE round_id=? ORDER BY code", (round_id,)))
            scope = CompanySyncRoundScope(codes, datetime.fromisoformat(cutoff), tuple(plans))
            if CompanySyncFamily.FINANCIAL not in plans:
                raise ValueError("本轮没有财务采集计划")
            query = ResolveCompanyFinancialScoringQuery(scope.cutoff, CompanySyncPhasePlan(
                CompanySyncFamily.FINANCIAL, plans[CompanySyncFamily.FINANCIAL]))
        stocks = set(codes)
        with SqliteTransaction(source):
            financial_scope, pages = ReadCompanyFinancialPages(source, query, scope)
            reports = ReadRoundLatestCompanyFinancialObservations(source, financial_scope)
            ratios: dict[str, set[str]] = defaultdict(set)
            for component, code in source.run("SELECT DISTINCT component,code FROM company_ratio_observation WHERE scope=? AND ratio IS NOT NULL",
                                              (scope.identity,)):
                if code in stocks:
                    ratios[component].add(code)
        by_code = {item.financial.code: item for item in reports if item.financial.code in stocks}
        if len(by_code) != len(reports):
            raise ValueError("本轮逐股最新财报含重复股票或范围外记录")
        note = "本轮财务分页已闭合。" if pages.closed else "本轮财务分页尚未闭合，当前为暂计。"
        counts: Counter[str] = Counter()
        missing = []
        for code in codes:
            observation = by_code.get(code)
            if observation is None:
                missing.append((code, tuple(_FINANCIAL.values())))
                continue
            absent = []
            for key, value in CompanyFinancialSourceValues(observation.financial):
                if value is None:
                    absent.append(_FINANCIAL[key])
                else:
                    counts[key] += 1
            if absent:
                missing.append((code, tuple(absent)))
        metrics = [CoverageMetric("最新财务来源", label, counts[key], note) for key, label in _FINANCIAL.items()]
        metrics.extend((CoverageMetric("最新财务来源", "八项财务全部有值", len(codes) - len(missing), note),
                        CoverageMetric("最新财务来源", "报告期、披露日", len(by_code), note)))
        metrics.extend((
            CoverageMetric("结构化来源", "质押日期、比例", len(ratios["pledge"]), "显示为 0.00% 的来源比例也计为有值，舍入后仍可能有质押余额。"),
            CoverageMetric("结构化来源", "未来解禁日期、比例", len(ratios["unlock"]), "至少有一条有值观测，无记录时解禁状态仍待核。"),
        ))
        with SqliteTransaction(formal):
            identity = ReadCompanyPublicationIdentity(formal)
            if identity is None:
                raise ValueError("公司库尚无正式发布")
            visible = ReadCompanyFinancialScoringObservations(formal, codes, observed_at, identity.extraction_hash)
            matches = sum(x.observation.financial.code in by_code and
                by_code[x.observation.financial.code].record_hash == x.observation.record_hash for x in visible)
            metrics.append(CoverageMetric("D25正式读取", "与本轮最新来源报告一致", matches, "以报表检查时刻读取，逐股核对来源记录摘要。"))
            metrics.extend(_formal_metrics(formal, stocks))
            events = _events(formal, stocks)
            columns = ReadCompanyColumnCoverage(formal, codes)
        if versions != tuple(c.run("PRAGMA data_version").require_one()[0] for c in connections):
            raise ValueError("读取期间公司库发生提交，请重新运行以生成同一快照的统计")
    return CompanyFieldReport(round_id, scope.cutoff, observed_at, identity.content_hash.hex(), len(codes),
                              tuple(metrics), events, tuple(missing), columns)


def Run(action_identity: str, arguments: list[str]) -> ToolOutcome:
    if action_identity != "company-field-coverage":
        raise ValueError("unknown_company_field_coverage_action")
    parser = argparse.ArgumentParser(description="只读统计本轮公司字段和适用股票的缺值情况，导出 Excel。", allow_abbrev=False)
    parser.add_argument("--runtime-config", type=Path, default=PROJECT_ROOT / "config/runtime.json")
    parser.add_argument("--output", type=Path, help="新的 .xlsx 文件，默认写入配置的运行目录 reports/ 下。")
    args = parser.parse_args(arguments)
    settings = LoadRuntimeSettings(args.runtime_config.expanduser().resolve())
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    path = args.output or settings.runtime_dir / "reports" / f"公司字段缺值统计-{now:%Y%m%d-%H%M%S-%f}.xlsx"
    path = path.expanduser().absolute()
    if path.suffix.lower() != ".xlsx" or path.exists() or path.is_symlink():
        raise ValueError("请指定新的 .xlsx 文件路径")
    report = ReadCompanyFieldReport(settings.data_paths, now)
    with TemporaryDirectory() as directory:
        staged = Path(directory) / "fields.xlsx"
        with staged.open("wb") as stream:
            WriteCompanyFieldWorkbook(stream, report)
        if not PublishImmutableFile(path, staged):
            raise FileExistsError(path)
    return ToolOutcome(0, {"status": "passed", "workbook": str(path), "cutoff": report.cutoff.isoformat(),
        "stocks": report.population, "stocks_with_missing_financials": len(report.missing_financial),
        "physical_columns": len(report.columns)})
