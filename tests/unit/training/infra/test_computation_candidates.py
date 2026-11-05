"""Exact values, bounded validation reuse and restoration of diagnostic-only hooks."""

from tests.helpers.training_resources import TrainingSampleSettingsFixture

import struct
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.checks.training.computation_candidates import UseComputationCandidate
from trader.recommendation.domain.market.feature_contracts import CalculateProfileQfqAlpha, QfqPriceAnchors
from trader.training.infra import sample_builder, sample_index, sample_schema
from trader.training.infra.sample_sequences import SampleSequenceLayout


def _bits(values):
    return tuple((item.feature_id.value, None if item.value is None else struct.pack("!d", item.value)) for item in values)


@pytest.mark.parametrize("horizons", ((20, 40, 60), (20, 40, 60, 120, 250)))
@pytest.mark.parametrize("price", (10.0, 0.0, -0.0, -1.0, None, float("nan"), float("inf")))
def test_indexed_anchors_preserve_every_value_missing_mask_and_feature_identity(horizons, price):
    anchors = QfqPriceAnchors(price, tuple((h, price if h == 5 else 2.5 + h / 100) for h in (1, 3, 5, *horizons)))
    expected = _bits(CalculateProfileQfqAlpha(anchors, horizons))
    original = sample_builder.CalculateProfileQfqAlpha
    with UseComputationCandidate("indexed_anchors") as counts:
        actual = sample_builder.CalculateProfileQfqAlpha(anchors, horizons)
        assert _bits(actual) == expected
        assert counts.indexed_anchor_calls == 1
    assert sample_builder.CalculateProfileQfqAlpha is original


def test_schema_cache_keeps_column_order_layout_and_format_checks_and_is_bounded():
    names = ("alpha", "momentum")
    expected = sample_schema.SampleFeatureHash(names, None)
    layout = SampleSequenceLayout(1, 1, 1)
    with UseComputationCandidate("schema_cache") as counts:
        assert sample_schema.SampleFeatureHash(names, None) == expected
        assert sample_index.SampleFeatureHash(names, None) == expected
        assert counts.schema_requests == 2 and counts.schema_computations == 1
        assert sample_schema.SampleFeatureHash(tuple(reversed(names)), None) != expected
        assert sample_schema.SampleFeatureHash(names, layout) != expected
        for _ in range(2):
            with pytest.raises(ValueError):
                sample_schema.SampleFeatureHash(("trade_date",), None)
        assert counts.schema_computations == 5
        for number in range(65):
            sample_schema.SampleFeatureHash((f"factor_{number}",), None)
        before = counts.schema_computations
        assert sample_index.SampleFeatureHash(names, None) == expected
        assert counts.schema_computations == before + 1


def test_layout_scalar_types_cannot_collide_in_validation_cache():
    names = ("alpha", "momentum")
    layouts = (SampleSequenceLayout(1, 1, 1), SampleSequenceLayout(True, 1, 1), SampleSequenceLayout(1.0, 1, 1))
    expected = tuple(sample_schema.SampleFeatureHash(names, layout) for layout in layouts)
    assert len(set(expected)) == 3
    with UseComputationCandidate("schema_cache") as counts:
        assert tuple(sample_schema.SampleFeatureHash(names, layout) for layout in layouts) == expected
        assert counts.schema_computations == 3


@pytest.mark.parametrize("failure", (RuntimeError, InterruptedError, KeyboardInterrupt))
def test_failure_restores_all_hooks_and_drops_job_local_schema_cache(failure):
    original = (sample_builder.CalculateProfileQfqAlpha, sample_schema.SampleFeatureHash, sample_index.SampleFeatureHash)
    with pytest.raises(failure), UseComputationCandidate("combined"):
        sample_schema.SampleFeatureHash(("alpha",), None)
        raise failure("fixture")
    assert (sample_builder.CalculateProfileQfqAlpha, sample_schema.SampleFeatureHash, sample_index.SampleFeatureHash) == original
    with UseComputationCandidate("schema_cache") as counts:
        sample_schema.SampleFeatureHash(("alpha",), None)
        assert counts.schema_computations == 1


def test_hooks_are_rejected_outside_serial_worker_and_unknown_candidate():
    def enter():
        with UseComputationCandidate("combined"):
            pytest.fail("worker-thread hook admitted")

    with ThreadPoolExecutor(max_workers=1) as executor, pytest.raises(ValueError, match="serial_worker"):
        executor.submit(enter).result(timeout=2)
    with pytest.raises(ValueError, match="candidate_invalid"), UseComputationCandidate("unknown"):
        pass


