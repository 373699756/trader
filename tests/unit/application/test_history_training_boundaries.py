from __future__ import annotations

from datetime import date
from pathlib import Path

from trader.download.application.download_history import DownloadHistoryUseCase
from trader.download.application.history_status import HistoryStatusUseCase
from trader.download.application.update_history import UpdateHistoryUseCase
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.infra.history_archive_gateway import HistoryArchiveGateway
from trader.training.application.profile_training_primary import TrainV2UseCase
from trader.training.application.profile_training_secondary import TrainV3UseCase
from trader.training.application.training_due import TrainingDueUseCase


def _status() -> HistoryMaintenanceStatus:
    return HistoryMaintenanceStatus(
        "completed",
        None,
        Path("data/history/baostock"),
        "baostock",
        None,
        "a" * 64,
        date(2026, 9, 15),
        date(2026, 9, 14),
        0,
        False,
        "data_incomplete",
        False,
    )


class _Archive:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def download(self, configuration, supplier, **kwargs):
        del configuration, supplier, kwargs
        self.calls.append("download")
        return _status()

    def update(self, configuration, supplier, **kwargs):
        del configuration, supplier, kwargs
        self.calls.append("update")
        return _status()


class _Status:
    def read(self, archive_root: Path) -> HistoryMaintenanceStatus:
        assert archive_root == Path("archive")
        return _status()


def test_history_use_cases_delegate_to_their_single_port() -> None:
    archive = _Archive()
    configuration = HistorySyncConfiguration()
    supplier = object()

    assert DownloadHistoryUseCase(archive).execute(configuration, supplier) is not None  # type: ignore[arg-type]
    assert UpdateHistoryUseCase(archive).execute(configuration, supplier) is not None  # type: ignore[arg-type]
    assert HistoryStatusUseCase(_Status()).execute(Path("archive")).state == "completed"
    assert archive.calls == ["download", "update"]


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


def test_history_update_fails_closed_without_active_snapshot(tmp_path: Path) -> None:
    configuration = HistorySyncConfiguration(archive_root=tmp_path / "baostock")
    status = HistoryArchiveGateway().update(configuration, object())  # type: ignore[arg-type]

    assert status.state == "blocked"
    assert status.reason == "history_archive_missing"
