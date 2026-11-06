from __future__ import annotations

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.training.application.training_due import TrainingDueUseCase


class _Publication:
    def manifest(self):
        return None

    def iter_windows(self, manifest, *, sessions, codes=None, check=None):
        del manifest, sessions, check
        return iter(())

    def read_windows(self, manifest, codes, *, sessions, check=None):
        del manifest, codes, sessions, check
        return ()


def test_published_history_use_case_reads_its_injected_port() -> None:
    assert ReadPublishedHistoryUseCase(_Publication()).manifest() is None


def test_training_due_use_case_is_read_only_and_injected() -> None:
    observed: list[object] = []

    def evaluate(query: object) -> str:
        observed.append(query)
        return "not_due"

    query = object()
    assert TrainingDueUseCase(evaluate).execute(query) == "not_due"
    assert observed == [query]
