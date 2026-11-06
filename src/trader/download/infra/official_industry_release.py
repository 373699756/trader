"""Parse CAPCO's structured release without executing website JavaScript."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit


@dataclass(frozen=True)
class OfficialIndustryRelease:
    period_end: date
    disclosed_on: date
    announcement_url: str
    attachment_url: str
    native_classification: str = "中国上市公司协会上市公司行业分类"

    @property
    def conservative_available_on(self) -> date:
        # A release date does not prove availability at the start of that day.
        return self.disclosed_on + timedelta(days=1)


class _AttachmentHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            if self._href is not None:
                raise ValueError("official_industry_release_invalid")
            self._href = next((value for key, value in attrs if key == "href"), None)
            self._label = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href.strip(), "".join(self._label).strip()))
            self._href = None

    def handle_data(self, value: str) -> None:
        if self._href is not None:
            self._label.append(value)


def ValidateOfficialIndustryUrl(url: str, *, attachment: bool = False) -> None:
    parsed = urlsplit(url)
    host = "sp.capco.org.cn:82" if attachment else "www.capco.org.cn"
    prefix = "/file/" if attachment else "/dataFile/zsxfb/hyfl/hyfljg/"
    suffix = ".pdf" if attachment else ".js"
    if (
        parsed.scheme != "https" or parsed.netloc != host
        or not parsed.path.startswith(prefix) or not parsed.path.lower().endswith(suffix)
        or parsed.query or parsed.fragment or "/../" in parsed.path
    ):
        raise ValueError("official_industry_url_invalid")


def ParseOfficialIndustryRelease(url: str, text: str) -> OfficialIndustryRelease:
    ValidateOfficialIndustryUrl(url)
    prefix = "export const allData = "
    if not text.startswith(prefix):
        raise ValueError("official_industry_release_invalid")
    payload: object = json.loads(text[len(prefix):].strip())
    if (
        not isinstance(payload, dict) or set(payload) != {"date", "text", "source", "title"}
        or not all(isinstance(value, str) for value in payload.values())
        or payload["source"] != "中国上市公司协会"
    ):
        raise ValueError("official_industry_release_invalid")
    title = re.fullmatch(r"(20[0-9]{2})年([上下])半年上市公司行业分类结果", payload["title"])
    if title is None:
        raise ValueError("official_industry_release_invalid")
    year = int(title[1])
    period_end = date(year, 6, 30) if title[2] == "上" else date(year, 12, 31)
    disclosed_on = date.fromisoformat(payload["date"])
    parser = _AttachmentHtml()
    parser.feed(payload["text"])
    attachments = {
        href for href, label in parser.links
        if re.sub(r"\s+", "", label) == payload["title"] + "（按股票代码排序）.pdf"
    }
    if len(attachments) != 1 or disclosed_on < period_end:
        raise ValueError("official_industry_release_invalid")
    attachment_url = attachments.pop()
    ValidateOfficialIndustryUrl(attachment_url, attachment=True)
    return OfficialIndustryRelease(period_end, disclosed_on, url, attachment_url)


__all__ = [
    "OfficialIndustryRelease",
    "ParseOfficialIndustryRelease",
    "ValidateOfficialIndustryUrl",
]
