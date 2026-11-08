"""Production policy is testable with typed in-memory ports and no SQL or native fitter."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.profile_training import (
    HeadTrainingResult,
    InspectProfileReuse,
    PreparedTrainingHead,
    PreparedTrainingSamples,
    ProfileTrainingOperationError,
    PublishedTrainingHead,
    TrainProfile,
)
from trader.training.domain.profile_training import ProfileTrainingHead, ProfileTrainingSource
from trader.training.domain.sample_coverage import SampleCoverage
from trader.training.domain.tomorrow_training_input import FrozenDailyInputDescriptor, REQUIRED_DAILY_FIELDS
from trader.training.entrypoints.profile_training import ProjectProfileTrainingResult
from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture


def _source():
    calendar = tuple(date(2018, 1, 1) + timedelta(days=index) for index in range(2000))
    descriptor = FrozenDailyInputDescriptor(
        "a" * 64, "baostock_daily_core", calendar[-1], 2000, ("code", "trade_date"),
        REQUIRED_DAILY_FIELDS, "same_row", "sha256", True,
    )
    return ProfileTrainingSource(
        "v3", "ridge", calendar, calendar[-1], "a" * 64, "complete_manifest", 2, 2, 61, 61, 10,
        (ProfileTrainingHead(Strategy.TOMORROW, 1, "b" * 64), ProfileTrainingHead(Strategy.D25, 5, "c" * 64)),
        descriptor,
        industry_content_hash=CurrentIndustrySnapshotFixture(("600001", "600002")).content_hash,
    )


class _Training:
    def __init__(
        self, *, missing_coverage=False, rejected_head=False, error=None, cleanup_error=False, coverage_case=None,
        scope_error=None,
    ):
        self.source = _source()
        self.calls = []
        self.plan = None
        self.missing_coverage = missing_coverage
        self.rejected_head = rejected_head
        self.error = error
        self.cleanup_error = cleanup_error
        self.coverage_case = coverage_case
        self.scope_error = scope_error

    def read_source(self):
        return self.source

    def has_industry_window(self, readable_dates, anchors=None):
        assert readable_dates and (anchors is None or anchors <= readable_dates)
        return True

    def industry_sample_upper_bounds(self, readable_dates, anchors):
        assert anchors <= readable_dates
        return (("银行", 10),)

    def prepare_samples(self, plan, window):
        self.plan = plan
        assert set(plan.split.fitting_dates) <= window.readable_dates
        partitions = (
            ("training", plan.split.fitting_dates), ("early_stopping", plan.split.early_stopping_dates),
            ("calibration", plan.split.calibration_dates), ("validation", plan.split.validation_dates), ("holdout", ()),
        )
        coverage = tuple(
            SampleCoverage(target, name, 999, len(days), len(days) * 2, len(days) * 2, ())
            for target in ("target_t1", "target_d25_aggregate") for name, days in partitions
        )
        if self.missing_coverage:
            coverage = (replace(coverage[0], effective_dates=0), *coverage[1:])
        if self.coverage_case == "empty":
            coverage = ()
        elif self.coverage_case == "missing_head":
            coverage = tuple(item for item in coverage if item.target == "target_t1")
        elif self.coverage_case == "missing_partition":
            coverage = coverage[1:]
        elif self.coverage_case == "duplicate":
            coverage = (*coverage, coverage[0])
        elif self.coverage_case == "duplicate_missing":
            coverage = (*coverage[:-1], coverage[0])
        elif self.coverage_case == "unknown_head":
            coverage = (replace(coverage[0], target="target_other"), *coverage[1:])
        elif self.coverage_case == "unknown_partition":
            coverage = (replace(coverage[0], partition="unknown"), *coverage[1:])
        elif self.coverage_case == "forged_dates":
            coverage = (replace(coverage[0], expected_dates=1, effective_dates=1), *coverage[1:])
        self.calls.append("samples")
        return PreparedTrainingSamples(coverage, 123)

    @contextmanager
    def fitting(self, plan):
        assert plan is self.plan
        self.calls.append("fitting_open")
        try:
            yield self
        finally:
            self.calls.append("fitting_closed")
            if self.scope_error is not None:
                raise self.scope_error("private scope detail")

    def fit_diagnostics(self):
        self.calls.append("diagnostics")
        if self.error is not None:
            raise self.error("controlled fitting failure")

    def prepare_head(self, head):
        self.calls.append(head.strategy.value)
        rejected = self.rejected_head and head.strategy is Strategy.D25
        result = HeadTrainingResult(
            head.strategy, "rejected" if rejected else "engineering_ready", "complete_manifest",
            self.plan.run_id, self.source.training_input_hash, 2, 2, "d" * 64, "e" * 64, 1, 122, 40,
            ("structure_invalid",) if rejected else (), label_cutoff=head.label_cutoff, final_fit=head.final_fit,
        )
        return PreparedTrainingHead(result, not rejected)

    def publish_pair(self):
        self.calls.append("published")

    def clear_samples(self):
        self.calls.append("cleared")
        if self.cleanup_error:
            raise ProfileTrainingOperationError("cleanup_failed")

    def sample_peak_bytes(self):
        return 123


def test_application_owns_date_plans_and_complete_pair_publication_order():
    training = _Training()

    result = TrainProfile(training)

    assert result.status == "engineering_ready"
    assert training.calls == [
        "samples", "fitting_open", "diagnostics", "tomorrow", "d25", "published", "fitting_closed", "cleared",
    ]
    assert tuple(head.final_fit.maturity_sessions for head in result.heads) == (1, 5)
    assert all(len(head.final_fit.fit_dates) == 61 and head.diagnostic_plan is training.plan.split for head in result.heads)
    assert training.plan.split.early_stopping_dates == ()


@pytest.mark.parametrize("case", (
    "empty", "missing_head", "missing_partition", "duplicate", "duplicate_missing", "unknown_head", "unknown_partition",
))
def test_incomplete_or_ambiguous_sample_coverage_never_enters_fitting(case):
    training = _Training(coverage_case=case)

    result = TrainProfile(training)

    assert training.calls == ["samples"]
    assert result.status == "blocked"
    assert all(head.failure_reasons == ("profile_training_coverage_contract_invalid",) for head in result.heads)
    assert all(head.model_hash == "" for head in result.heads)


def test_adapter_reported_expected_count_cannot_justify_too_few_effective_dates():
    training = _Training(coverage_case="forged_dates")

    result = TrainProfile(training)

    assert training.calls == ["samples"]
    assert result.status == "blocked"
    assert all(head.failure_reasons == ("profile_training_effective_dates_insufficient",) for head in result.heads)
    assert result.heads[0].coverage[0].expected_dates == 61


def test_sample_date_gap_blocks_both_heads_before_any_native_fit_or_publication():
    training = _Training(missing_coverage=True)

    result = TrainProfile(training)

    assert training.calls == ["samples"]
    assert result.status == "blocked"
    assert all(head.failure_reasons == ("profile_training_effective_dates_insufficient",) for head in result.heads)
    assert result.heads[0].coverage[0].expected_dates == 61


def test_rejected_second_head_cannot_publish_or_return_first_head_as_ready():
    training = _Training(rejected_head=True)

    result = TrainProfile(training)

    assert "published" not in training.calls
    assert result.heads[0].status == "blocked"
    assert result.heads[0].failure_reasons == ("profile_pair_incomplete",)
    assert result.heads[1].status == "rejected"


@pytest.mark.parametrize("error", (ProfileTrainingOperationError, InterruptedError))
def test_fitting_failure_closes_the_injected_scope_and_cancellation_still_propagates(error):
    training = _Training(error=error)

    if error is InterruptedError:
        with pytest.raises(InterruptedError, match="controlled fitting failure"):
            TrainProfile(training)
    else:
        result = TrainProfile(training)
        assert result.status == "blocked"
        assert all(head.model_hash == "" for head in result.heads)
    assert training.calls[-1] == "fitting_closed"
    assert "published" not in training.calls and "cleared" not in training.calls


def test_post_publication_cleanup_failure_does_not_relabel_an_already_committed_pair():
    training = _Training(cleanup_error=True)

    result = TrainProfile(training)

    assert result.status == "engineering_ready"
    assert all(head.model_hash == "e" * 64 for head in result.heads)
    assert training.calls[-2:] == ["fitting_closed", "cleared"]
    assert result.post_commit_issues[0].stage == "sample_cleanup"
    assert result.post_commit_issues[0].error_type == "ProfileTrainingOperationError"


class _CompletionFailure:
    def publish(self, event):
        if event.stage == "artifact_publish" and event.state == "completed":
            raise BrokenPipeError("private completion detail")


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_completed_progress_failure_retains_the_committed_pair_and_visible_diagnostics(cleanup_error):
    training = _Training(cleanup_error=cleanup_error)

    result = TrainProfile(training, progress=_CompletionFailure())

    assert result.status == "engineering_ready"
    assert all(head.model_hash == "e" * 64 and not head.failure_reasons for head in result.heads)
    assert training.calls.count("published") == 1
    assert training.calls[-2:] == ["fitting_closed", "cleared"]
    assert tuple(issue.stage for issue in result.post_commit_issues) == (
        ("completion_notification", "sample_cleanup") if cleanup_error else ("completion_notification",)
    )
    payload = ProjectProfileTrainingResult("v3", result)
    assert payload["post_commit_issues"][0] == {
        "stage": "completion_notification", "error_type": "BrokenPipeError", "cause_type": None,
    }
    assert "private" not in str(payload)


def test_fitting_scope_exit_failure_after_commit_does_not_erase_the_published_heads():
    training = _Training(scope_error=OSError)

    result = TrainProfile(training)

    assert result.status == "engineering_ready"
    assert all(head.model_hash == "e" * 64 and not head.failure_reasons for head in result.heads)
    assert training.calls.count("published") == 1
    assert training.calls[-1] == "fitting_closed"
    assert result.post_commit_issues[0].stage == "scope_cleanup"
    assert result.post_commit_issues[0].error_type == "OSError"


@pytest.mark.parametrize("mismatch", (None, "source", "industry", "target", "label", "contract", "run", "final_fit", "diagnostic", "missing_head"))
def test_application_reuses_only_a_complete_pair_matching_the_current_training_plan(mismatch):
    training = _Training()
    result = TrainProfile(training)
    heads = tuple(PublishedTrainingHead(head, identity.training_contract_hash) for head, identity in zip(
        result.heads, training.source.heads, strict=True,
    ))
    first = heads[0]
    if mismatch == "source":
        first = replace(first, result=replace(first.result, training_input_hash="f" * 64))
    elif mismatch == "industry":
        previous_price_hash = training.source.snapshot_hash
        training.source = replace(training.source, industry_content_hash="f" * 64)
        assert training.source.snapshot_hash == previous_price_hash
    elif mismatch == "target":
        training.source = replace(training.source, target_matches=False)
    elif mismatch == "label":
        first = replace(first, result=replace(first.result, label_cutoff=training.source.source_cutoff))
    elif mismatch == "contract":
        first = replace(first, training_contract_hash="f" * 64)
    elif mismatch == "run":
        first = replace(first, result=replace(first.result, run_id="f" * 64))
    elif mismatch == "final_fit":
        first = replace(first, result=replace(first.result, final_fit=None))
    elif mismatch == "diagnostic":
        first = replace(first, result=replace(first.result, diagnostic_plan=None))
    heads = (first,) if mismatch == "missing_head" else (first, heads[1])

    reused = InspectProfileReuse(training.source, heads)

    if mismatch is None:
        assert reused is not None and reused.reused and reused.heads == result.heads
    else:
        assert reused is None
