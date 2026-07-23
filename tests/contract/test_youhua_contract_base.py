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


def test_youhua_g2_is_published_after_all_phase2_reports_are_ready() -> None:
    g2 = (PROJECT_ROOT / "docs/reports/youhua-g2-gate-review.md").read_text(encoding="utf-8")
    c2 = (PROJECT_ROOT / "tests/fixtures/deepseek/youhua_c2/report_to_a.md").read_text(encoding="utf-8")
    b2 = (PROJECT_ROOT / "tests/fixtures/market_data/youhua_b2/report_to_a.md").read_text(encoding="utf-8")

    assert "G2 已发布" in g2
    assert "tests/fixtures/market_data/youhua_b2/report_to_a.md" in g2
    assert "tests/fixtures/deepseek/youhua_c2/report_to_a.md" in g2
    assert "B2 标准字段 | 已补齐，`ready_for_gate=yes`" in g2
    assert "C2 标准字段 | 已补齐，`ready_for_gate: yes`" in g2
    assert "ready_for_gate: `yes`" in b2
    assert "ready_for_gate: yes" in c2
    assert "A3 has not started" in g2


def test_youhua_a3_integration_handoff_waits_for_bcd_phase3_reports() -> None:
    plan = (PROJECT_ROOT / "docs/plan_youhua.md").read_text(encoding="utf-8")
    report = (PROJECT_ROOT / "docs/reports/youhua-a3-integration.md").read_text(encoding="utf-8")

    assert "A3.1-A3.7 已完成 A owner 集成接线" in report
    assert "G3 未发布" in report
    assert "B3/C3/D3" in report
    assert "ready_for_gate: `yes; A3 integration handoff is available; G3 is pending B3/C3/D3 ready reports`" in report
    assert "A3.1 合并 B" in plan
    assert "G3：阶段 3 完成条件" in plan


def test_youhua_g3_publishes_after_all_phase3_reports_are_ready() -> None:
    g3 = (PROJECT_ROOT / "docs/reports/youhua-g3-gate-review.md").read_text(encoding="utf-8")
    b3 = (PROJECT_ROOT / "tests/fixtures/market_data/youhua_b3/report_to_a.md").read_text(encoding="utf-8")
    c3 = (PROJECT_ROOT / "tests/fixtures/deepseek/youhua_c3/report_to_a.md").read_text(encoding="utf-8")
    d3 = (PROJECT_ROOT / "docs/reports/youhua-d1-p6-web.md").read_text(encoding="utf-8")

    assert "G3 已发布" in g3
    assert "tests/fixtures/market_data/youhua_b3/report_to_a.md" in g3
    assert "ready_for_gate: `yes`" in b3
    assert "C3 集成态请求/降级报告" in g3
    assert "ready_for_gate: yes" in c3
    assert "docs/reports/youhua-d1-p6-web.md" in g3
    assert "Codex D / D3.x" in d3
    assert "ready_for_gate\nyes" in d3
    assert "ready_for_gate: `yes; G3 is published and A4 has not started`" in g3
    assert "A4.x 必须等待下一次用户继续指令" in g3


def test_youhua_a4_acceptance_closes_failures_without_starting_g4_or_a5() -> None:
    report = (PROJECT_ROOT / "docs/reports/youhua-a4-acceptance.md").read_text(encoding="utf-8")
    design = (PROJECT_ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "A4.1-A4.6" in report
    assert "A4-F01" in report
    assert "A4-F04" in report
    assert "| A4-F01 | B | closed |" in report
    assert "| A4-F04 | D + A | closed |" in report
    assert "ready_for_gate: `yes; A4.1-A4.6 complete" in report
    assert "G4 is not published" in report
    assert "A5 has not started" in report
    assert "必须先由 P6 接纳，再更新 RuntimeState、session、检查点和 SSE" in design
    assert "通过 P6 前不得" in design


def test_youhua_g4_publishes_after_all_phase4_gates_are_ready() -> None:
    a4 = (PROJECT_ROOT / "docs/reports/youhua-a4-acceptance.md").read_text(encoding="utf-8")
    b4 = (PROJECT_ROOT / "tests/fixtures/market_data/youhua_b4/report_to_a.md").read_text(encoding="utf-8")
    c4 = (PROJECT_ROOT / "tests/fixtures/deepseek/youhua_c4/report_to_a.md").read_text(encoding="utf-8")
    d4 = (PROJECT_ROOT / "docs/reports/youhua-d1-p6-web.md").read_text(encoding="utf-8")
    g4 = (PROJECT_ROOT / "docs/reports/youhua-g4-gate-review.md").read_text(encoding="utf-8")

    assert "ready_for_gate: `yes; A4.1-A4.6 complete" in a4
    assert "ready_for_gate: `yes`" in b4
    assert "ready_for_gate: yes" in c4
    assert "Codex D / D4.x" in d4
    assert "ready_for_gate\nyes; D4-owned gates pass" in d4
    assert "G4 已发布" in g4
    assert "8e7ab24985ff73f7ec54cf62c9440f97b5d179c6" in g4
    assert "A4-F04" in g4
    assert "G4 is published and A5 has not started" in g4
