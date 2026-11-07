from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from tests.helpers.shared.python_source import source_text
from urllib.parse import unquote

import pytest

from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs"
DESIGN = DOCS / "b_系统设计.md"
STRATEGY = DOCS / "c_评分设计.md"
DOWNLOAD = DOCS / "d_下载设计.md"
TRAINING = DOCS / "e_训练设计.md"
PRODUCT_CODE_ROOT = ROOT / "src" / "trader"
AUTHORITIES = ("c_评分设计.md", "b_系统设计.md", "g_实施进度.md", "d_下载设计.md")


def test_freeze_contract_has_one_boundary_for_each_strategy() -> None:
    design = source_text(DESIGN)
    strategy = source_text(STRATEGY)

    assert "只保留15:00作为荐股封口控制节点" in design
    assert "15:00冻结独立优先，不能被本轮等待阻塞" in design
    assert "有效 14:49:50 检查点" not in design
    assert "Tomorrow/D25 15:00 后" in strategy
    assert "Long：不冻结、不写推荐历史" in strategy


def test_freeze_types_and_budget_belong_to_engineering_while_risk_belongs_to_scoring() -> None:
    design = source_text(DESIGN)
    strategy = source_text(STRATEGY)

    for name in ("local ScoredFreeze", "hybrid ScoredFreeze", "UnifiedFreezeIndex", "LongProjection"):
        assert name in design
    assert "long 永远只发布 local" not in strategy
    assert "local/hybrid FreezeEpoch" not in strategy
    assert "mandatory_known_local_risk_penalty" in strategy
    assert "不得假定已确认风险消失" in strategy
    assert "tomorrow 独占正常目标 36、硬上限 66" not in design
    assert "tomorrow 独占目标 21、硬上限 38" in design


def test_authoritative_docs_keep_algorithm_and_product_ownership_separate() -> None:
    design = source_text(DESIGN)
    strategy = source_text(STRATEGY)

    assert "mandatory_known_local_risk_penalty" not in design
    assert "mandatory_known_local_risk_penalty" in strategy
    assert "GET /api/freezes/<strategy>/current" in design
    assert "GET /api/freezes/<strategy>/current" not in strategy
    assert "GET /api/v2/freezes/<strategy>/current" not in design
    assert "GET /api/status" in design
    assert "GET /api/status" not in strategy
    assert "DeepSeek 单次网络 timeout 20 秒" in _compact(design)
    assert "单次网络timeout当前为20秒" in strategy


def test_scoring_contract_preserves_strict_qfq_equivalence_boundary() -> None:
    strategy = source_text(STRATEGY)
    contract = "逐行公司行动元数据为空且两个调整标志均为零"

    assert contract in strategy
    assert "不得把一般未复权 `day` 标记为 qfq" in strategy


def test_documented_profiles_and_action_thresholds_match_current_contracts() -> None:
    strategy = source_text(STRATEGY)
    review = source_text(DOCS / "f_人工审查.md")
    selection = json.loads(source_text(ROOT / "config/selection.json"))["selection"]
    profiles = {profile.lower() for profile in re.findall(r"^\| (?:全新)?(V\d+) \|", strategy, re.MULTILINE)}
    assert profiles == set(SCORING_PROFILE_IDS)
    action_rows = re.findall(r"^\| (Tomorrow|D25) \| (\d+) \| (\d+) \| 回避 \|", strategy, re.MULTILINE)
    assert len(action_rows) == 2
    for name, execute, observe in action_rows:
        threshold = selection["thresholds"][name.lower()]
        assert float(execute) == threshold
        assert float(observe) == threshold - selection["observation_margin"]
        assert f"{name}{execute}/{observe}" in review.replace(" ", "")


def _compact(content: str) -> str:
    return " ".join(content.split())


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
    content = source_text(DOCS / name)
    assert "唯一" in content
    assert content.startswith("# ")
    assert source_text(DOCS / "README.md").count(f"]({name})") == 1


def test_retired_blueprints_are_removed_without_forwarding_stubs() -> None:
    for name in (
        "项目重构大纲.md", "项目重构详细.md", "04_策略回溯.md", "1.md", "2.md", "todo.md",
        "company.md", "score.md",
    ):
        assert not (DOCS / name).exists()


def test_current_document_links_resolve() -> None:
    current_documents = tuple(DOCS / name for name in (
        "a_总体目标.md", "b_系统设计.md", "c_评分设计.md", "d_下载设计.md",
        "e_训练设计.md", "f_人工审核.md", "g_施工进度.md", "h_需求文档.md",
    ))
    assert all(path.is_file() for path in current_documents)
    paths = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "scripts/README.md",
        DOCS / "README.md",
        *current_documents,
        *(ROOT / ".agents/skills/python-engineering-guide").rglob("*.md"),
    ]
    failures = []
    for path in paths:
        content = source_text(path)
        # Old documents and their pre-rename links remain read-only evidence.
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", content):
            if target.startswith(("http:", "https:", "#", "mailto:")):
                continue
            relative, _, anchor = unquote(target).partition("#")
            destination = path.parent / relative
            if not destination.is_file():
                failures.append(f"{path.relative_to(ROOT)} -> {target}")
            elif anchor and destination.suffix == ".md":
                anchors = _document_anchors(source_text(destination))
                if anchor not in anchors:
                    failures.append(f"{path.relative_to(ROOT)} -> missing anchor {target}")
    assert failures == []


