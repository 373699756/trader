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

    for token in (
        "youhua_contract_base_v1",
        "p3_p4_feature_snapshot_market_change_set_v1",
        "p4_p5_high_value_review_manifest_v1",
        "p4p5_p6_projection_event_v1",
        "p6_overlay_event_v1",
    ):
        assert token in design
        assert token in report

    assert "deepseek_v4_review_facts_v1" in strategy
    assert "deepseek_v4_review_facts_v1" in report
    assert "Codex A 是 schema、版本、公共 port/event" in design
    assert "publisher、bootstrap 和集成测试 owner" in design
    assert "B/C/D 内部算法 | 未执行、未修改" in report


def test_youhua_g1_waits_for_bcd_phase_reports() -> None:
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    report = (PROJECT_ROOT / "docs/reports/youhua-a1-baseline.md").read_text(encoding="utf-8")

    assert "B1/C1/D1 标准报告均 `ready_for_gate=yes`" in design
    assert "B1 P1-P3 盘点报告 | 已收到" in report
    assert "C1 DeepSeek 盘点报告 | 未收到" in report
    assert "D1 P6/Web 盘点报告 | 未收到" in report
    assert "CONTRACT_BASE | 未发布" in report
    assert "ready_for_gate | no" in report
