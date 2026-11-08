"""Cleanup and publication preserve every unowned object before changing a profile."""

from pathlib import Path

import pytest

from tests.helpers.training.profiles.v3.profile import _publish_profile
from trader.training.infra.model_bundles import profile_publication
from trader.training.infra.model_bundles.profile_publication import (
    ClearProfileStaging,
    InspectPublishedProfileBundles,
    PublishProfileBundles,
    RecoverProfileBundles,
)
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE


def _snapshot(root: Path) -> dict[str, tuple[int, int, int, int, int, int, str | bytes]]:
    result = {}
    for path in (root, *root.rglob("*")):
        if path.name == ".v3.publication.lock":
            continue
        stamp = path.lstat()
        content = path.readlink().as_posix() if path.is_symlink() else path.read_bytes() if path.is_file() else b""
        result[str(path.relative_to(root))] = (
            stamp.st_dev, stamp.st_ino, stamp.st_mode, stamp.st_size,
            stamp.st_mtime_ns, stamp.st_ctime_ns, content,
        )
    return result


def _staged(parent: Path):
    return tuple(
        (head.strategy, parent / f"staging-v3-{head.strategy.value}")
        for head in V3_TRAINING_PROFILE.heads
    )


def _publication_candidates(parent: Path) -> tuple[Path, ...]:
    return tuple(path for path in parent.glob(".v3.publication.*") if path.name != ".v3.publication.lock")


@pytest.mark.parametrize("action", ("cleanup", "publish", "recover"))
@pytest.mark.parametrize("damage", (
    "notes", "directory", "old_workspace", "staging_name", "staging_case", "staging_extra",
    "staging_link", "staging_file_link", "head_link", "head_file_link", "head_extra",
))
def test_every_mutating_profile_boundary_preserves_the_whole_tree_on_unknown_content(tmp_path, action, damage):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    owned = root / ".bundle-staging.aaaabbbb"
    owned.mkdir()
    (owned / "model.json").write_text("owned partial staging", encoding="utf-8")
    if damage == "notes":
        (root / "notes").write_text("operator notes", encoding="utf-8")
    elif damage == "directory":
        (root / "operator-data").mkdir()
    elif damage == "old_workspace":
        (root / ".training-sample-workspace.abcdefgh").mkdir()
    elif damage in {"staging_name", "staging_case", "staging_extra"}:
        suffix = "keep" if damage == "staging_name" else "ABCDEFGH" if damage == "staging_case" else "ccccdddd"
        staging = root / (".bundle-staging." + suffix)
        staging.mkdir()
        (staging / "operator.txt").write_text("not disposable", encoding="utf-8")
    elif damage == "staging_link":
        (root / ".bundle-staging.ccccdddd").symlink_to(owned, target_is_directory=True)
    elif damage == "staging_file_link":
        (owned / "metadata.json").symlink_to(root / "tomorrow/metadata.json")
    elif damage == "head_link":
        head = root / "tomorrow"
        head.rename(tmp_path / "outside-head")
        head.symlink_to(tmp_path / "outside-head", target_is_directory=True)
    elif damage == "head_file_link":
        head_file = root / "tomorrow/model.json"
        head_file.rename(tmp_path / "outside-model")
        head_file.symlink_to(tmp_path / "outside-model")
    else:
        (root / "tomorrow/operator.txt").write_text("not a model", encoding="utf-8")
    before = _snapshot(tmp_path)

    with pytest.raises(ValueError):
        if action == "cleanup":
            ClearProfileStaging(root)
        elif action == "publish":
            PublishProfileBundles(root, V3_TRAINING_PROFILE, _staged(tmp_path))
        else:
            RecoverProfileBundles(root, V3_TRAINING_PROFILE)

    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("action", ("cleanup", "publish", "recover"))
def test_profile_root_link_is_rejected_without_touching_its_target(tmp_path, action):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    outside = tmp_path / "outside"
    root.rename(outside)
    root.symlink_to(outside, target_is_directory=True)
    before = _snapshot(tmp_path)
    with pytest.raises(ValueError):
        if action == "cleanup":
            ClearProfileStaging(root)
        elif action == "publish":
            PublishProfileBundles(root, V3_TRAINING_PROFILE, _staged(tmp_path))
        else:
            RecoverProfileBundles(root, V3_TRAINING_PROFILE)
    assert _snapshot(tmp_path) == before


def test_publisher_rechecks_the_old_tree_after_copy_before_any_directory_switch(tmp_path, monkeypatch):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    copy = profile_publication.shutil.copyfile
    observed = []

    def add_unowned_file(source, destination):
        if not observed:
            (root / "operator.txt").write_text("keep this", encoding="utf-8")
            observed.append(_snapshot(root))
        return copy(source, destination)

    monkeypatch.setattr(profile_publication.shutil, "copyfile", add_unowned_file)
    with pytest.raises(ValueError):
        PublishProfileBundles(root, V3_TRAINING_PROFILE, _staged(tmp_path))
    assert _snapshot(root) == observed[0]
    assert not tuple(tmp_path.glob(".v3.previous.*"))
    assert not _publication_candidates(tmp_path)


def test_partial_owned_publication_is_cleaned_after_copy_failure_and_old_pair_is_unchanged(tmp_path, monkeypatch):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    before = _snapshot(root)
    copy = profile_publication.shutil.copyfile

    def fail_metadata(source, destination):
        if Path(source).name == "metadata.json":
            raise OSError("controlled copy failure")
        return copy(source, destination)

    monkeypatch.setattr(profile_publication.shutil, "copyfile", fail_metadata)
    with pytest.raises(OSError, match="controlled copy failure"):
        PublishProfileBundles(root, V3_TRAINING_PROFILE, _staged(tmp_path))
    assert _snapshot(root) == before
    assert not _publication_candidates(tmp_path)


