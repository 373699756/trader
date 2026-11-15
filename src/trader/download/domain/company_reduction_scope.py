"""Bind one declared qualifying holder's numbered plan without merging other actors."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

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
COMPANY_QUALIFIED_REDUCTION_DIGEST = hashlib.sha256(
    "\0".join((_SUMMARY, _NUMBER, _DECLARATION, _HOLDER, _HOLDER_CONTEXT, _HOLDING, _PLAN,
               _OTHER_ACTOR, _OTHER_QUALIFICATION, _OUTSIDE_ACTOR)).encode("utf-8"),
).digest()


@dataclass(frozen=True, slots=True)
class CompanyQualifiedReduction:
    start_date: date
    end_date: date
    planned_ratio_pct: float
    statement: str

    @property
    def plan(self) -> ReductionPlan:
        return ReductionPlan(self.start_date, self.end_date, self.planned_ratio_pct)


def ExtractCompanyQualifiedReduction(document: CompanyDocument, subject: CompanySubject) -> CompanyQualifiedReduction | None:
    title = "".join(document.disclosure.title.split())
    if (subject is not CompanySubject.MAJOR_SHAREHOLDER or len(document.disclosure.codes) != 1
        or "预披露" not in title or "减持" not in title
        or any(word in title for word in ("更正", "修订", "变更", "补充"))
        or ExtractCompanyAnnouncementNumbers(document.text).issuer_codes != document.disclosure.codes):
        return None
    body = "".join(document.text.split())
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


__all__ = [
    "COMPANY_QUALIFIED_REDUCTION_DIGEST",
    "CompanyQualifiedReduction",
    "ExtractCompanyQualifiedReduction",
]
