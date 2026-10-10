import hashlib
from dataclasses import replace

from tests.unit.application.test_decision_queries import _Repository
from tests.unit.application.test_recommendation_boundaries import _features, _runtime_policy
from tests.unit.application.test_tomorrow_projection import _native_input
from tests.unit.domain.test_decision_identity import NOW, decision
from trader.http_api.response.decision_projection import serialize_event
from trader.recommendation.application.pipeline.freeze_publish.decision_events import build_decision_committed
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedDecisionEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.pipeline.local_score.base_scoring import LocalScoringService
from trader.recommendation.domain.publication.decision_identity import (
    CommittedDecisionRecord,
    DecisionOverlay,
    formal_scored_decision,
)
from trader.recommendation.domain.publication.models import Strategy


class _Clock:
    def __init__(self, at):
        self.at = at

    def now(self):
        return self.at


def _no_content_digest(*args, **kwargs):
    raise AssertionError("realtime publication must not serialize content for SHA-256")


def test_scoring_publication_get_and_sse_do_not_compute_content_hash(monkeypatch, application_feature_factory):
    policy = _runtime_policy()
    native = _native_input(_features(application_feature_factory))
    monkeypatch.setattr(hashlib, "sha256", _no_content_digest)
    projection = LocalScoringService().score(native, policy, sequence=1)
    current = projection.local
    index = UnifiedDecisionIndex()
    assert index.publish(current, expected_version=None).accepted
    queries = UnifiedDecisionQueries(index, UnifiedDecisionDraftIndex(), _Repository(), _Clock(current.observed_at))
    view = queries.current(Strategy.TOMORROW)
    patch = serialize_event(UnifiedDecisionEventStream().publish_committed(build_decision_committed(current)))
    assert view.decision_version == current.version
    assert view.content_hash is None
    assert patch["snapshot_id"] == view.decision_version
    assert patch["content_hash"] is None


def test_same_sequence_conflicts_and_same_time_overlay_corrections_are_distinct(monkeypatch):
    monkeypatch.setattr(hashlib, "sha256", _no_content_digest)
    current = decision()
    index = UnifiedDecisionIndex()
    assert index.publish(current, expected_version=None).accepted
    conflicting = replace(current, items=(replace(current.items[0], final_score=80.0, local_score=80.0),))
    assert conflicting.version == current.version
    assert not index.publish(conflicting, expected_version=current.version).accepted
    quote = current.items[0].quote
    first = DecisionOverlay(current.strategy, current.trade_date, current.version, NOW, (quote,), sequence=2)
    assert index.publish_overlay(first, expected_version=None).accepted
    corrected = replace(first, sequence=3, quotes=(replace(quote, price=quote.price + 0.01),))
    assert corrected.version != first.version
    assert index.publish_overlay(corrected, expected_version=first.version).accepted
    same_coordinate = replace(corrected, quotes=(replace(quote, price=quote.price + 0.02),))
    assert index.publish_overlay(same_coordinate, expected_version=corrected.version).reason == "overlay_conflict"


def test_formal_projection_changes_etag_without_a_content_digest(monkeypatch):
    monkeypatch.setattr(hashlib, "sha256", _no_content_digest)
    current = decision()
    index = UnifiedDecisionIndex()
    assert index.publish(current, expected_version=None).accepted
    queries = UnifiedDecisionQueries(index, UnifiedDecisionDraftIndex(), _Repository(), _Clock(current.observed_at))
    live = queries.current(current.strategy)
    record = CommittedDecisionRecord(formal_scored_decision(current), NOW.replace(hour=15, minute=0), "scheduled")
    assert index.restore_formal(record)
    queries = UnifiedDecisionQueries(
        index, UnifiedDecisionDraftIndex(), _Repository(record), _Clock(record.committed_at)
    )
    formal = queries.current(current.strategy)
    assert formal.frozen
    assert formal.etag != live.etag
    assert formal.decision_version == record.decision.version
    assert formal.items == live.items