def test_copy_failure_preserves_a_new_candidate_if_unknown_content_appears(tmp_path, monkeypatch):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    before = _snapshot(root)
    copy = profile_publication.shutil.copyfile
    candidates = []

    def interrupt_copy(source, destination):
        copy(source, destination)
        candidate = Path(destination).parent.parent
        (candidate / "operator.txt").write_text("keep this candidate", encoding="utf-8")
        candidates.append((candidate, _snapshot(candidate)))
        raise OSError("controlled copy failure")

    monkeypatch.setattr(profile_publication.shutil, "copyfile", interrupt_copy)
    with pytest.raises(ValueError, match="recovery head is invalid"):
        PublishProfileBundles(root, V3_TRAINING_PROFILE, _staged(tmp_path))

    assert _snapshot(root) == before
    assert len(candidates) == 1
    candidate, contents = candidates[0]
    assert _snapshot(candidate) == contents
    assert _publication_candidates(tmp_path) == (candidate,)


def test_publisher_keeps_a_complete_valid_old_pair_with_managed_staging_replaceable(tmp_path):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    staging = root / ".bundle-staging.abcdefgh"
    staging.mkdir()
    (staging / "model.json").write_text("owned partial staging", encoding="utf-8")
    before = tuple(item.model_hash for item in InspectPublishedProfileBundles(root, V3_TRAINING_PROFILE))

    published = PublishProfileBundles(root, V3_TRAINING_PROFILE, _staged(tmp_path))

    assert tuple(item.model_hash for item in published) == before
    assert {path.name for path in root.iterdir()} == {"tomorrow", "d25"}
    assert not tuple(tmp_path.glob(".v3.previous.*"))


def test_unknown_content_in_previous_directory_preserves_it_without_failing_the_committed_pair(tmp_path, monkeypatch, caplog):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    expected = InspectPublishedProfileBundles(root, V3_TRAINING_PROFILE)
    rename = profile_publication.os.replace
    previous_contents = []

    def add_operator_file_after_commit(source, destination):
        rename(source, destination)
        if destination == root:
            previous, = tmp_path.glob(".v3.previous.*")
            (previous / "operator.txt").write_text("preserve operator content", encoding="utf-8")
            previous_contents.append((previous, _snapshot(previous)))

    monkeypatch.setattr(profile_publication.os, "replace", add_operator_file_after_commit)

    published = PublishProfileBundles(root, V3_TRAINING_PROFILE, _staged(tmp_path))

    assert published == expected
    assert InspectPublishedProfileBundles(root, V3_TRAINING_PROFILE) == published
    assert len(previous_contents) == 1
    previous, contents = previous_contents[0]
    assert _snapshot(previous) == contents
    assert "trained_profile_previous_cleanup_failed" in caplog.text
    assert not _publication_candidates(tmp_path)

    formal_contents = _snapshot(root)
    with pytest.raises(ValueError, match="unresolved previous directory"):
        PublishProfileBundles(root, V3_TRAINING_PROFILE, _staged(tmp_path))

    assert _snapshot(root) == formal_contents
    assert _snapshot(previous) == contents
    assert tuple(tmp_path.glob(".v3.previous.*")) == (previous,)
    assert not _publication_candidates(tmp_path)


def test_previous_directory_appearing_after_recovery_blocks_the_final_swap(tmp_path, monkeypatch):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    expected = _snapshot(root)
    recover = profile_publication.RecoverProfileBundles
    previous = tmp_path / (".v3.previous." + "a" * 32)

    def leave_previous(profile_root, profile, *, timeout_seconds):
        recover(profile_root, profile, timeout_seconds=timeout_seconds)
        previous.mkdir()

    monkeypatch.setattr(profile_publication, "RecoverProfileBundles", leave_previous)
    with pytest.raises(ValueError, match="unresolved previous directory"):
        PublishProfileBundles(root, V3_TRAINING_PROFILE, _staged(tmp_path))

    assert _snapshot(root) == expected
    assert tuple(tmp_path.glob(".v3.previous.*")) == (previous,)
    assert not tuple(previous.iterdir())
    assert not _publication_candidates(tmp_path)


@pytest.mark.parametrize("missing", ("file", "head", "both_heads"))
def test_owned_incomplete_models_can_still_be_cleaned_and_replaced_without_promoting_them(tmp_path, missing):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    if missing == "file":
        (root / "d25/metadata.json").unlink()
    else:
        heads = ("d25",) if missing == "head" else ("tomorrow", "d25")
        for name in heads:
            head = root / name
            for path in head.iterdir():
                path.unlink()
            head.rmdir()
    staging = root / ".bundle-staging.abcdefgh"
    staging.mkdir()
    (staging / "metadata.json").write_text("owned partial staging", encoding="utf-8")

    RecoverProfileBundles(root, V3_TRAINING_PROFILE)
    ClearProfileStaging(root)
    assert not staging.exists()
    published = PublishProfileBundles(root, V3_TRAINING_PROFILE, _staged(tmp_path))

    assert len(published) == 2
    assert {path.name for path in root.iterdir()} == {"tomorrow", "d25"}
    assert not tuple(tmp_path.glob(".v3.previous.*"))
