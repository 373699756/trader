"""Explicit issuer announcement numbers; they are distinct from supplier IDs."""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER = re.compile(r"公告编号[：:]?[（(]?(20[0-9]{2}[-－—][0-9]{1,4})(?![0-9])")
_DASHES = str.maketrans({"－": "-", "—": "-"})
_CODE = re.compile(r"(?:证券|股票)代码[：:]?([0-9]{6})(?![0-9])")


@dataclass(frozen=True, slots=True)
class CompanyAnnouncementNumbers:
    own: str | None
    references: tuple[str, ...]
    issuer_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.own is not None and re.fullmatch(r"20[0-9]{2}-[0-9]{1,4}", self.own) is None
            or len(self.references) > 32 or tuple(sorted(set(self.references))) != self.references
            or any(re.fullmatch(r"20[0-9]{2}-[0-9]{1,4}", number) is None for number in self.references)
            or self.own in self.references
            or tuple(sorted(set(self.issuer_codes))) != self.issuer_codes
            or any(re.fullmatch(r"[0-9]{6}", code) is None for code in self.issuer_codes)
        ):
            raise ValueError("company_announcement_numbers_invalid")


def ExtractCompanyAnnouncementNumbers(text: str) -> CompanyAnnouncementNumbers:
    """An own number must be in the document header, never in a later citation."""
    header: list[str] = []
    for line in text[:2048].splitlines()[:12]:
        compact = "".join(line.split())
        if any(marker in compact for marker in ("本公司", "特别提示", "重要提示", "一、", "公告如下")):
            break
        if compact.startswith(("公告编号", "证券代码", "证券简称", "股票代码", "股票简称")):
            header.append(compact)
    header_text = "\n".join(header)
    issuer_codes = tuple(sorted({match[1] for match in _CODE.finditer(header_text)}))
    own_numbers = {match[1].translate(_DASHES) for match in _NUMBER.finditer(header_text)}
    if len(own_numbers) > 1:
        return CompanyAnnouncementNumbers(None, (), issuer_codes)
    own = next(iter(own_numbers), None)
    mentions: set[str] = set()
    body = "".join(text.split())
    for match in _NUMBER.finditer(body):
        number = match[1].translate(_DASHES)
        if number in own_numbers:
            continue
        start = max(body.rfind(mark, 0, match.start()) for mark in ("。", "；", ";", "\n")) + 1
        ends = (body.find(mark, match.end()) for mark in ("。", "；", ";", "\n"))
        end = min((position for position in ends if position >= 0), default=len(body))
        if any(marker in body[start:end] for marker in ("无关", "不属于", "并非", "不是", "新一轮", "另一次")):
            return CompanyAnnouncementNumbers(own, (), issuer_codes)
        mentions.add(number)
        if len(mentions) > 32:
            return CompanyAnnouncementNumbers(own, (), issuer_codes)
    return CompanyAnnouncementNumbers(own, tuple(sorted(mentions - own_numbers)), issuer_codes)


__all__ = ["CompanyAnnouncementNumbers", "ExtractCompanyAnnouncementNumbers"]
