"""A timing recommendation requires complete, equivalent measured samples."""

from dataclasses import replace

import pytest

from scripts.download.company import company_pipeline_audit as audit
from tests.helpers.configuration import SqlitePolicyFixture


def _trials(seconds=(100.0, 80.0, 78.0, 77.0)):
    return tuple(
        audit.CompanyPublicationTrial(interval, duration, "inputs", "facts", "payloads", True)
        for interval, duration in zip((5.0, 15.0, 30.0, 60.0), seconds, strict=True)
        for _round in range(5)
    )


def test_interval_selection_prefers_shortest_within_five_percent_of_fastest():
    assert audit.SelectCompanyPublicationInterval(_trials()) == 15.0
    assert audit.SelectCompanyPublicationInterval(_trials((100.0, 99.0, 98.0, 97.0))) == 5.0
    assert audit.SelectCompanyPublicationInterval(_trials((100.0, 90.0, 85.0, 70.0))) == 60.0


@pytest.mark.parametrize("change", ({"passed": False}, {"facts_digest": "changed"}, {"input_digest": "changed"}, {"payloads_digest": "changed"}))
def test_failed_or_changed_input_never_selects_an_interval(change):
    trials = _trials()
    assert audit.SelectCompanyPublicationInterval((replace(trials[0], **change), *trials[1:])) is None


def test_missing_round_never_selects_an_interval():
    assert audit.SelectCompanyPublicationInterval(_trials()[:-1]) is None


@pytest.mark.parametrize("seconds", (float("nan"), float("inf"), 0, -1))
def test_invalid_duration_is_rejected(seconds):
    trials = _trials()
    with pytest.raises(ValueError, match="company_publication_trial_invalid"):
        audit.SelectCompanyPublicationInterval((replace(trials[0], wall_seconds=seconds), *trials[1:]))


def test_matrix_uses_same_frozen_sample_and_time_without_enabling_production(tmp_path, monkeypatch):
    calls = []
    sample = ()
    monkeypatch.setattr(audit, "_sample", lambda *args, **kwargs: sample)

    def paired(path, directory, **kwargs):
        calls.append((directory, kwargs))
        interval = kwargs["candidate_publication_interval"]
        return {
            "status": "passed",
            "summary": {"pipeline_audit": {
                "input_sample_digest": "inputs",
                "runs": [
                    {"publication_interval_seconds": 5.0, "seconds": 100.0, "facts_digest": "facts", "payloads_digest": "payloads"},
                    {"publication_interval_seconds": interval, "seconds": 80.0, "facts_digest": "facts", "payloads_digest": "payloads"},
                ],
            }},
        }

    monkeypatch.setattr(audit, "AuditCompanyPipeline", paired)
    result = audit.AuditCompanyIoCandidates(tmp_path / "source.sqlite3", tmp_path / "results", budget_seconds=60, sqlite_policy=SqlitePolicyFixture())
    summary = result["summary"]["io_candidates"]
    assert result["status"] == "passed" and len(calls) == 15
    assert len({kwargs["observed_at"] for _directory, kwargs in calls}) == 1
    assert all(kwargs["sample"] is sample for _directory, kwargs in calls)
    assert summary["selected_interval_seconds"] == 15.0
    assert not summary["production_enabled"] and not summary["performance_accepted"]
    assert not summary["directory_prefetch_measured"] and not summary["full_sync_verified"]
