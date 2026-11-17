"""Exact issuer buyback table fields and current-purpose statement boundaries."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.domain.company_events import RepurchasePurpose
from trader.download.domain.company_evidence import CompanyDocument

_CONDITIONAL = r"假如|假设|假定|如果|倘若|若|一旦|只要|仅当|是否|能否"
_INTRODUCTION = r"(?:^|[，,])现将[^。；;]{0,30}(?:公告|披露)如下[：:]"
_CURRENT_PURPOSE = re.compile(r"本次回购(?:的)?股份用途(?:为|是)([^，,。；;]{1,80})")
_QUANTITY = r"[0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]+)?"
_TABLE = re.compile(
    r"累计已回购股数(?P<quantity>" + _QUANTITY + r")(?P<unit>万|亿)?股"
    r"累计已回购股数占总股本比例(?P<ratio>[0-9]+(?:\.[0-9]+)?)[%％]"
)
_FIRST_QUANTITY = re.compile(
    r"首次(?:实施)?回购(?:本公司|公司)?(?:A股)?(?:股份|股票)?(?:数量为|数量|共|合计)?"
    r"(?P<quantity>" + _QUANTITY + r")(?P<unit>万|亿)?股"
)
_TABLE_TITLE = r"首次(?:实施)?回购|回购(?:公司)?股份(?:的)?进展|回购进展"
_TABLE_PLAN_TITLE = r"拟|计划|方案(?!的?首次(?:实施)?回购)|预案|意向|预计|尚未|尚待|未实施"
_MONTHLY_ACTION_SCOPE = r"(?:^|[。；;：:])20[0-9]{2}年(?:[1-9]|1[0-2])月[,，]$"
_NO_ACTION = (
    r"(?:本公司|公司)(?:尚未|未曾|并未|没有|未)(?:实际)?(?:开展|实施|进行)?(?:首次)?(?:股份)?回购"
)
COMPANY_REPURCHASE_FIELDS_DIGEST = hashlib.sha256("\0".join((
    _CONDITIONAL, _INTRODUCTION, _CURRENT_PURPOSE.pattern, _TABLE.pattern, _FIRST_QUANTITY.pattern,
    _TABLE_TITLE, _TABLE_PLAN_TITLE, _MONTHLY_ACTION_SCOPE, _NO_ACTION, "更正前", "更正后", "除上述更正外", "issuer_header_exact", "first_quantity_consistent",
)).encode()).digest()


@dataclass(frozen=True, slots=True)
class CompanyRepurchaseTableMeasurement:
    ratio_pct: float
    statement: str


class CompanyRepurchaseTableConflict(ValueError):
    """A recognized table conflicts with issuer, action or revision evidence."""


def CompanyRepurchaseActionPrefix(prefix: str) -> str:
    """A declarative disclosure introduction is not a future-action verb."""
    sentence = re.split(r"[。；;！？!?]", prefix)[-1]
    introductions = tuple(re.finditer(_INTRODUCTION, sentence))
    if len(introductions) == 1 and re.search(_CONDITIONAL, sentence[:introductions[0].start()]) is None:
        return sentence[introductions[0].end():]
    return sentence


def _shares(match: re.Match[str]) -> Decimal:
    return Decimal(match["quantity"].replace(",", "").replace("，", "")) * {
        None: Decimal(1), "万": Decimal(10_000), "亿": Decimal(100_000_000),
    }[match["unit"]]


def _nonimplementation_conflicts(body: str) -> bool:
    # A dated month's lack of new purchases does not deny an explicitly
    # disclosed cumulative holding. Unqualified denials still conflict.
    return any(re.search(_MONTHLY_ACTION_SCOPE, body[:match.start()]) is None
               for match in re.finditer(_NO_ACTION, body))


def ExtractCompanyRepurchaseTable(document: CompanyDocument) -> CompanyRepurchaseTableMeasurement | None:
    """Read the exact cumulative table, never a planned amount or a shareholder holding."""
    title = "".join(document.disclosure.title.split())
    body = "".join(document.text.split())
    recognized = "累计已回购股数" in body and "累计已回购股数占总股本比例" in body
    if (len(document.disclosure.codes) != 1
        or ExtractCompanyAnnouncementNumbers(document.text).issuer_codes != document.disclosure.codes
        or re.search(_TABLE_PLAN_TITLE, title) is not None
        or "注销" in title or "用途" in title
        or any(word in body for word in ("子公司", "参股公司", "前次回购", "历史回购", "上一轮回购"))
        or _nonimplementation_conflicts(body)):
        if recognized:
            raise CompanyRepurchaseTableConflict("company_repurchase_table_context_conflict")
        return None
    if re.search(_TABLE_TITLE, title) is None:
        if recognized and ("更正" in title or "更正前" in body or "更正后" in body):
            raise CompanyRepurchaseTableConflict("company_repurchase_correction_scope_unverified")
        # This locator only owns the observed first/progress table. Existing
        # completion narratives retain their single sentence-parser owner.
        return None
    if "更正" in title:
        before = tuple(re.finditer(r"重要内容提示[（(]更正前[）)]", body))
        after = tuple(re.finditer(r"重要内容提示[（(]更正后[）)]", body))
        if len(before) != 1 or len(after) != 1 or before[0].end() >= after[0].start():
            raise CompanyRepurchaseTableConflict("company_repurchase_correction_scope_unverified")
        end = body.find("除上述更正外", after[0].end())
        if end < 0:
            raise CompanyRepurchaseTableConflict("company_repurchase_correction_scope_unverified")
        section = body[after[0].end():end]
    elif "更正前" in body or "更正后" in body:
        raise CompanyRepurchaseTableConflict("company_repurchase_correction_scope_unverified")
    else:
        section = body
    matches = tuple(_TABLE.finditer(section))
    if len(matches) > 1:
        raise CompanyRepurchaseTableConflict("company_repurchase_table_multiple_measurements")
    if not matches:
        return None
    match = matches[0]
    if re.search(_CONDITIONAL, re.split(r"[。；;！？!?]", section[:match.start()])[-1]):
        raise CompanyRepurchaseTableConflict("company_repurchase_table_conditional_context")
    quantity, ratio = _shares(match), float(match["ratio"])
    if (quantity <= 0 or quantity != quantity.to_integral_value()
        or not math.isfinite(ratio) or not 0 < ratio <= 100):
        raise CompanyRepurchaseTableConflict("company_repurchase_table_quantity_or_ratio_invalid")
    if "首次" in title:
        quantities = {_shares(part) for part in _FIRST_QUANTITY.finditer(section)}
        if quantities and quantities != {quantity}:
            raise CompanyRepurchaseTableConflict("company_repurchase_table_first_quantity_conflict")
    statement = match[0]
    if body.count(statement) != 1:
        if "更正" not in title:
            raise CompanyRepurchaseTableConflict("company_repurchase_table_locator_ambiguous")
        statement = after[0][0] + section[:match.end()]
        if body.count(statement) != 1:
            raise CompanyRepurchaseTableConflict("company_repurchase_table_locator_ambiguous")
    return CompanyRepurchaseTableMeasurement(ratio, statement)


def CompanyRepurchasePurposeStatement(
    body: str,
    purpose: RepurchasePurpose,
    option_pattern: re.Pattern[str],
    maintenance_pattern: str,
    parse_purpose: Callable[[str], RepurchasePurpose],
) -> str | None:
    """Locate a current declared purpose while preserving the existing option semantics."""
    if purpose is RepurchasePurpose.UNKNOWN:
        return None
    declared = tuple(_CURRENT_PURPOSE.finditer(body))
    if declared:
        if (len(declared) != 1 or purpose is not RepurchasePurpose.OTHER
            or declared[0][1] not in {"维护公司价值及股东权益", "用于维护公司价值及股东权益"}):
            return None
        selected = tuple(match for match in option_pattern.finditer(body) if match[1] not in {"□", "☐"})
        if selected and (len(selected) != 1 or "维护公司价值及股东权益" not in selected[0][0]):
            return None
        prefix = re.split(r"[。；;！？!?]", body[:declared[0].start()])[-1]
        if re.search(_CONDITIONAL + r"|(?:不是|并不|不再|不会|不得|不能|并非|尚未|未曾|拟|计划|预计|将|未|不|非)[^，,]{0,40}$", prefix):
            return None
        return declared[0][0]
    options = tuple(option_pattern.finditer(body))
    selected = tuple(match for match in options if match[1] not in {"□", "☐"})
    normalized = body
    for match in reversed(options):
        if match[1] in {"□", "☐"}:
            normalized = normalized[:match.start()] + normalized[match.end():]
    signals = (
        (RepurchasePurpose.CANCELLATION, r"减少注册资本|用于注销"),
        (RepurchasePurpose.EMPLOYEE_PLAN, r"员工持股"),
        (RepurchasePurpose.INCENTIVE, r"股权激励"),
        (RepurchasePurpose.OTHER, r"用于转换公司|维护公司价值"),
    )
    if tuple(value for value, expression in signals if re.search(expression, normalized)) != (purpose,):
        return None
    if re.search(
        r"(?:不是|并不|不再|不会|不得|不能|并非|无需|无须|尚未|未|不|非)(?:用于)?"
        r"(?:注销|减少注册资本|员工持股|股权激励|转换公司|为?维护公司价值)", normalized,
    ):
        return None
    if selected:
        statement = selected[0][0] if len(selected) == 1 and parse_purpose(selected[0][0]) is purpose else None
    else:
        expression = {
            RepurchasePurpose.CANCELLATION: r"用于注销(?:并减少注册资本)?|注销并减少注册资本",
            RepurchasePurpose.EMPLOYEE_PLAN: r"用于员工持股(?:计划)?",
            RepurchasePurpose.INCENTIVE: r"用于股权激励(?:计划)?",
            RepurchasePurpose.OTHER: r"用于转换公司(?:可转债|发行的可转换为股票的公司债券)|" + maintenance_pattern,
        }[purpose]
        statements = tuple(re.finditer(expression, body))
        statement = statements[0][0] if len(statements) == 1 else None
    if statement is not None and re.search(
        r"(?:不是|并不|不再|不会|不得|不能|并非|无需|无须|尚未|未曾|不将|未将)[^。；;]{0,40}$",
        body[:body.find(statement)],
    ):
        return None
    return statement


__all__ = [
    "COMPANY_REPURCHASE_FIELDS_DIGEST",
    "CompanyRepurchaseTableMeasurement",
    "CompanyRepurchaseTableConflict",
    "CompanyRepurchaseActionPrefix",
    "ExtractCompanyRepurchaseTable",
    "CompanyRepurchasePurposeStatement",
]
