"""Parse CAPCO's structured release without executing website JavaScript."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit


_ROOT_URL = "https://www.capco.org.cn"
_RELEASE_PATH = "zsxfb/hyfl/hyfljg"
_MAX_HTML_BYTES = 2 * 1024 * 1024
_MAX_BUNDLE_BYTES = 8 * 1024 * 1024
_MAX_CATALOG_BYTES = 512 * 1024
_MAX_CATALOG_RECORDS = 128
_MAX_RELEASE_LINKS = 64
_CATALOG_FIELDS = frozenset({
    "title", "articleUrl", "date", "path", "url", "href", "articleContext", "fileName", "articleSource",
})
_CATALOG_HEADER = re.compile(
    r'\{id:[0-9]+,parentId:[0-9]+,menuName:"行业分类结果",url:null,'
    r'path:"zsxfb/hyfl/hyfljg",articleList:\['
)
_BUNDLE_PATH = re.compile(r"/assets/index-[A-Za-z0-9_-]{1,128}\.js")
_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*")


@dataclass(frozen=True)
class OfficialIndustryReleaseLink:
    announcement_url: str
    disclosed_on: date
    title: str
    period_end: date


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


class _HomepageHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bundles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        if sum(name == "type" for name, _value in attrs) > 1 or sum(name == "src" for name, _value in attrs) > 1:
            raise ValueError("official_industry_homepage_invalid")
        attributes = dict(attrs)
        if attributes.get("type") != "module":
            return
        source = attributes.get("src")
        if source is None:
            return
        source = source.strip()
        path = urlsplit(source).path
        if not path.startswith(("/assets/index-", "./assets/index-", "assets/index-")):
            return
        if source.startswith("./"):
            source = _ROOT_URL + source[1:]
        elif source.startswith("/assets/"):
            source = _ROOT_URL + source
        elif source.startswith("assets/"):
            source = _ROOT_URL + "/" + source
        ValidateOfficialIndustryBundleUrl(source)
        self.bundles.append(source)


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


def _require_text(text: str, maximum_bytes: int, reason: str) -> None:
    if not isinstance(text, str) or len(text) > maximum_bytes or len(text.encode("utf-8")) > maximum_bytes:
        raise ValueError(reason)


def ValidateOfficialIndustryBundleUrl(url: str) -> None:
    """Accept only the public site's current main bundle, never an arbitrary script."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.netloc != "www.capco.org.cn"
        or _BUNDLE_PATH.fullmatch(parsed.path) is None or parsed.query or parsed.fragment
        or url != _ROOT_URL + parsed.path
    ):
        raise ValueError("official_industry_bundle_url_invalid")


def ParseOfficialIndustryBundleUrl(html: str) -> str:
    """Read the unique main module URL from bounded official root HTML."""
    _require_text(html, _MAX_HTML_BYTES, "official_industry_homepage_invalid")
    parser = _HomepageHtml()
    parser.feed(html)
    parser.close()
    if len(parser.bundles) != 1:
        raise ValueError("official_industry_homepage_invalid")
    return parser.bundles[0]


def _catalog_record(text: str, position: int) -> tuple[dict[str, str | None], int]:
    """Parse only the observed flat, quoted-string/null article literal grammar."""
    values: dict[str, str | None] = {}
    decoder = json.JSONDecoder()
    if not text.startswith("{", position):
        raise ValueError("official_industry_catalog_invalid")
    position += 1
    while True:
        field = _FIELD_NAME.match(text, position)
        if field is None or field[0] in values or not text.startswith(":", field.end()):
            raise ValueError("official_industry_catalog_invalid")
        position = field.end() + 1
        value: str | None
        if text.startswith('"', position):
            decoded: object
            decoded, position = decoder.raw_decode(text, position)
            if not isinstance(decoded, str):
                raise ValueError("official_industry_catalog_invalid")
            value = decoded
        elif text.startswith("null", position):
            value = None
            position += 4
        else:
            raise ValueError("official_industry_catalog_invalid")
        values[field[0]] = value
        if text.startswith("}", position):
            if values.keys() != _CATALOG_FIELDS:
                raise ValueError("official_industry_catalog_invalid")
            return values, position + 1
        if not text.startswith(",", position):
            raise ValueError("official_industry_catalog_invalid")
        position += 1


