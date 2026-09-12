from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_history_repack_has_one_typed_owner_and_a_thin_explicit_cli() -> None:
    owner = ROOT / "src/trader/infra/research/history_archive_repack.py"
    codec = ROOT / "src/trader/infra/research/history_archive_repack_codec.py"
    command = ROOT / "scripts/repack_baostock_history.py"

    assert owner.is_file()
    assert codec.is_file()
    assert command.is_file()
    owner_source = owner.read_text(encoding="utf-8")
    codec_source = codec.read_text(encoding="utf-8")
    command_source = command.read_text(encoding="utf-8")
    assert "class HistoryArchiveRepackCoordinator" in owner_source
    assert "HistoryArchiveRepackBuildState" in owner_source
    assert "HistoryArchiveActivationJournal" in owner_source
    assert "PRAGMA page_size=8192" in owner_source
    assert "VACUUM INTO ?" in owner_source
    assert "history_archive_repack_status" in command_source
    assert all(name in command_source for name in ('"build"', '"activate"', '"rollback"', '"finalize"'))
    assert "dict[" not in owner_source
    assert "Mapping[" not in owner_source
    assert "artifact_content_hash" in codec_source


def test_history_repack_does_not_add_a_run_sh_public_command() -> None:
    run_script = (ROOT / "run.sh").read_text(encoding="utf-8")

    assert "repack_baostock_history" not in run_script
    assert "repack-history" not in run_script


def test_repacked_snapshot_cannot_rebind_the_retired_history_training_cache() -> None:
    retired_cache = ROOT / "src/trader/infra/research/history_training_cache.py"

    assert not retired_cache.exists()
