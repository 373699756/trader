"""Ambiguous or unowned publication residues stay untouched during explicit recovery."""

import shutil
from pathlib import Path

import pytest

from trader.training.infra.model_bundles.profile_publication import (
    ProfilePublicationBusyError,
    ProfilePublicationLock,
    RecoverProfilePublication,
    RecoverProfileBundles,
)
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from tests.unit.training.infra.profile.v3.test_profile import _publish_profile


def _pair(root: Path) -> None:
    for head in ("tomorrow", "d25"):
        directory = root / head
        directory.mkdir(parents=True)
        for name in ("model.json", "metadata.json"):
            (directory / name).write_text(head, encoding="utf-8")


def _residue_signature(path: Path) -> tuple[int, int, int, int, int, int, str | bytes]:
    stat = path.lstat()
    content = path.readlink().as_posix() if path.is_symlink() else path.read_bytes() if path.is_file() else b""
    # Inspecting a symlink or directory can change atime on a real filesystem.
    # Content, inode, permissions and modification metadata must stay unchanged.
    return stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, content


@pytest.mark.parametrize(
    "damage",
    ("ambiguous", "name", "root_link", "head_link", "file_link", "extra", "missing", "staging_extra", "invalid"),
)
def test_recovery_preserves_all_residue_when_ownership_or_pair_is_invalid(tmp_path, damage):
    root = tmp_path / "v3"
    previous = tmp_path / (".v3.previous." + "a" * 32)
    _pair(previous)
    publication = tmp_path / ".v3.publication.orphan"
    _pair(publication)
    if damage == "ambiguous":
        _pair(tmp_path / (".v3.previous." + "b" * 32))
    elif damage == "name":
        previous.rename(tmp_path / ".v3.previous.unowned")
    elif damage == "root_link":
        outside = tmp_path / "outside"
        previous.rename(outside)
        previous.symlink_to(outside, target_is_directory=True)
    elif damage == "head_link":
        head = previous / "tomorrow"
        head.rename(tmp_path / "outside")
        head.symlink_to(tmp_path / "outside", target_is_directory=True)
    elif damage == "file_link":
        file = previous / "d25/model.json"
        file.rename(tmp_path / "outside")
        file.symlink_to(tmp_path / "outside")
    elif damage == "extra":
        (previous / "notes").write_text("unowned", encoding="utf-8")
    elif damage == "missing":
        (previous / "d25/metadata.json").unlink()
    elif damage == "staging_extra":
        staging = previous / ".bundle-staging.abcdefgh"
        staging.mkdir()
        (staging / "notes").write_text("unowned", encoding="utf-8")
    before = {str(path.relative_to(tmp_path)): _residue_signature(path) for path in tmp_path.rglob("*")}

    def validate(path):
        assert path == previous
        raise ValueError("invalid pair")

    with pytest.raises(ValueError):
        RecoverProfilePublication(root, validate)
    assert not root.exists()
    assert all(_residue_signature(tmp_path / path) == signature for path, signature in before.items())
    assert len(tuple(tmp_path.rglob("*"))) == len(before) + 1  # Only the publication lock was created.


def test_recovery_never_promotes_an_unpublished_candidate_and_requires_the_lock(tmp_path):
    root = tmp_path / "v3"
    publication = tmp_path / ".v3.publication.orphan"
    _pair(publication)

    def validate(_path):
        pytest.fail("an unpublished pair must not be validated as a previous pair")

    RecoverProfilePublication(root, validate)
    assert not root.exists() and publication.exists()
    with ProfilePublicationLock(root):
        with pytest.raises(ProfilePublicationBusyError):
            RecoverProfilePublication(root, validate, timeout_seconds=0)


def test_recovery_is_idempotent_and_keeps_a_completed_publication(tmp_path):
    root = tmp_path / "v3"
    previous = tmp_path / (".v3.previous." + "a" * 32)
    _pair(previous)
    validated = []
    RecoverProfilePublication(root, validated.append)
    assert validated == [previous] and root.exists() and not previous.exists()
    shutil.copytree(root, previous)
    RecoverProfilePublication(root, validated.append)
    assert validated == [previous] and previous.exists()


def test_recovery_of_first_installation_restores_only_the_empty_training_workspace(tmp_path):
    root = tmp_path / "v3"
    previous = tmp_path / (".v3.previous." + "a" * 32)
    staging = previous / ".bundle-staging.abcdefgh"
    staging.mkdir(parents=True)
    (staging / "model.json").write_text("partial staging", encoding="utf-8")
    publication = tmp_path / ".v3.publication.orphan"
    _pair(publication)

    def validate(_path):
        pytest.fail("a first-install workspace contains no previous model to reuse")

    RecoverProfilePublication(root, validate)
    assert not (root / "tomorrow").exists() and not (root / "d25").exists()
    assert (root / staging.name / "model.json").read_text(encoding="utf-8") == "partial staging"
    assert publication.exists() and not previous.exists()


def test_recovery_validates_model_content_before_restoring_the_previous_pair(tmp_path):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    previous = tmp_path / (".v3.previous." + "a" * 32)
    root.rename(previous)
    (previous / "d25/metadata.json").write_text("{}", encoding="utf-8")
    before = {path.relative_to(previous): path.read_bytes() for path in previous.rglob("*.json")}
    with pytest.raises(ValueError):
        RecoverProfileBundles(root, V3_TRAINING_PROFILE)
    assert not root.exists()
    assert before == {path.relative_to(previous): path.read_bytes() for path in previous.rglob("*.json")}