def _release_link(record: dict[str, str | None]) -> OfficialIndustryReleaseLink | None:
    title = record["title"]
    if title is None:
        raise ValueError("official_industry_catalog_invalid")
    matched = re.fullmatch(r"(20[0-9]{2})年([上下])半年上市公司行业分类结果", title)
    if matched is None or int(matched[1]) < 2023:
        return None
    year = int(matched[1])
    period_end = date(year, 6, 30) if matched[2] == "上" else date(year, 12, 31)
    filename, disclosed = record["fileName"], record["date"]
    if (
        record["path"] != _RELEASE_PATH or record["articleSource"] != "中国上市公司协会"
        or filename is None or re.fullmatch(r"[0-9a-f]{32}\.js", filename) is None
        or record["articleUrl"] != _RELEASE_PATH + "/" + filename
        or any(record[name] is not None for name in ("url", "href", "articleContext"))
        or disclosed is None or re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", disclosed) is None
    ):
        raise ValueError("official_industry_catalog_invalid")
    disclosed_on = date.fromisoformat(disclosed)
    if disclosed_on < period_end:
        raise ValueError("official_industry_catalog_invalid")
    announcement_url = _ROOT_URL + "/dataFile/" + _RELEASE_PATH + "/" + filename
    ValidateOfficialIndustryUrl(announcement_url)
    return OfficialIndustryReleaseLink(announcement_url, disclosed_on, title, period_end)


def ParseOfficialIndustryReleaseLinks(bundle_url: str, text: str) -> tuple[OfficialIndustryReleaseLink, ...]:
    """Read the sole industry-result list as data; return 2023+ half-years oldest first."""
    ValidateOfficialIndustryBundleUrl(bundle_url)
    _require_text(text, _MAX_BUNDLE_BYTES, "official_industry_catalog_invalid")
    headers = tuple(_CATALOG_HEADER.finditer(text))
    if len(headers) != 1:
        raise ValueError("official_industry_catalog_invalid")
    start = position = headers[0].end()
    links: list[OfficialIndustryReleaseLink] = []
    records = 0
    while True:
        record, position = _catalog_record(text, position)
        records += 1
        if records > _MAX_CATALOG_RECORDS or len(text[start:position].encode("utf-8")) > _MAX_CATALOG_BYTES:
            raise ValueError("official_industry_catalog_invalid")
        if record["path"] != _RELEASE_PATH:
            raise ValueError("official_industry_catalog_invalid")
        link = _release_link(record)
        if link is not None:
            links.append(link)
            if len(links) > _MAX_RELEASE_LINKS:
                raise ValueError("official_industry_catalog_invalid")
        if text.startswith("],children:[]}", position):
            break
        if not text.startswith(",", position):
            raise ValueError("official_industry_catalog_invalid")
        position += 1
    if (
        not links or len({link.announcement_url for link in links}) != len(links)
        or len({link.period_end for link in links}) != len(links)
    ):
        raise ValueError("official_industry_catalog_invalid")
    return tuple(sorted(links, key=lambda link: link.period_end))


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
    _require_text(text, _MAX_HTML_BYTES, "official_industry_release_invalid")
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
        if re.sub(r"\s+", "", label) in {
            payload["title"] + "（按股票代码排序）", payload["title"] + "（按股票代码排序）.pdf",
        }
    }
    if len(attachments) != 1 or disclosed_on < period_end:
        raise ValueError("official_industry_release_invalid")
    attachment_url = attachments.pop()
    ValidateOfficialIndustryUrl(attachment_url, attachment=True)
    return OfficialIndustryRelease(period_end, disclosed_on, url, attachment_url)


__all__ = [
    "OfficialIndustryRelease",
    "OfficialIndustryReleaseLink",
    "ParseOfficialIndustryBundleUrl",
    "ParseOfficialIndustryRelease",
    "ParseOfficialIndustryReleaseLinks",
    "ValidateOfficialIndustryBundleUrl",
    "ValidateOfficialIndustryUrl",
]
