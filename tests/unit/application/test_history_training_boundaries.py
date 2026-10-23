from __future__ import annotations

from datetime import date
from pathlib import Path

from trader.download.application.download_history import DownloadHistoryUseCase
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.training.application.profile_training_primary import TrainV2UseCase
from trader.training.application.profile_training_secondary import TrainV3UseCase
from trader.training.application.training_due import TrainingDueUseCase


def _status() -> HistoryMaintenanceStatus:
    return HistoryMaintenanceStatus(
        "completed", None, Path("data/history"), "baostock", None, "a" * 64, date(2026, 9, 15), date(2026, 9, 14)
    )


class _Archive:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def download(self, configuration, supplier, **kwargs):
        del configuration, supplier, kwargs
        self.calls.append("download")
        return _status()


class _Publication:
    def manifest(self):
        return None

    def iter_windows(self, manifest, *, sessions):
        del manifest, sessions
        return iter(())

    def read_windows(self, manifest, codes, *, sessions):
        del manifest, codes, sessions
        return ()


def test_history_use_cases_delegate_to_their_single_port(tmp_path) -> None:
    archive = _Archive()
    configuration = HistorySyncConfiguration(archive_root=tmp_path / "history", reference_root=tmp_path / "reference")
    supplier = object()

    assert DownloadHistoryUseCase(archive).execute(configuration, supplier) is not None  # type: ignore[arg-type]
    assert archive.calls == ["download"]
    assert ReadPublishedHistoryUseCase(_Publication()).manifest() is None


def test_profile_training_use_cases_never_share_runner_state() -> None:
    calls: list[tuple[str, Path, Path]] = []

    def v2(history_root, train_root, **kwargs):
        del kwargs
        calls.append(("v2", history_root, train_root))
        return "v2-result"

    def v3(history_root, train_root, **kwargs):
        del kwargs
        calls.append(("v3", history_root, train_root))
        return "v3-result"

    assert TrainV2UseCase(v2).execute(Path("history"), Path("train")) == "v2-result"
    assert TrainV3UseCase(v3).execute(Path("history"), Path("train")) == "v3-result"
    assert calls == [("v2", Path("history"), Path("train")), ("v3", Path("history"), Path("train"))]


def test_training_due_use_case_is_read_only_and_injected() -> None:
    observed: list[object] = []

    def evaluate(query: object) -> str:
        observed.append(query)
        return "not_due"

    query = object()
    assert TrainingDueUseCase(evaluate).execute(query) == "not_due"
    assert observed == [query]
