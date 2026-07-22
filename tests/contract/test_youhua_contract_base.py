from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_youhua_dual_memory_contract_is_authoritative() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "P1-P6 逻辑缓存载荷" in design
    assert "248 MiB" in design
    assert "迁移期进程峰值 RSS" in design
    assert "384 MiB（402,653,184 字节）" in design
    assert "`process_peak_rss_bytes <= 402653184`" in design
    assert "不得把多出的 136 MiB 分给缓存" in design
    assert "用 Python 分配或逻辑缓存估算代替 RSS 峰值" in design


def test_youhua_public_seams_have_single_versions_and_owners() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy = (PROJECT_ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    report = (PROJECT_ROOT / "docs/reports/youhua-a1-baseline.md").read_text(encoding="utf-8")
    g1 = (PROJECT_ROOT / "docs/reports/youhua-g1-contract-base.md").read_text(encoding="utf-8")
    a2 = (PROJECT_ROOT / "docs/reports/youhua-a2-public-skeleton.md").read_text(encoding="utf-8")

    for token in (
        "youhua_contract_base_v1",
        "p3_p4_feature_snapshot_market_change_set_v1",
        "p4_p5_high_value_review_manifest_v1",
        "p4p5_p6_projection_event_v1",
        "p6_overlay_event_v1",
    ):
        assert token in design
        assert token in report
        assert token in g1
        assert token in a2

    assert "deepseek_v4_review_facts_v1" in strategy
    assert "deepseek_v4_review_facts_v1" in report
    assert "deepseek_v4_review_facts_v1" in g1
    assert "deepseek_v4_review_facts_v1" in a2
    assert "Codex A 是 schema、版本、公共 port/event" in design
    assert "publisher、bootstrap 和集成测试 owner" in design
    assert "B/C/D 内部算法 | 未执行、未修改" in report


def test_youhua_g1_waits_for_bcd_phase_reports() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    report = (PROJECT_ROOT / "docs/reports/youhua-a1-baseline.md").read_text(encoding="utf-8")
    g1 = (PROJECT_ROOT / "docs/reports/youhua-g1-contract-base.md").read_text(encoding="utf-8")

    assert "B1/C1/D1 标准报告均 `ready_for_gate=yes`" in design
    assert "B1 P1-P3 盘点报告 | 已收到" in report
    assert "C1 DeepSeek 盘点报告 | 已收到" in report
    assert "D1 P6/Web 盘点报告 | 已收到" in report
    assert "CONTRACT_BASE | `45bd2fab992d36eb873b7c448fbd9739f0cad43c`" in report
    assert "ready_for_gate | yes" in report
    assert "CONTRACT_BASE | `45bd2fab992d36eb873b7c448fbd9739f0cad43c`" in g1
    assert "G1 已发布" in g1


def test_youhua_a2_memory_config_uses_dual_budget_keys() -> None:
    runtime_config = (PROJECT_ROOT / "config/v2/runtime.json").read_text(encoding="utf-8")
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    a2 = (PROJECT_ROOT / "docs/reports/youhua-a2-public-skeleton.md").read_text(encoding="utf-8")

    assert '"cache_logical_bytes": 260046848' in runtime_config
    assert '"process_peak_rss_bytes": 402653184' in runtime_config
    assert '"cache_total_bytes"' not in runtime_config
    assert "旧 `cache_total_bytes`" in design
    assert "旧 `cache_total_bytes`" in a2


def test_youhua_g2_is_not_published_until_all_phase2_reports_are_ready() -> None:
    g2 = (PROJECT_ROOT / "docs/reports/youhua-g2-gate-review.md").read_text(encoding="utf-8")
    c2 = (PROJECT_ROOT / "tests/fixtures/deepseek/youhua_c2/report_to_a.md").read_text(encoding="utf-8")

    assert "G2 未发布" in g2
    assert "tests/fixtures/market_data/youhua_b2/report_to_a.md" in g2
    assert "tests/fixtures/deepseek/youhua_c2/report_to_a.md" in g2
    assert "ready_for_gate` 为 `no`" in g2
    assert "C2 标准字段 | 已补齐，`ready_for_gate: yes`" in g2
    assert "ready_for_gate: yes" in c2
    assert "A 不进入 A3" in g2