def test_nonmatching_populations_are_never_shared_and_raw_formula_owns_the_calculation():
    from scripts.checks.training.computation_candidates import _IndexedPriceAnchors

    first = QfqPriceAnchors(10.0, ((1, 9.0), (3, 8.0), (5, 7.0), (20, 6.0)))
    second = QfqPriceAnchors(10.0, ((1, 8.0), (3, 7.0), (5, 6.0), (20, 5.0)))
    with UseComputationCandidate("combined"):
        assert _bits(sample_builder.CalculateProfileQfqAlpha(first, (20,))) == _bits(CalculateProfileQfqAlpha(first, (20,)))
        assert _bits(sample_builder.CalculateProfileQfqAlpha(second, (20,))) == _bits(CalculateProfileQfqAlpha(second, (20,)))
    cached = _IndexedPriceAnchors(first.current_close, first.lagged_closes)
    with pytest.raises(TypeError):
        cached._prices[1] = 100.0


@pytest.mark.parametrize("profile_id", ("v2", "v3", "v5"))
def test_full_fixture_sample_rows_partitions_and_sequences_match_baseline(tmp_path, profile_id):
    from tests.helpers.configuration import SqlitePolicyFixture
    from tests.unit.training.infra.test_selected_profiles import _history
    from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
    from trader.training.application.tomorrow_training import TomorrowTrainingWindow
    from trader.training.domain.training_dates import BuildTrainingDateSplit
    from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
    from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
    from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
    from trader.training.infra.training_settings import LoadProfileTrainingSettings
    from pathlib import Path

    history = _history(tmp_path / "history")
    profile = {"v2": V2_TRAINING_PROFILE, "v3": V3_TRAINING_PROFILE, "v5": V5_TRAINING_PROFILE}[profile_id]
    settings = LoadProfileTrainingSettings(Path(__file__).resolve().parents[4], profile_id)
    split = BuildTrainingDateSplit(
        history.snapshot.calendar.open_dates,
        parent_manifest_hash=history.snapshot.snapshot_hash,
        fit_dates=settings.fit_dates,
        history_sessions=profile.history_sessions,
        early_stopping_sessions=20 if profile.algorithm != "ridge" else 0,
        recent_fit=profile.algorithm == "tcn",
        history_excludes_signal=profile.algorithm == "tcn",
    )
    results = []
    for label in ("baseline", "combined"):
        path = tmp_path / (label + ".sqlite3")
        layout = SampleSequenceLayout(6, 60, 6) if profile.algorithm == "tcn" else None
        with UseComputationCandidate(label), sample_index.SQLiteTrainingSampleIndex(
            path, profile.heads[0].feature_manifest.names, sequence_layout=layout, sqlite_policy=SqlitePolicyFixture(),
            sample_settings=TrainingSampleSettingsFixture(),
        ) as index:
            sample_builder.BuildTrainingSamples(sample_builder.TrainingSampleBuildRequest(
                history, history.snapshot.training_codes, TomorrowTrainingWindow(split), index, profile
            ))
        with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=SqlitePolicyFixture()) as connection:
            tables = tuple(row[0] for row in connection.run("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
            contents = tuple((table, tuple(tuple(row) for row in connection.run('SELECT * FROM "' + table + '"'))) for table in tables)
        results.append(contents)
    assert results[0] == results[1]
    assert any(table == "samples" and rows for table, rows in results[0])


def test_existing_worker_projects_candidate_counts_without_claiming_performance(tmp_path, monkeypatch):
    from scripts.checks.training import sample_comparison_worker
    from tests.helpers.configuration import SqlitePolicyFixture

    def construct(*_args, **_kwargs):
        sample_schema.SampleFeatureHash(("alpha",), None)
        sample_index.SampleFeatureHash(("alpha",), None)
        sample_builder.CalculateProfileQfqAlpha(QfqPriceAnchors(10.0, ((1, 9.0),)), ())
        return {"fixture": True, "models_trained": 0}

    monkeypatch.setattr(sample_comparison_worker, "_construct_samples", construct)
    report = sample_comparison_worker._measure_feature_construction(
        tmp_path, tmp_path / "output", 6, 2000, sqlite_policy=SqlitePolicyFixture(), computation_candidate="combined"
    )
    assert report["computation_candidate"] == {
        "name": "combined", "indexed_anchor_calls": 1, "schema_requests": 2, "schema_computations": 1,
        "schema_cache_capacity": 64, "production_enabled": False, "performance_evidence": False,
    }
