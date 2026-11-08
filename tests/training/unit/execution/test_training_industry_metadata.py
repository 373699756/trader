"""Training identities bind current grouping while artifacts retain the adopted evidence."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from trader.training.domain.training_input_identity import BuildTrainingInputHash, TRAINING_INDUSTRY_POLICY
from trader.training.infra.training_industry_metadata import (
    ParseTrainingIndustrySnapshot, ProjectTrainingIndustrySnapshot, ValidateTrainingInputIdentity,
)


def test_industry_metadata_round_trip_retains_all_mapping_and_publication_evidence():
    snapshot = CurrentIndustrySnapshotFixture(("600001", "600002")).project(("600001", "600002", "600003"))
    document = ProjectTrainingIndustrySnapshot(snapshot)
    assert ParseTrainingIndustrySnapshot(document) == snapshot
    assert document["publication_hash"] == snapshot.publication_hash
    assert document["members"][0]["reference"]["industry_id"] == "capco:J66货币金融服务"
    assert document["gaps"] == [{"code": "600003", "reason": "current_industry_not_published"}]


def test_semantic_identity_ignores_refresh_but_changes_with_price_or_current_mapping():
    original = CurrentIndustrySnapshotFixture(("600001",))
    refreshed = replace(
        original, observed_at=original.observed_at + timedelta(days=1),
        refresh_after=original.refresh_after + timedelta(days=1), publication_hash="e" * 64,
        members=(replace(original.members[0], reference=replace(original.members[0].reference, data_version="refreshed")),),
    )
    changed = CurrentIndustrySnapshotFixture(("600001",), sectors=(("I65", "软件和信息技术服务业"),))
    first = BuildTrainingInputHash("a" * 64, original.content_hash)
    assert first == BuildTrainingInputHash("a" * 64, refreshed.content_hash)
    assert first != BuildTrainingInputHash("b" * 64, original.content_hash)
    assert first != BuildTrainingInputHash("a" * 64, changed.content_hash)
    assert ProjectTrainingIndustrySnapshot(original) != ProjectTrainingIndustrySnapshot(refreshed)


@pytest.mark.parametrize("policy", (None, "historical_intervals"))
def test_legacy_or_missing_industry_policy_is_rejected(policy):
    snapshot = CurrentIndustrySnapshotFixture(("600001",))
    with pytest.raises(ValueError, match="training_input_industry_identity_mismatch"):
        ValidateTrainingInputIdentity({
            "price_snapshot_hash": "a" * 64, "industry_content_hash": snapshot.content_hash,
            "industry_policy": policy, "training_input_hash": BuildTrainingInputHash("a" * 64, snapshot.content_hash),
        })


def test_input_hash_and_adopted_mapping_cannot_disagree():
    snapshot = CurrentIndustrySnapshotFixture(("600001",))
    with pytest.raises(ValueError, match="training_input_industry_identity_mismatch"):
        ValidateTrainingInputIdentity({
            "price_snapshot_hash": "a" * 64, "industry_content_hash": snapshot.content_hash,
            "industry_policy": TRAINING_INDUSTRY_POLICY, "training_input_hash": "a" * 64,
        })
    document = ProjectTrainingIndustrySnapshot(snapshot)
    document["content_hash"] = "b" * 64
    with pytest.raises(ValueError, match="training_industry_content_hash_mismatch"):
        ParseTrainingIndustrySnapshot(document)
