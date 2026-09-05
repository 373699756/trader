import json
from datetime import date, timedelta
from importlib import resources

import pytest

from trader.application.research.tomorrow_v3_training import TomorrowV3TrainingWindow
from trader.domain.research.baostock_daily import build_baostock_v3_split
from trader.domain.recommendation.model_scoring import V3_EXPOSURE_CONTRACT, residualize_exposure
from trader.infra.research.tomorrow_v3_training import _aligned_sample_dates, _model_document, _residualize_sample_day
from trader.infra.scoring.artifact_hashing import artifact_content_hash
from trader.infra.scoring.profiles.v3.bundle_codec import decode_v3_tomorrow_bundle


def test_training_window_never_authorizes_the_latest_two_hundred_dates() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1250))
    split = build_baostock_v3_split(dates, parent_manifest_hash="a" * 64)
    window = TomorrowV3TrainingWindow(split)

    assert window.readable_dates.isdisjoint(split.point_in_time_holdout_dates)
    assert split.daily_proxy_holdout_dates[-1] in window.readable_dates
    with pytest.raises(ValueError, match="point-in-time holdout"):
        window.require_readable((split.point_in_time_holdout_dates[0],))


def test_training_window_rejects_dates_outside_the_frozen_manifest() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1250))
    window = TomorrowV3TrainingWindow(build_baostock_v3_split(dates, parent_manifest_hash="a" * 64))

    with pytest.raises(ValueError, match="outside the frozen split"):
        window.require_readable((date(2020, 1, 1),))


def test_v3_sample_dates_use_global_calendar_and_reject_gaps() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(100))
    readable = frozenset(dates)
    available = set(dates)
    available.remove(dates[69])

    samples = _aligned_sample_dates(dates, available, readable)

    assert all(day != dates[68] and day != dates[69] and next_day != dates[69] for day, next_day, _ in samples)
    assert all(next_day == dates[indices[0] + 1] for _day, next_day, indices in samples)


def test_v3_sample_dates_do_not_pad_short_listing_history() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(100))
    available = set(dates[30:])

    samples = _aligned_sample_dates(dates, available, frozenset(dates))

    assert samples
    assert samples[0][0] == dates[95]


def test_v3_sample_dates_require_every_amount_window_session() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(100))
    available = set(dates)
    available.remove(dates[70])

    samples = _aligned_sample_dates(dates, available, frozenset(dates))

    assert all(day != dates[80] for day, _next_day, _indices in samples)


def test_v3_training_uses_the_shared_online_exposure_contract() -> None:
    momenta = ((1.0, 10.0), (3.0, 8.0), (2.0, 6.0), (8.0, 4.0))
    boards = ("main", "main", "star", "star")
    industries = ("bank", "software", "bank", "software")
    amounts = (10.0, 20.0, 15.0, 30.0)

    result = _residualize_sample_day(momenta, boards, industries, amounts)
    expected = tuple(
        residualize_exposure(
            tuple(row[offset] for row in momenta),
            boards,
            amounts,
            industries=industries,
            contract=V3_EXPOSURE_CONTRACT,
        )
        for offset in range(2)
    )

    for actual_values, expected_values in zip(result, expected, strict=True):
        assert actual_values == pytest.approx(expected_values)


def test_v3_training_document_is_accepted_by_the_production_codec() -> None:
    p2 = json.loads(
        resources.files("trader.resources.models").joinpath("tomorrow_p2_model.json").read_text(encoding="utf-8")
    )
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1250))
    split = build_baostock_v3_split(dates, parent_manifest_hash="a" * 64)
    industry_model: dict[str, object] = {
        "transformer_means": [0.0] * 6,
        "transformer_scales": [1.0] * 6,
        "ridge_intercept": 0.0,
        "ridge_coefficients": [0.1] * 6,
        "lightgbm_model": p2["lightgbm_model"],
        "lightgbm_best_iteration": p2["lightgbm_best_iteration"],
        "calibration_intercept": 0.0,
        "calibration_slope": 1.0,
        "training_rows": 20_000,
        "validation_rows": 1_000,
    }
    document = _model_document("a" * 64, split, "b" * 64, {"银行": industry_model}, 20_000, 1_000)
    document["content_hash"] = artifact_content_hash(document)

    artifact = decode_v3_tomorrow_bundle(document)

    assert artifact.feature_ids[-1] == "qfq_residual_momentum_60d_skip5"
    assert artifact.exposure_contract == V3_EXPOSURE_CONTRACT
    assert tuple(industry for industry, _model in artifact.industries) == ("银行",)
