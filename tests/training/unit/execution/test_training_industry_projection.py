"""Latest-industry coverage counts securities separately from discarded sample rows."""

from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from trader.training.entrypoints.profile_training import ProjectTrainingIndustryCoverage
from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TrainingSampleDiagnostics
from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress


def test_current_industry_projection_lists_missing_codes_and_actual_publication_times():
    current = CurrentIndustrySnapshotFixture(("600001",)).project(("600001", "600002"))
    projected = ProjectTrainingIndustryCoverage(current)
    assert projected["classified_codes"] == projected["missing_codes"] == 1
    assert projected["gaps"] == [{"code": "600002", "reason": "current_industry_not_published"}]
    assert projected["received_at"] == current.observed_at.isoformat()
    assert projected["refresh_after"] == current.refresh_after.isoformat()
    assert projected["industry_content_hash"] == current.content_hash
    assert projected["publication_hash"] == current.publication_hash
    assert projected["policy"] == "latest_snapshot"


def test_discarded_sample_rows_are_reported_as_stock_days_with_warmup(capsys):
    output = StderrTomorrowTrainingProgress(command_label="train·V3", monotonic=lambda: 0)
    output.publish(TomorrowTrainingProgress(
        "cross_section_conversion", "completed", 0, 0,
        sample_diagnostics=TrainingSampleDiagnostics(3_000_000, 0, 42, 0, 0),
    ))
    output.publish_result("blocked", "coverage_insufficient")
    text = "".join(capsys.readouterr().err.split())
    assert "候选样本诊断（股日）" in text and "缺行业3000000" in text
    assert "窗口不足（含预热）42" in text
