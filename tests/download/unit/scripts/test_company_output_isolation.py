"""Company audit outputs stay separate from external runtime data too."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.download.company.commands import BuildCommands
from scripts.download.company.options import CompanyOptions
from scripts.download.company.output_paths import RequireIsolatedReplay


@pytest.mark.parametrize("relation", ("same", "child", "parent", "symlink"))
def test_replay_rejects_every_overlap_before_writing(tmp_path: Path, relation: str) -> None:
    protected = tmp_path / "current" / "data"
    protected.mkdir(parents=True)
    replay = protected if relation == "same" else protected / "replay" if relation == "child" else protected.parent
    if relation == "symlink":
        replay = tmp_path / "linked"
        replay.symlink_to(protected, target_is_directory=True)
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    with pytest.raises(ValueError, match="company_replay_overlaps"):
        RequireIsolatedReplay(replay, (protected,))
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


def test_public_replay_rejects_external_current_data_before_dispatch(tmp_path: Path, monkeypatch) -> None:
    import trader.infra.settings

    data = tmp_path / "current" / "data"
    runtime = SimpleNamespace(
        project_root=tmp_path / "product", runtime_dir=tmp_path / "current" / "runtime",
        data_paths=SimpleNamespace(root=data, company=data / "facts.sqlite3", company_collection=data / "raw.sqlite3"),
    )
    monkeypatch.setattr(trader.infra.settings, "LoadRuntimeSettings", lambda config: runtime)
    options = CompanyOptions("company-resume", company_replay_directory=data / "replay")
    with pytest.raises(ValueError, match="company_replay_overlaps"):
        BuildCommands(options)
    assert not data.exists()


def test_independent_replay_is_only_validated_without_creating_it(tmp_path: Path) -> None:
    replay = tmp_path / "diagnostic" / "replay"
    RequireIsolatedReplay(replay, (tmp_path / "product", tmp_path / "current"))
    assert not replay.exists()