def test_normative_tables_and_round_deadline_have_one_definition() -> None:
    design = source_text(DESIGN)
    for table in (
        "表 R：资源所有权唯一规范",
        "表 T：交易日时间线唯一规范",
        "表 F：失败分类唯一规范",
        "表 A：API schema 唯一规范",
    ):
        assert design.count(table) == 1
    for rule in (
        "RecommendationRounds拥有整轮状态",
        "达到第120秒即失去提交资格",
        "任一档拒绝或到期均不改变任一档current",
        "15:00冻结独立优先，不能被本轮等待阻塞",
    ):
        assert design.count(rule) == 1


def test_scoring_and_download_contracts_do_not_reintroduce_retired_behavior() -> None:
    scoring = source_text(STRATEGY)
    history = source_text(DOWNLOAD)
    training = source_text(TRAINING)
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
    ):
        assert invariant in history
    for invariant in (
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
        assert invariant in training
    for retired in ("Today模型头", "三个生产头", "download --mode", "train-v3 --sessions"):
        assert retired not in history
        assert retired not in training


def test_test_entrypoints_distinguish_fast_owner_and_full_gates() -> None:
    design = source_text(DESIGN)
    for command in ("make test-fast", "make test-history", "make test-train", "make test-full", "PYTEST_WORKERS"):
        assert command in design
    assert "禁止共享可变SQLite连接或硬链接" in design


def test_strategy_document_follows_the_end_to_end_stock_selection_chain() -> None:
    content = source_text(STRATEGY)
    ordered_sections = (
        "## 1. 目标、范围与当前结论",
        "## 2. 端到端荐股链路",
        "## 3. 数据采集、点时归一化与数据就绪",
        "## 4. 两级硬过滤",
        "## 5. 板内总体、评分资格与全局排名",
        "## 6. 本地评分与模型过滤",
        "## 7. 本地风险与 DeepSeek 结构化复核",
        "## 8. 最终分、动作、稳定排名与 TopK",
        "## 9. 冻结、恢复与不可变历史",
        "## 10. 展示与可解释性",
        "## 11. 逐环节优化空间与优先级",
        "## 12. 收益验证与变更门禁",
        "## 13. 链路外边界",
    )

    positions = tuple(content.index(section) for section in ordered_sections)
    assert positions == tuple(sorted(positions))


def test_research_review_separates_measurements_from_return_claims() -> None:
    content = source_text(STRATEGY)
    review = content[content.index("## 11. 逐环节优化空间与优先级") : content.index("## 12. 收益验证与变更门禁")]

    for column in ("问题", "比较方法", "判定依据"):
        assert column in review
    for topic in (
        "旧候选分提前截断",
        "模型过滤",
        "风险扣分",
        "动作分数",
        "同轮到Web",
        "复核是否有增量价值",
        "历史人口和事实时点",
        "P50/P95",
    ):
        assert topic in review
    assert "所有收益优化均为研究建议" in review
    assert "不能宣称提高收益" in review


def test_strategy_document_removes_delivery_history_and_implementation_noise() -> None:
    content = source_text(STRATEGY)
    executor_label = "Co" + "dex"

    for removed in (
        "engine_review28_2026_07",
        "#### 15.1.9",
        "#### 15.1.10",
        "#### 15.1.11",
        "#### 15.1.12",
        "#### 15.1.13",
        "#### 15.1.14",
        "#### 15.1.16",
        "#### 15.1.17",
        "#### 15.1.18",
        "#### 15.1.19",
        "#### 15.1.20",
        *(f"{executor_label} {lane}" for lane in "ABCD"),
        "CHOKEPOINT_INDUSTRY_LEADERS",
        "docs/reports/a-share-long-industry-research",
    ):
        assert removed not in content


def test_product_code_uses_business_responsibilities_instead_of_agent_lane_names() -> None:
    executor_label = ("co" + "dex").casefold()
    offending = tuple(
        path.relative_to(ROOT).as_posix()
        for path in PRODUCT_CODE_ROOT.rglob("*.py")
        if executor_label in path.as_posix().casefold() or executor_label in source_text(path).casefold()
    )

    assert offending == ()


def test_fixed_scoring_and_freeze_invariants_remain_explicit() -> None:
    content = source_text(STRATEGY)

    for invariant in (
        "board_data_reliability >= 0.85",
        "local_score = clamp(base_score - local_risk_penalty, 0, 100)",
        "final_score = ROUND_HALF_UP(clamp(base_score - local_risk_penalty, 0, 100), 2)",
        "deepseek_score=null",
        "deepseek_risk_penalty=0",
        "ROUND_HALF_UP",
        "Tomorrow/D25 15:00",
        "单一板块最多 `ceil(6 * 60%) = 4`",
        "没有行业名额限制",
        "没有候选分、50分线、每板120只或360只业务上限",
        "复核veto、动作变化或封口不从第七名以后递补",
    ):
        assert invariant in content


def test_d25_means_one_future_t2_to_t5_rising_stock_signal() -> None:
    content = source_text(STRATEGY)

    for required in (
        "未来第 2 至第 5 个交易日区间内具备上涨能力的股票",
        "T+2、T+3、T+4、T+5",
        "一个完整且唯一的 2–5 日生产策略头",
        "V2/V3 启动时",
        "所选档位双头中自己的单一 2–5 日模型头",
    ):
        assert required in content
