"""Official root discovery, bounded literal parsing and actual 2023 attachment labels."""

from __future__ import annotations

import json
from datetime import date

import pytest

from trader.download.infra.official_industry_release import (
    ParseOfficialIndustryBundleUrl,
    ParseOfficialIndustryRelease,
    ParseOfficialIndustryReleaseLinks,
    ValidateOfficialIndustryBundleUrl,
)

_BUNDLE = "https://www.capco.org.cn/assets/index-iLyoJn-6.js"
_PATH = "zsxfb/hyfl/hyfljg"
_FIRST_FILE = "46318a4cc28644fc9504c761540dc241.js"
_SECOND_FILE = "0ca8986fa29344898e01e24b27948051.js"


def _record(title: str, disclosed_on: str, filename: str) -> str:
    return (
        "{title:" + json.dumps(title, ensure_ascii=False)
        + ',articleUrl:"' + _PATH + "/" + filename + '",date:"' + disclosed_on
        + '",path:"' + _PATH + '",url:null,href:null,articleContext:null,fileName:"'
        + filename + '",articleSource:"中国上市公司协会"}'
    )


def _bundle(*records: str) -> str:
    return (
        'const menu=[{id:127,parentId:147,menuName:"行业分类结果",url:null,path:"'
        + _PATH + '",articleList:[' + ",".join(records) + "],children:[]}];"
    )


_FIRST = _record("2023年上半年上市公司行业分类结果", "2024-02-08", _FIRST_FILE)
_SECOND = _record("2023年下半年上市公司行业分类结果", "2024-04-03", _SECOND_FILE)


@pytest.mark.parametrize("source", (
    "./assets/index-iLyoJn-6.js",
    "/assets/index-iLyoJn-6.js",
    "assets/index-iLyoJn-6.js",
    _BUNDLE,
))
def test_root_html_discovers_only_its_unique_main_module(source):
    html = (
        '<!-- <script type="module" src="/assets/index-decoy.js"></script> -->'
        '<script src="/analytics.js"></script><script type="module">ignored()</script>'
        f'<script type="module" crossorigin src="{source}"></script>'
    )
    assert ParseOfficialIndustryBundleUrl(html) == _BUNDLE


@pytest.mark.parametrize("html", (
    "<html><script src='/assets/index-x.js'></script></html>",
    '<script type="module" src="/assets/index-x.js"></script>'
    '<script type="module" src="/assets/index-y.js"></script>',
    '<script type="module" src="https://evil.example/assets/index-x.js"></script>',
    '<script type="module" src="//www.capco.org.cn/assets/index-x.js"></script>',
    '<script type="module" src="/assets/index-x.js?download=1"></script>',
    '<script type="module" src="/assets/index-x.js#"></script>',
    '<script type="module" src="/assets/index-x.js" src="/assets/index-y.js"></script>',
    '<script type="module" src="/assets/index-x.js"></script>' + " " * (2 * 1024**2),
))
def test_missing_ambiguous_unsafe_and_oversized_homepages_are_rejected(html):
    with pytest.raises(ValueError):
        ParseOfficialIndustryBundleUrl(html)


@pytest.mark.parametrize("url", (
    "http://www.capco.org.cn/assets/index-x.js",
    "https://www.capco.org.cn.evil.example/assets/index-x.js",
    "https://www.capco.org.cn/assets/other-x.js",
    "https://www.capco.org.cn/assets/../assets/index-x.js",
    _BUNDLE + "?",
    _BUNDLE + "#",
    _BUNDLE.replace("index-", "index-\n"),
))
def test_bundle_allowlist_rejects_noncanonical_or_unrelated_scripts(url):
    with pytest.raises(ValueError, match="official_industry_bundle_url_invalid"):
        ValidateOfficialIndustryBundleUrl(url)


def test_catalog_keeps_2023_half_years_separate_from_old_quarters_and_other_sections():
    old = _record("2021年第3季度上市公司行业分类结果", "2021-11-11", "d3117b49492844ecbe2c62215f43ade7.js")
    unrelated = _bundle(_SECOND).replace('menuName:"行业分类结果"', 'menuName:"数据发布"').replace(
        'path:"zsxfb/hyfl/hyfljg",articleList:', 'path:"zsxfb/sjfb",articleList:',
    )
    links = ParseOfficialIndustryReleaseLinks(_BUNDLE, unrelated + _bundle(_SECOND, _FIRST, old))
    assert [(item.period_end, item.disclosed_on) for item in links] == [
        (date(2023, 6, 30), date(2024, 2, 8)), (date(2023, 12, 31), date(2024, 4, 3)),
    ]
    assert links[0].title == "2023年上半年上市公司行业分类结果"
    assert links[0].announcement_url == "https://www.capco.org.cn/dataFile/" + _PATH + "/" + _FIRST_FILE


