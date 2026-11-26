"""Bind one declared qualifying holder's numbered plan without merging other actors."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.domain.company_events import CompanySubject, ReductionPlan
from trader.download.domain.company_evidence import CompanyDocument


_SUMMARY = r"特别提示[：:](.{1,5000}?)(?=(?:本公司|公司)于近日收到)"
_NUMBER = r"(?:^|(?<=[。；;]))([1-8])[、.]"
_DECLARATION = r"(?:持股5%以上股东|5%以上股东)([^、，,。；;：:0-9]{2,100})(?=[、，,。；;])"
_HOLDER = r"股东([^，,。；;：:]{2,100}?)[（(]以下简称[“\"]([\u4e00-\u9fffA-Za-z]{2,30})[”\"][）)]"
_HOLDER_CONTEXT = r"(?:为公司首次公开发行股票上市前股东[，,])?"
_HOLDING = (
    r"(?:合计)?持有公司股份([0-9]+(?:[,，][0-9]{3})*)股[，,]?[（(]占(?:本公司|公司)总股本比例(?:为)?"
    r"([0-9]+(?:\.[0-9]+)?)[%％](?:[，,]占剔除公司回购专用账户股份后总股本比例(?:为)?"
    r"[0-9]+(?:\.[0-9]+)?[%％])?[）)]"
)
_PLAN = (
    r"([\u4e00-\u9fffA-Za-z]{2,30})计划自本公告(?:发布|披露)之日起"
    r"[一二三四五六七八九十百0-9]{1,4}个交易日后的[一二三四五六七八九十0-9]{1,3}个月内"
    r"[（(]即(20\d{2})年(\d{1,2})月(\d{1,2})日至(20\d{2})年(\d{1,2})月(\d{1,2})日[）)][，,]"
    r"以(?:集中竞价交易方式|大宗交易方式)(?:或(?:集中竞价交易方式|大宗交易方式))?"
    r"减持(?:公司)?股份(?:数量合计|累计)?不超过([0-9]+(?:[,，][0-9]{3})*)股[（(]"
    r"占(?:本公司|公司)总股本(?:的|比例(?:为)?)([0-9]+(?:\.[0-9]+)?)[%％]"
    r"(?:[，,]占剔除公司回购专用账户股份后总股本的[0-9]+(?:\.[0-9]+)?[%％])?[）)][。]?"
)
_OTHER_ACTOR = r"^(?:本公司|公司)(?:原)?(?:董事|高级管理人员|常务副总经理|副总经理|财务总监)"
_OTHER_QUALIFICATION = r"控股股东|实际控制人|一致行动人|持股5%以上|5%以上股东"
_OUTSIDE_ACTOR = r"子公司|参股公司|一致行动人|分别|各自"
_SINGLE_SUMMARY = r"特别提示[：:](.{1,2500}?)(?=(?:一[、．]|(?:本公司|公司)(?:于)?近日收到))"
_SINGLE_ISSUER = (
    r"本公司|[\u4e00-\u9fffA-Za-z（）()]{2,80}股份有限公司[（(](?:以下简称|简称)[“\"]公司[”\"]"
    r"(?:或[“\"]本公司[”\"])?[）)]"
)
_SINGLE_HOLDING = (
    r"持有(?P<issuer>" + _SINGLE_ISSUER + r")(?:股份)?(?P<quantity>[0-9]+(?:[,，][0-9]{3})*)(?:\.00)?股[（(]"
    r"占(?:本公司|公司)总股本(?:比例)?(?P<ratio>[0-9]+(?:\.[0-9]+)?)[%％][）)]的"
    r"(?P<role>控股股东|(?:持股)?5%以上股东|大股东|股东)(?P<holder>[^。；;：:]{1,180}?)(?=计划)"
)
_SINGLE_PLAN = (
    r"计划[自在]?(?:自)?本(?:次)?公告(?:披露|发布)之日起[一二三四五六七八九十百0-9]{1,4}个交易日后的"
    r"[三3]个月内[（(](?:即)?(20\d{2})年(\d{1,2})月(\d{1,2})日至(20\d{2})年(\d{1,2})月(\d{1,2})日[）)]"
    r"以((?:(?:集中竞价(?:交易)?|大宗交易)(?:方式)?(?:或|、)?){1,2})(?:方式)?"
    r"减持(?:本公司|公司)股份(?:合计)?(?:不超过)?([0-9]+(?:[,，][0-9]{3})*)(?:\.00)?股[（(]"
    r"(?:即)?(?:占|不超过)(?:本公司|公司)总股本(?:比例)?(?:不超过|的)?([0-9]+(?:\.[0-9]+)?)[%％][）)]"
)
_SINGLE_ALIAS = r"[（(](?:以下简称|简称)[“\"]([^”\"]{1,40})[”\"](?:或[“\"]股东[”\"])?[）)]"
_SINGLE_ACTOR = (
    r"(?:控股股东|实际控制人|(?:持股)?5%以上股东|大股东|股东)"
    r"([^。；;，,：:]{1,180}?)(?=计划(?:[自在]|减持)|拟减持)"
)
COMPANY_REDUCTION_NONPARTICIPANT_DECLARATION = (
    r"(?:不属于|并非|不是)(?:本公司|公司)?的?(?:控股股东|实际控制人)(?:的?一致行动人)?"
)
_EMPLOYEE_HOLDING = (
    r"持有本公司股份([0-9]+(?:[,，][0-9]{3})*)(?:\.00)?股[（(]占本公司总股本([0-9]+(?:\.[0-9]+)?)[%％][）)]的"
    r"([^。；;]{2,150}?员工参与(?:主板|创业板|科创板)战略配售集合资产管理计划)" + _SINGLE_ALIAS
)
_ADDITIONAL_ACTOR = r"(?:^|[。；;：:])(?:持股5%以上股东|大股东|股东)([^。；;，,：:]{1,150}?)(?=计划(?:[自在]|减持)|拟减持)"
_GROUP_DECLARATION = r"(?m)^控股股东及其一致行动人([^、。]{2,150})、([^、。]{2,150})保证"
_INTERVAL = r"(20\d{2})年(\d{1,2})月(\d{1,2})日至(20\d{2})年(\d{1,2})月(\d{1,2})日"
_GROUP_PERIOD = (
    r"([\u4e00-\u9fffA-Za-z]{2,30})、([\u4e00-\u9fffA-Za-z]{2,30})将于本减持计划公告之日起"
    r"[一二三四五六七八九十0-9]{1,4}个交易日之后[（(]即" + _INTERVAL + r"[）)]的三个月内进行"
)
_JUDICIAL_TITLE = r"(?:[\u4e00-\u9fffA-Za-z]{2,60}股份有限公司)?(?:关于)?控股股东被动减持股份的提示性公告"
_JUDICIAL_QUANTITY = r"(?:[0-9]{1,15}|[0-9]{1,3}(?:[,，][0-9]{3}){1,4})(?:\.[0-9]{1,4})?"
_JUDICIAL_CASE = r"[（(]20[0-9]{2}[）)][\u4e00-\u9fff][0-9]{1,4}执[0-9]{1,8}号(?:之[一二三四五六七八九十0-9]+)?"
_JUDICIAL_ORDER = (
    r"裁定被执行人(?P<holder>[\u4e00-\u9fffA-Za-z]{2,30})所在的证券公司营业部"
    r"在收到《执行裁定书》之日起[1-9][0-9]{0,2}个交易日内"
    r"(?:[（(]具体由证券公司营业部在相关股份可售状态变更完成之日起[1-9][0-9]{0,2}个交易日内[）)])?"
    r"[，,]通过证券交易所集中竞价交易方式[，,](?P<sale>将被执行人(?P=holder)名下持有的"
    r"(?P<issuer>[\u4e00-\u9fffA-Za-z]{2,24})(?P<quantity>" + _JUDICIAL_QUANTITY + r")(?P<unit>万?)股股票"
    r"[（(]占公司总股本的(?P<ratio>[0-9]+(?:\.[0-9]+)?)[%％][）)]予以卖出)"
)
COMPANY_QUALIFIED_REDUCTION_DIGEST = hashlib.sha256(
    "\0".join((_SUMMARY, _NUMBER, _DECLARATION, _HOLDER, _HOLDER_CONTEXT, _HOLDING, _PLAN,
               _OTHER_ACTOR, _OTHER_QUALIFICATION, _OUTSIDE_ACTOR, _SINGLE_SUMMARY, _SINGLE_ISSUER, _SINGLE_HOLDING,
               _SINGLE_PLAN, _SINGLE_ALIAS, _SINGLE_ACTOR, COMPANY_REDUCTION_NONPARTICIPANT_DECLARATION,
               _EMPLOYEE_HOLDING, _ADDITIONAL_ACTOR, _GROUP_DECLARATION, _INTERVAL, _GROUP_PERIOD)).encode("utf-8"),
).digest()


@dataclass(frozen=True, slots=True)
class CompanyQualifiedReduction:
    start_date: date | None
    end_date: date | None
    planned_ratio_pct: float | None
    statement: str
    case_id: str | None = None

    @property
    def plan(self) -> ReductionPlan:
        return ReductionPlan(self.start_date, self.end_date, self.planned_ratio_pct)


def ExtractCompanyQualifiedReduction(document: CompanyDocument, subject: CompanySubject) -> CompanyQualifiedReduction | None:
    title = "".join(document.disclosure.title.split())
    if IsCompanyJudicialReductionNotice(title):
        return _judicial_plan(document) if subject is CompanySubject.CONTROLLING_SHAREHOLDER else None
    if (subject not in {CompanySubject.MAJOR_SHAREHOLDER, CompanySubject.CONTROLLING_SHAREHOLDER} or len(document.disclosure.codes) != 1
        or "预披露" not in title or "减持" not in title
        or any(word in title for word in ("更正", "修订", "变更", "补充"))
        or ExtractCompanyAnnouncementNumbers(document.text).issuer_codes != document.disclosure.codes):
        return None
    body = "".join(document.text.split())
    group = _group_period(document, body, subject)
    if group is not None:
        return group
    employee = _employee_plan(document, body, subject)
    if employee is not None:
        return employee
    single = _single_holder(document, body, subject)
    if single is not None:
        return single
    if subject is not CompanySubject.MAJOR_SHAREHOLDER:
        return None
    summaries = tuple(re.finditer(_SUMMARY, body))
    if len(summaries) != 1:
        return None
    summary = summaries[0]
    declarations = {match[1] for match in re.finditer(_DECLARATION, body[:summary.start()])}
    if len(declarations) != 1:
        return None
    numbered = tuple(re.finditer(_NUMBER, summary[1]))
    if (not 2 <= len(numbered) <= 8 or numbered[0].start() != 0
        or tuple(int(match[1]) for match in numbered) != tuple(range(1, len(numbered) + 1))):
        return None
    target: tuple[str, re.Match[str], re.Match[str]] | None = None
    for position, marker in enumerate(numbered):
        part_end = numbered[position + 1].start() if position + 1 < len(numbered) else len(summary[1])
        part = summary[1][marker.end():part_end]
        holdings = tuple(re.finditer(_HOLDING, part))
        if len(holdings) != 1:
            return None
        holding = holdings[0]
        try:
            holding_quantity = int(holding[1].replace(",", "").replace("，", ""))
        except ValueError:
            return None
        if holding_quantity <= 0:
            return None
        ratio = float(holding[2])
        if not 0 <= ratio <= 100:
            return None
        if ratio < 5:
            if re.search(_OTHER_ACTOR, part) is None or re.search(_OTHER_QUALIFICATION, part) is not None:
                return None
            continue
        holders = tuple(re.finditer(_HOLDER, part))
        if (target is not None or len(holders) != 1 or holders[0][1] not in declarations
            or holders[0].end() > holding.start() or re.search(_OUTSIDE_ACTOR, part) is not None
            or re.fullmatch(_HOLDER_CONTEXT, part[holders[0].end():holding.start()]) is None):
            return None
        target = (part, holders[0], holding)
    if target is None:
        return None
    part, holder, holding = target
    plans = tuple(re.finditer(_PLAN, part))
    if (len(plans) != 1 or plans[0][1] != holder[2] or plans[0].start() < holding.end()
        or re.fullmatch(r"[，,]?", part[holding.end():plans[0].start()]) is None or plans[0].end() != len(part)):
        return None
    plan = plans[0]
    try:
        start = date(*(int(plan[i]) for i in (2, 3, 4)))
        end = date(*(int(plan[i]) for i in (5, 6, 7)))
        quantity = int(plan[8].replace(",", "").replace("，", ""))
    except ValueError:
        return None
    ratio = float(plan[9])
    if start > end or not 0 <= ratio <= 100 or quantity <= 0:
        return None
    return CompanyQualifiedReduction(start, end, ratio, plan[0])


def IsCompanyJudicialReductionNotice(title: str) -> bool:
    return re.fullmatch(_JUDICIAL_TITLE, "".join(title.split())) is not None


def _judicial_plan(document: CompanyDocument) -> CompanyQualifiedReduction | None:
    # The court order and the issuer's single-holder plan table must independently agree.
    if len(document.disclosure.codes) != 1 or ExtractCompanyAnnouncementNumbers(document.text).issuer_codes != document.disclosure.codes:
        return None
    body = "".join(document.text.split())
    summary, separator, plan = body.partition("二、减持计划的主要内容")
    if (not separator or body.count(separator) != 1 or "上述减持主体无一致行动人" not in summary
        or "截至本公告披露日，上述股票尚未被处置" not in body
        or any(marker in body for marker in ("撤回", "撤销", "中止执行", "终结执行", "暂缓执行", "取消执行", "已被处置", "处置完毕",
                                              "未收到", "若收到", "如收到", "拟申请执行", "不予执行",
                                              "分别", "各自", "共同减持", "其他股东", "另有股东", "其他被执行人"))):
        return None
    issuers = re.findall(r"(?:证券|股票)简称[：:]([^\s：:，,]{2,24})", document.text)
    declarations = re.findall(r"公司控股股东([\u4e00-\u9fffA-Za-z]{2,80}有限公司)[（(]以下简称[“\"]([\u4e00-\u9fffA-Za-z]{2,30})[”\"]", summary)
    cases = re.findall(r"收到[^。；;]{2,80}人民法院[^。；;]{0,80}发出的《执行裁定书》[〔【](%s)[〕】]" % _JUDICIAL_CASE, summary)
    orders = tuple(re.finditer(r"[，,]" + _JUDICIAL_ORDER, summary))
    if len(issuers) != 1 or len(declarations) != 1 or len(cases) != 1 or len(orders) != 1 or re.findall(_JUDICIAL_CASE, body) != cases:
        return None
    holder, alias = declarations[0]
    order = orders[0]
    if (re.search(r"[及与和]", holder) or order["holder"] != alias or order["issuer"] != issuers[0]
        or summary.count("裁定被执行人") != 1 or body.count("予以卖出") != 1):
        return None
    table = plan.partition("减持方式及对应减持数量")[0]
    match = re.fullmatch(r"股东名称" + re.escape(holder) + r"计划减持数量(" + _JUDICIAL_QUANTITY
                         + r")股计划减持比例([0-9]+(?:\.[0-9]+)?)[%％]", table)
    if match is None or plan.count("股东名称") != 1 or plan.count("计划减持比例") != 1:
        return None
    quantity = Decimal(re.sub("[,，]", "", order["quantity"])) * (10000 if order["unit"] == "万" else 1)
    ratio = Decimal(order["ratio"])
    if (quantity <= 0 or quantity != quantity.to_integral_value() or quantity != Decimal(re.sub("[,，]", "", match[1]))
        or not 0 < ratio <= 100 or ratio != Decimal(match[2])):
        return None
    return CompanyQualifiedReduction(None, None, float(ratio), order["sale"], cases[0])


def _single_holder(document: CompanyDocument, body: str, subject: CompanySubject) -> CompanyQualifiedReduction | None:
    summaries = tuple(re.finditer(_SINGLE_SUMMARY, body))
    if len(summaries) != 1:
        return None
    summary = summaries[0][1]
    holdings = tuple(re.finditer(_SINGLE_HOLDING, summary))
    plans = tuple(re.finditer(_SINGLE_PLAN, summary))
    if (len(holdings) != 1 or len(plans) != 1 or re.search(_OUTSIDE_ACTOR, summary)
        or re.search(r"(?:尚未|并未|未|不会|不是|如|若|假设|取消|终止|不再)计划", summary)):
        return None
    holding, plan = holdings[0], plans[0]
    participants = re.sub(COMPANY_REDUCTION_NONPARTICIPANT_DECLARATION, "", re.sub(r"《[^》]{0,160}》", "", body))
    if (holding.start() != 0 or holding.end() != plan.start()
        or re.search(r"一致行动人|分别|各自|(?:另|其他)股东", participants)
        or any(match[0] != plan[0] for match in re.finditer(_SINGLE_PLAN, body))
        or (subject is CompanySubject.CONTROLLING_SHAREHOLDER) != (holding["role"] == "控股股东")):
        return None
    if holding["issuer"] != "本公司":
        issuer = re.split(r"[（(](?:以下简称|简称)", holding["issuer"])[0]
        header = tuple(line for line in ("".join(line.split()) for line in document.text.splitlines()) if line)[:24]
        names = {line for line in header if re.fullmatch(r"[\u4e00-\u9fffA-Za-z（）()]{2,80}股份有限公司", line)}
        if names != {issuer}:
            return None
    holder = holding["holder"].rstrip("，,")
    aliases = tuple(re.finditer(_SINGLE_ALIAS, holder))
    name = re.sub(_SINGLE_ALIAS, "", holder)
    names = {name, *(match[1] for match in aliases)}
    if (not name or len(name) > 150 or re.search(r"[、及与和]|(?:另|其他)股东", name)
        or any(match[1] in {"公司", "本公司", "股东", "大股东", "实际控制人"} for match in aliases)
        or any(re.sub(_SINGLE_ALIAS, "", match[1].rstrip("，,")) not in names for match in re.finditer(_SINGLE_ACTOR, body))):
        return None
    remainder = summary[plan.end():]
    if remainder and not re.fullmatch(r"[。]?", remainder) and not remainder.startswith(("。其中", "其中", "。通过", "通过")):
        return None
    try:
        start = date(*(int(plan[i]) for i in (1, 2, 3)))
        end = date(*(int(plan[i]) for i in (4, 5, 6)))
        holding_quantity = int(holding["quantity"].replace(",", "").replace("，", ""))
        quantity = int(plan[8].replace(",", "").replace("，", ""))
    except ValueError:
        return None
    if (start > end or not 5 <= float(holding["ratio"]) <= 100 or not 0 < quantity <= holding_quantity
        or not 0 < float(plan[9]) <= float(holding["ratio"])):
        return None
    return CompanyQualifiedReduction(start, end, float(plan[9]), summary[:plan.end()])


def _employee_plan(document: CompanyDocument, body: str, subject: CompanySubject) -> CompanyQualifiedReduction | None:
    if subject is not CompanySubject.MAJOR_SHAREHOLDER:
        return None
    summaries = tuple(re.finditer(_SINGLE_SUMMARY, body))
    if len(summaries) != 1:
        return None
    summary = summaries[0][1]
    holders = tuple(re.finditer(_SINGLE_HOLDING, summary))
    employees = tuple(re.finditer(_EMPLOYEE_HOLDING, summary))
    plans = tuple(re.finditer(_SINGLE_PLAN, summary))
    clean = re.sub(r"《[^》]{0,160}》", "", body)
    if (len(holders) != 1 or len(employees) != 1 or len(plans) != 2
        or re.search(r"控股股东|实际控制人|一致行动人|子公司|参股公司|其他股东", clean)):
        return None
    holder, employee = holders[0], employees[0]
    first, second = plans
    allowed = {holder["holder"], employee[3], employee[4]}
    if any(re.sub(_SINGLE_ALIAS, "", match[1]) not in allowed for match in re.finditer(_ADDITIONAL_ACTOR, clean)):
        return None
    if (holder.start() != 0 or holder.end() != first.start() or employee.end() != second.start()
        or re.fullmatch(r"[。]?", summary[first.end():employee.start()]) is None
        or re.fullmatch(r"[。]?", summary[second.end():]) is None
        or re.fullmatch(r"[\u4e00-\u9fff]{2,4}(?:先生|女士)?", holder["holder"]) is None
        or not 5 <= float(holder["ratio"]) <= 100 or not 0 < float(employee[2]) < 5):
        return None
    issuer = re.split(r"[（(](?:以下简称|简称)", holder["issuer"])[0]
    header = tuple(line for line in ("".join(line.split()) for line in document.text.splitlines()) if line)[:24]
    if holder["issuer"] != "本公司" and issuer not in header:
        return None
    try:
        start, end = date(*(int(first[i]) for i in (1, 2, 3))), date(*(int(first[i]) for i in (4, 5, 6)))
        other_start, other_end = date(*(int(second[i]) for i in (1, 2, 3))), date(*(int(second[i]) for i in (4, 5, 6)))
        quantity = int(first[8].replace(",", "").replace("，", ""))
        employee_quantity = int(second[8].replace(",", "").replace("，", ""))
        held = int(holder["quantity"].replace(",", "").replace("，", ""))
        employee_held = int(employee[1].replace(",", "").replace("，", ""))
    except ValueError:
        return None
    if (start > end or other_start > other_end or not 0 < quantity <= held or not 0 < employee_quantity <= employee_held
        or not 0 < float(first[9]) <= float(holder["ratio"]) or not 0 < float(second[9]) <= float(employee[2])):
        return None
    return CompanyQualifiedReduction(start, end, float(first[9]), summary[:first.end()])


def _group_period(document: CompanyDocument, body: str, subject: CompanySubject) -> CompanyQualifiedReduction | None:
    if subject is not CompanySubject.CONTROLLING_SHAREHOLDER:
        return None
    lines = "\n".join("".join(line.split()) for line in document.text.splitlines() if line.strip())
    declarations = tuple(re.finditer(_GROUP_DECLARATION, lines))
    summaries = tuple(re.finditer(_SINGLE_SUMMARY, body))
    if len(declarations) != 1 or len(summaries) != 1:
        return None
    names = tuple("".join(declarations[0][i].split()) for i in (1, 2))
    aliases: list[str] = []
    for name in names:
        matches = tuple(re.finditer(re.escape(name) + _SINGLE_ALIAS, summaries[0][1]))
        if len(matches) != 1:
            return None
        aliases.append(matches[0][1])
    plans = tuple(re.finditer(_GROUP_PERIOD, summaries[0][1]))
    clean = re.sub(r"《[^》]{0,160}》", "", body)
    if (len(plans) != 1 or len(set(aliases)) != 2 or {plans[0][1], plans[0][2]} != set(aliases)
        or set(aliases) & {"公司", "本公司", "股东", "大股东", "实际控制人"}
        or re.search(r"董事(?!会)|高级管理人员|其他股东|子公司|参股公司|分别|各自", clean)):
        return None
    allowed = {*names, *aliases}
    if any(re.sub(_SINGLE_ALIAS, "", match[1]) not in allowed for match in re.finditer(_SINGLE_ACTOR, body)):
        return None
    intervals = tuple(re.finditer(_INTERVAL, body))
    try:
        dates = {(date(*(int(match[i]) for i in (1, 2, 3))), date(*(int(match[i]) for i in (4, 5, 6)))) for match in intervals}
    except ValueError:
        return None
    if len(dates) != 1:
        return None
    start, end = next(iter(dates))
    if start > end:
        return None
    return CompanyQualifiedReduction(start, end, None, plans[0][0])


__all__ = [
    "COMPANY_QUALIFIED_REDUCTION_DIGEST",
    "COMPANY_REDUCTION_NONPARTICIPANT_DECLARATION",
    "CompanyQualifiedReduction",
    "ExtractCompanyQualifiedReduction",
    "IsCompanyJudicialReductionNotice",
]
