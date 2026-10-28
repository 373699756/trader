from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
AUTHORITIES = ("1_评分逻辑.md", "2_工程设计.md", "3_工程实施.md", "4_下载训练.md")


class _HtmlAnchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value and (name == "id" or (tag == "a" and name == "name")):
                self.anchors.add(value)


def _document_anchors(content: str) -> set[str]:
    headings = re.findall(r"^#{1,6} +(.+)$", content, re.MULTILINE)
    parser = _HtmlAnchors()
    parser.feed(content)
    parser.close()
    return parser.anchors | {
        re.sub(r"[^\w\- ]", "", heading.replace("`", "")).lower().replace(" ", "-") for heading in headings
    }


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("## 工程 `Schema`", {"工程-schema"}),
        ('<a class="section" id="company-schema"></a>', {"company-schema"}),
        ("<a name='company-raw'></a>\n<section id=details></section>", {"company-raw", "details"}),
        ('<!-- <a id="missing"></a> --><a href="#missing" data-id="missing">link</a>', set()),
    ],
)
def test_document_anchor_extraction(content: str, expected: set[str]) -> None:
    assert _document_anchors(content) == expected


@pytest.mark.parametrize("name", AUTHORITIES)
def test_each_document_has_one_explicit_responsibility(name: str) -> None:
    content = (DOCS / name).read_text(encoding="utf-8")
    assert "唯一" in content
    assert content.startswith("# ")
    assert (DOCS / "README.md").read_text(encoding="utf-8").count(f"]({name})") == 1


def test_retired_blueprints_are_removed_without_forwarding_stubs() -> None:
    for name in ("项目重构大纲.md", "项目重构详细.md", "04_策略回溯.md", "1.md", "2.md", "todo.md", "company.md"):
        assert not (DOCS / name).exists()


def test_current_document_links_resolve() -> None:
    paths = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "scripts/README.md",
        *sorted(DOCS.glob("*.md")),
        *(ROOT / ".agents/skills/trader-delivery").rglob("*.md"),
        *(ROOT / ".agents/skills/python-engineering-guide").rglob("*.md"),
    ]
    failures = []
    for path in paths:
        content = path.read_text(encoding="utf-8")
        if path.name == "3_工程实施.md":
            # Historical evidence is immutable context, not a current filesystem promise.
            content = content.split("## 历史回溯索引", 1)[0]
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", content):
            if target.startswith(("http:", "https:", "#", "mailto:")):
                continue
            relative, _, anchor = unquote(target).partition("#")
            destination = path.parent / relative
            if not destination.is_file():
                failures.append(f"{path.relative_to(ROOT)} -> {target}")
            elif anchor and destination.suffix == ".md":
                anchors = _document_anchors(destination.read_text(encoding="utf-8"))
                if anchor not in anchors:
                    failures.append(f"{path.relative_to(ROOT)} -> missing anchor {target}")
    assert failures == []


def test_normative_tables_and_latest_wins_have_one_definition() -> None:
    design = (DOCS / "2_工程设计.md").read_text(encoding="utf-8")
    for table in (
        "表 R：资源所有权唯一规范",
        "表 T：交易日时间线唯一规范",
        "表 F：失败分类唯一规范",
        "表 A：API schema 唯一规范",
    ):
        assert design.count(table) == 1
    for rule in (
        "尚未开始的旧 pending 可以被最新输入替换",
        "已经开始纯本地评分的旧任务必须完成并可先发布 local",
        "若已有更新 pending，旧任务跳过 DeepSeek",
        "新 local 随后通过 sequence/CAS 替换临时 current",
    ):
        assert design.count(rule) == 1


def test_scoring_and_download_contracts_do_not_reintroduce_retired_behavior() -> None:
    scoring = (DOCS / "1_评分逻辑.md").read_text(encoding="utf-8")
    history = (DOCS / "4_下载训练.md").read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", scoring)
    assert "d25_aggregate=(net_excess_T2+net_excess_T3+net_excess_T4+net_excess_T5)/4" in compact
    assert "final_score=ROUND_HALF_UP(clamp(base_score-local_risk_penalty,0,100),2)" in compact
    assert "deepseek_score*0.32" not in compact
    assert "ROUND_HALF_UP" in scoring and "168" in scoring
    assert "terminal_holdout_already_opened" in scoring
    for command in ("train", "sync market", "sync company"):
        assert f"./run.sh {command}" in history
    for invariant in (
        "不依赖history",
        "已有",
        "raw",
        "qfq",
        "logical_hash",
        "tencent_inferred_suspended",
        "推定停牌/缺行估算不得生成未来标签",
        "不向后借价格、不回填上市前、不修复单侧缺价或异常价格",
        "1000",
        "750",
        "LightGBM",
        "Ridge",
        "双头全部合格后",
        "拟合、校验或切换失败回滚上一完整组",
        "不自动训练",
        "production_authority",
    ):
        assert invariant in history
    for retired in ("Today模型头", "三个生产头", "download --mode", "train-v3 --sessions"):
        assert retired not in history


def test_test_entrypoints_distinguish_fast_owner_and_full_gates() -> None:
    design = (DOCS / "2_工程设计.md").read_text(encoding="utf-8")
    for command in ("make test-fast", "make test-history", "make test-train", "make test-full", "PYTEST_WORKERS"):
        assert command in design
    assert "禁止共享可变SQLite连接或硬链接" in design