@pytest.mark.parametrize("content", (
    _bundle(_FIRST) + _bundle(_SECOND),
    _bundle(_FIRST, _FIRST),
    _bundle(_FIRST, _record("2023年上半年上市公司行业分类结果", "2024-02-09", _SECOND_FILE)),
    _bundle(_FIRST).replace('path:"zsxfb/hyfl/hyfljg",articleList:', 'path:"other",articleList:'),
    _bundle(_FIRST).replace('articleUrl:"zsxfb/hyfl/hyfljg/', 'articleUrl:"other/'),
    _bundle(_FIRST).replace('fileName:"46318', 'fileName:"56318'),
    _bundle(_FIRST).replace('date:"2024-02-08"', 'date:"20240208"'),
    _bundle(_FIRST).replace('date:"2024-02-08"', 'date:"2024-02-30"'),
    _bundle(_FIRST).replace('date:"2024-02-08"', 'date:"2023-06-29"'),
    _bundle(_FIRST).replace('articleSource:"中国上市公司协会"', 'articleSource:"中国证监会"'),
    _bundle(_FIRST).replace('url:null', 'url:"https://evil.example"'),
    _bundle(_FIRST).replace('articleContext:null', 'articleContext:(function(){throw "never execute"})()'),
    _bundle(_FIRST).replace('href:null', 'href:null,href:null'),
    _bundle(_FIRST).replace('href:null', 'href:null,unexpected:null'),
    _bundle(_FIRST).replace("],children:[]}", ",],children:[]}"),
    _bundle(_FIRST)[:-15],
    _bundle(_record("2021年3季度上市公司行业分类结果", "2021-11-11", _FIRST_FILE)),
    _bundle(_FIRST) + " " * (8 * 1024**2),
))
def test_catalog_rejects_ambiguous_incomplete_or_executable_article_records(content):
    with pytest.raises(ValueError):
        ParseOfficialIndustryReleaseLinks(_BUNDLE, content)


def test_catalog_rejects_more_than_64_current_half_year_releases():
    records = []
    for number in range(65):
        year = 2023 + number // 2
        half = "上" if number % 2 == 0 else "下"
        records.append(_record(
            f"{year}年{half}半年上市公司行业分类结果",
            f"{year}-09-30" if half == "上" else f"{year + 1}-04-03",
            f"{number:032x}.js",
        ))
    with pytest.raises(ValueError, match="official_industry_catalog_invalid"):
        ParseOfficialIndustryReleaseLinks(_BUNDLE, _bundle(*records))


@pytest.mark.parametrize("half", ("上", "下"))
@pytest.mark.parametrize("suffix", ("", ".pdf"))
def test_actual_2023_labels_allow_optional_pdf_and_ignore_wrong_anchor_title(half, suffix):
    title = f"2023年{half}半年上市公司行业分类结果"
    content = "export const allData = " + json.dumps({
        "date": "2024-02-08" if half == "上" else "2024-04-03",
        "source": "中国上市公司协会",
        "title": title,
        "text": (
            '<a href="https://sp.capco.org.cn:82/file/202603/hangyefenlei/result.pdf\r\n" '
            'title="2023年上半年上市公司行业分类结果（按股票代码排序）.pdf">'
            f'{title}（按股票代码排序）{suffix}</a>'
        ),
    }, ensure_ascii=False)
    release = ParseOfficialIndustryRelease("https://www.capco.org.cn/dataFile/" + _PATH + "/" + _FIRST_FILE, content)
    expected_period = date(2023, 6, 30) if half == "上" else date(2023, 12, 31)
    assert release.period_end == expected_period
    assert release.attachment_url == "https://sp.capco.org.cn:82/file/202603/hangyefenlei/result.pdf"


@pytest.mark.parametrize("label", (
    "2023年下半年上市公司行业分类结果（按股票代码排序）.pdf",
    "2023年上半年上市公司行业分类结果（按行业排序）.pdf",
    "2023年上半年上市公司行业分类结果（按股票代码排序）.pdf.bak",
))
def test_optional_pdf_does_not_admit_a_different_period_or_sorting(label):
    content = "export const allData = " + json.dumps({
        "date": "2024-02-08",
        "source": "中国上市公司协会",
        "title": "2023年上半年上市公司行业分类结果",
        "text": f'<a href="https://sp.capco.org.cn:82/file/202603/result.pdf">{label}</a>',
    }, ensure_ascii=False)
    with pytest.raises(ValueError, match="official_industry_release_invalid"):
        ParseOfficialIndustryRelease("https://www.capco.org.cn/dataFile/" + _PATH + "/" + _FIRST_FILE, content)
