from __future__ import annotations
from dataclasses import replace
from trader.training.application.baseline_replay_report import BaselineReplaySelection


class _ReplayEvaluator:
    def replay(self, day):  # noqa: ANN001
        return tuple(
            BaselineReplaySelection(item.code, rank if rank <= 2 else None, rank if rank <= 2 else None)
            for rank, item in enumerate(sorted(day.evaluated, key=lambda value: value.code), start=1)
        )


class _RecallEvaluator(_ReplayEvaluator):
    def replay(self, day):  # noqa: ANN001
        ordered = sorted(day.evaluated, key=lambda value: value.code)
        return (
            BaselineReplaySelection(ordered[0].code, 1, 1),
            BaselineReplaySelection(ordered[1].code, 2, None),
            BaselineReplaySelection(ordered[2].code, None, 2),
        )


class _NoFreezeEvaluator(_ReplayEvaluator):
    def replay(self, day):  # noqa: ANN001
        return tuple(BaselineReplaySelection(item.code, None, None) for item in day.evaluated)


class _InvalidReplayEvaluator(_ReplayEvaluator):
    def replay(self, day):  # noqa: ANN001
        result = super().replay(day)
        return (replace(result[0], production_rank=2), replace(result[1], production_rank=2), result[2])
