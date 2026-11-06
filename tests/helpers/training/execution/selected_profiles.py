"""Four real profile workers share one synthetic history scan and retain independent pairs."""

from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from tests.helpers.training.training_resources import TrainingResourceSettingsFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
import pytest
from tests.helpers.training.profiles.v1.learned_rank_training import _settings
from tests.helpers.training.profiles.v5.v5_training import _history
from trader.infra.settings import LoadRuntimeSettings
from trader.training.application.selected_training import TrainingLocations
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.application.resources import TrainingProfileThreads
from trader.training.infra.execution_options import TrainingExecutionOptions
from trader.training.infra.selected_training import SelectedTrainingRequest, TrainSelectedProfiles
from trader.training.infra.training_settings import LoadProfileTrainingSettings
from tests.helpers.shared.configuration import TcnFittingParametersFixture

ROOT = Path(__file__).resolve().parents[4]


class _Progress:
    def publish(self, _update):
        pass


@dataclass(frozen=True)
class FourProfileSeed:
    models: Path
    execution_case: str
    files: tuple[tuple[Path, bytes], ...]


def ReadModelFiles(root: Path) -> tuple[tuple[Path, bytes], ...]:
    return tuple(
        (path.relative_to(root), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def BuildFourProfileRequest(
    directory: Path,
    execution_case: str,
    monkeypatch: pytest.MonkeyPatch,
    scans: list[frozenset[date]],
    tcn_threads: list[int],
) -> SelectedTrainingRequest:
    import torch
    from trader.training.infra.profile.v5 import network

    configure = network.ConfigureTcnRuntime

    def configure_tcn(*, training, compute_threads):
        configure(training=training, compute_threads=compute_threads)
        tcn_threads.append(torch.get_num_threads())

    monkeypatch.setattr(network, "ConfigureTcnRuntime", configure_tcn)
    history = _history(directory / "history")
    original = history.iter_training_rows
    missing_day = history.snapshot.calendar.open_dates[-10]

    def rows(dates, progress=None):
        scans.append(dates)
        for row in original(dates, progress):
            yield (
                replace(row, point=None, cell_status="unknown_missing")
                if row.trade_date == missing_day and row.code == "600005"
                else row
            )

    history.iter_training_rows = rows
    monkeypatch.setattr(
        "trader.training.infra.selected_training.SQLiteHistoryTrainingInputSnapshot.open", lambda _p, *, sqlite_policy: history
    )
    monkeypatch.setattr("trader.training.infra.selected_training.LoadV1TrainingSettings", lambda _p: _settings())

    def settings(project, profile):
        result = LoadProfileTrainingSettings(project, profile)
        if profile == "v5":
            return replace(
                result,
                fitting=TcnFittingParametersFixture(
                    minimum_industry_training_rows=10,
                    epochs=2,
                    early_stopping_rounds=2,
                ),
            )
        fitting = replace(result.fitting, minimum_industry_training_rows=10)
        if profile == "v2":
            fitting = replace(fitting, boosting_rounds=12, early_stopping_rounds=3, minimum_leaf_rows=5)
        return replace(result, fitting=fitting)

    monkeypatch.setattr("trader.training.infra.selected_training.LoadProfileTrainingSettings", settings)
    candidate = execution_case == "combined"
    cpu_settings = TrainingResourceSettingsFixture().cpu
    if candidate:
        cpu_settings = replace(cpu_settings, native_threads=1, profile_threads=TrainingProfileThreads(1, 1, 1))
    return SelectedTrainingRequest(
        LoadRuntimeSettings(ROOT / "config/runtime.json"),
        ("v1", "v2", "v3", "v5"),
        tuple(_Progress() for _ in range(4)),
        lambda: False,
        TrainingMemoryBudget(3 * 1024**3),
        TrainingLocations(history.history_root, directory / "models", directory / "samples"),
        cpu_budget=TrainingCpuBudget(4 if candidate else 2, settings=cpu_settings),
        fanout_settings=TrainingResourceSettingsFixture().fanout,
        sample_settings=TrainingSampleSettingsFixture(),
        memory_capacity_bytes=3 * 1024**3,
        execution_options=TrainingExecutionOptions(*(candidate for _ in range(5))),
    )


@pytest.fixture(
    scope="module",
    params=(
        pytest.param("baseline", marks=pytest.mark.xdist_group(name="four-profile-seed-baseline")),
        pytest.param("combined", marks=pytest.mark.xdist_group(name="four-profile-seed-combined")),
    ),
)
def four_profile_seed(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[FourProfileSeed]:
    torch = pytest.importorskip("torch", reason="V5 requires the optional CPU tensor dependency")
    execution_case = request.param
    directory = tmp_path_factory.mktemp(f"four-profile-seed-{execution_case}")
    original_threads = torch.get_num_threads()
    original_deterministic = torch.are_deterministic_algorithms_enabled()
    scans: list[frozenset[date]] = []
    tcn_threads: list[int] = []
    with pytest.MonkeyPatch.context() as patches:
        selection = BuildFourProfileRequest(directory, execution_case, patches, scans, tcn_threads)
        with ThreadPoolExecutor(max_workers=4) as executor:
            result = TrainSelectedProfiles(selection, executor, sqlite_policy=SqlitePolicyFixture())
        assert len(scans) == result.history_scan_passes == 1
        assert tuple(item.profile for item in result.outcomes) == selection.profiles
        assert all(item.failure is None and item.result.status == "engineering_ready" for item in result.outcomes), result
        assert len(tuple(selection.paths.models.rglob("model.json"))) == 8
        assert 1 <= selection.cpu_budget.peak_reserved_threads <= (3 if execution_case == "combined" else 1)
        assert all("[num_threads: 1]" in head.rank_model for head in result.outcomes[0].result.heads)
        assert tcn_threads == [1]
        assert torch.get_num_threads() == original_threads
        assert torch.are_deterministic_algorithms_enabled() == original_deterministic
        assert result.timing is not None
        assert all(item.cpu.acquired_count == item.memory.acquired_count == 1 for item in result.timing.profiles)
        assert all(item.cpu.inflight_count == item.memory.inflight_count == 0 for item in result.timing.profiles)
        assert all(item.finished_after_seconds >= item.started_after_seconds >= 0 for item in result.timing.profiles)
        seed = FourProfileSeed(selection.paths.models, execution_case, ReadModelFiles(selection.paths.models))
    yield seed
    assert ReadModelFiles(seed.models) == seed.files
