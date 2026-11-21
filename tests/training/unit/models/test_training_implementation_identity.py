"""Sample and model reuse must observe the download-owned training projection."""

from pathlib import Path

import pytest

from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.training.infra.implementation_identity import LaneTrainingImplementationIdentity, TrainingImplementationIdentity


def test_changed_download_training_projection_invalidates_implementation_identity(monkeypatch):
    original = Path.read_bytes
    observed = []

    def changed(path):
        content = original(path)
        if path.as_posix().endswith("/download/infra/history_prices.py"):
            observed.append(path)
            return content + b"\n# fixture: changed training projection\n"
        return content

    TrainingImplementationIdentity.cache_clear()
    try:
        previous = TrainingImplementationIdentity()
        monkeypatch.setattr(Path, "read_bytes", changed)
        TrainingImplementationIdentity.cache_clear()
        current = TrainingImplementationIdentity()
        assert len(observed) == 1 and current != previous
    finally:
        TrainingImplementationIdentity.cache_clear()


@pytest.mark.parametrize(("source", "affected"), (
    ("training/infra/tomorrow/v3/model.py", {(Strategy.TOMORROW, "v3")}),
    ("training/infra/d25/v2/probability.py", {(Strategy.D25, "v2")}),
    ("training/infra/tcn/binary_fitting.py", {(Strategy.TOMORROW, "v5"), (Strategy.D25, "v5")}),
    ("download/infra/history_prices.py", {
        (strategy, profile) for strategy in (Strategy.TOMORROW, Strategy.D25) for profile in SCORING_PROFILE_IDS
    }),
))
def test_only_consuming_heads_change_when_training_source_changes(monkeypatch, source, affected):
    original = Path.read_bytes
    heads = tuple((strategy, profile) for strategy in (Strategy.TOMORROW, Strategy.D25) for profile in SCORING_PROFILE_IDS)

    def changed(path):
        contents = original(path)
        return contents + b"\n# changed training source\n" if path.as_posix().endswith(source) else contents

    LaneTrainingImplementationIdentity.cache_clear()
    try:
        original_ids = {head: LaneTrainingImplementationIdentity(*head) for head in heads}
        assert len(set(original_ids.values())) == 8
        monkeypatch.setattr(Path, "read_bytes", changed)
        LaneTrainingImplementationIdentity.cache_clear()
        assert {head for head in heads if LaneTrainingImplementationIdentity(*head) != original_ids[head]} == affected
    finally:
        LaneTrainingImplementationIdentity.cache_clear()
