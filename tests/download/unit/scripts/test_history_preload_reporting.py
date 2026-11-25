from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.download.history import history_sqlite_performance as preload
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.domain.publication.models import Strategy


@pytest.mark.parametrize(
    ("tomorrow_identity", "company_identity", "status"),
    (
        ("loaded-tomorrow", "pinned-company", "passed"),
        ("loaded-tomorrow", "", "degraded"),
        (None, "pinned-company", "degraded"),
    ),
)
def test_preload_report_projects_the_bound_inputs(monkeypatch, tmp_path, tomorrow_identity, company_identity, status):
    loaded_d25 = SimpleNamespace(
        publications=("utility-publication",),
        heads={Strategy.D25: SimpleNamespace(predictor=SimpleNamespace(model_hash="bound-d25"))},
    )
    references = SimpleNamespace(
        initialize=lambda: None, reference=lambda: SimpleNamespace(open_dates=()), versions=lambda: {},
    )
    projection = SimpleNamespace(
        initialize=lambda _: None, scan_count=1, window_count=1,
        timings=SimpleNamespace(
            read_seconds=0, alignment_seconds=0, derivation_seconds=0, publication_seconds=0,
            extension_requests=0, extension_rows=0,
        ),
    )
    bindings = []
    profile = SimpleNamespace(
        history_sessions=61, bind_inputs=lambda _, **kwargs: bindings.append(kwargs["versions"]["company"]),
        record_projection_observation=lambda: None,
        status=lambda: SimpleNamespace(state="published", maintenance_state="ready", entries=1, maintenance_reason=None),
    )
    company = SimpleNamespace(
        initialize=lambda *_args, **_kwargs: None,
        loaded_identity=company_identity or "company:unavailable",
        status=lambda: SimpleNamespace(last_error="", loaded_identity=company_identity, disk_identity="unloaded-company"),
    )
    reports = []
    monkeypatch.setattr(preload, "PublishedReferences", lambda *_args, **_kwargs: references)
    monkeypatch.setattr(preload, "LoadD25ScoringProfile", lambda *_args, **_kwargs: loaded_d25)
    monkeypatch.setattr(preload, "LoadTomorrowProbabilityModel", lambda *_args, **_kwargs: (
        SimpleNamespace(identity=tomorrow_identity) if tomorrow_identity is not None else None
    ))
    monkeypatch.setattr(preload, "ReadPublishedHistoryUseCase", lambda _: SimpleNamespace(
        manifest=lambda: SimpleNamespace(snapshot_hash="published-history", universe_codes=("000001",)),
    ))
    monkeypatch.setattr(preload, "SharedHistoryProjection", lambda *_args, **_kwargs: projection)
    monkeypatch.setattr(preload, "PublishedHistoryCache", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(preload, "PublishedCompanyResearch", lambda *_args, **_kwargs: company)
    monkeypatch.setattr(preload, "DatabaseMeasurement", lambda *_args, **_kwargs: nullcontext(SimpleNamespace(result={})))
    monkeypatch.setattr(preload, "emit_report", reports.append)
    config = Path("config/runtime.json")
    assert preload._measure_preload(
        tmp_path, config, ("v1",), sqlite_policy=preload.LoadRuntimeSettings(config).sqlite_policy.for_medium("hdd"),
    ) == 0
    report, = reports
    summary = report["summary"]
    assert report["status"] == status
    assert summary["evidence_scope"] == "isolated_cold_start_history_and_company_preload_diagnostic"
    assert summary["snapshot_hash"] == "published-history"
    assert summary["d25_utility_publication_identities"] == {"v1": CanonicalDigest(loaded_d25.publications)}
    assert "model_versions" not in summary
    assert summary["d25_model_identities"] == {"v1": "bound-d25"}
    assert summary["tomorrow_model_identities"] == {"v1": tomorrow_identity}
    assert summary["company_loaded_identity"] == (company_identity or None)
    assert bindings == [company_identity or "company:unavailable"]
