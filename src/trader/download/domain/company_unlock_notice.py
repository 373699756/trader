"""Validate individual unlock measurements from exact issuer notices."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import re

from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.domain.company_evidence import CompanyDocument, CompanyRatio

_NOTICE = re.compile(r"限售股(?:份)?上市流通(?:的)?(?:提示性)?公告(?:[（(]临?20[0-9]{2}-[0-9]{1,4}[）)])?$")
_REVISIONS = ("更正", "修订", "延期", "取消", "终止", "暂缓", "变更")
_DATE = re.compile(
    r"本次(?:股票|限售股(?:份)?|解除限售的股份)?上市流通日期(?:为|[:：])"
    r"(?P<year>20[0-9]{2})年(?P<month>[0-9]{1,2})月(?P<day>[0-9]{1,2})日"
)
_SHARES = r"[1-9][0-9]{0,9}|[1-9][0-9]{0,2}(?:[,，][0-9]{3}){1,3}"
_TOTAL_SHARES = re.compile(
    r"本次(?:股票上市流通总数|上市流通的限售股(?:份)?总数|解除限售的?股份数量)"
    rf"(?:为|[:：])(?P<shares>{_SHARES})股"
)
_RATIO = re.compile(
    r"(?:本次上市流通的限售股(?:份)?数量|解除限售的?股份数量|持有公司限售股(?:份)?数量)"
    rf"(?:为|[:：])(?P<shares>{_SHARES})股[，,]"
    r"(?:股东数量为[0-9]+人[，,])?占(?:截至本公告披露之日)?公司"
    r"(?:(?:目前|最新|发行后)?总股本|股本总数)(?:的)?(?:比例)?(?:为)?"
    r"(?P<percent>[0-9]{1,3}(?:\.[0-9]{1,8})?)[%％]"
)


def IsCompanyUnlockListingNotice(title: str) -> bool:
    """Select issuer listing announcements without expanding to opinion PDFs."""
    title = "".join(title.split())
    return _NOTICE.search(title) is not None and not any(marker in title for marker in _REVISIONS)


def IsCompanyUnlockListingRevision(title: str) -> bool:
    """Recognize later material that prevents automatic use of an earlier plan."""
    title = "".join(title.split())
    return "上市流通" in title and "限售" in title and any(marker in title for marker in _REVISIONS)


def ConfirmCompanyUnlockListing(
    document: CompanyDocument,
    observations: tuple[CompanyRatio, ...],
    observed_at: datetime,
) -> CompanyRatio | None:
    """Confirm one known event and the issuer's stated total-capital ratio."""
    item = document.disclosure
    if (
        not document.checked or document.terminal_failure or item.kind != "announcement" or item.source != "cninfo"
        or observed_at.utcoffset() is None or item.published_at > observed_at or len(item.codes) != 1
        or not IsCompanyUnlockListingNotice(item.title)
    ):
        return None
    identity = ExtractCompanyAnnouncementNumbers(document.text)
    if identity.issuer_codes != item.codes or identity.own is None:
        return None
    code = item.codes[0]
    text = "".join(document.text.split())
    if "本公司" not in text[:1200] or "保证" not in text[:1200]:
        return None
    days: set[date] = set()
    for match in _DATE.finditer(text):
        try:
            days.add(date(int(match["year"]), int(match["month"]), int(match["day"])))
        except ValueError:
            return None
    if len(days) != 1:
        return None
    day = next(iter(days))
    if day < item.published_at.date():
        return None
    observed = tuple(row for row in observations if row.code == code and row.day == day)
    if len(observed) != 1 or observed[0].ratio_pct <= 0:
        return None
    totals = {_shares(match["shares"]) for match in _TOTAL_SHARES.finditer(text)}
    if len(totals) != 1:
        return None
    total = next(iter(totals))
    values = {Decimal(match["percent"]) for match in _RATIO.finditer(text) if _shares(match["shares"]) == total}
    if len(values) != 1:
        return None
    ratio = next(iter(values))
    if not 0 < ratio <= 100:
        return None
    observed_ratio = Decimal(str(observed[0].ratio_pct))
    if observed_ratio.quantize(ratio, rounding=ROUND_HALF_UP) != ratio:
        return None
    return CompanyRatio(code, day, float(ratio))


def _shares(text: str) -> int:
    return int(text.replace(",", "").replace("，", ""))


__all__ = ["ConfirmCompanyUnlockListing", "IsCompanyUnlockListingNotice", "IsCompanyUnlockListingRevision"]
