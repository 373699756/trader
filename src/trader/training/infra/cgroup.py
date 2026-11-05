"""Resolve the training process's cgroup controller scopes and mounted parents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class TrainingCgroupScope:
    unified: bool
    directories: tuple[Path, ...]


def ReadTrainingCgroupScopes(controller: Literal["cpu", "memory"]) -> tuple[TrainingCgroupScope, ...]:
    """Return visible controller ancestors; reject an unmappable membership."""
    memberships = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    mounts = Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines()
    scopes: list[TrainingCgroupScope] = []
    for line in memberships:
        identity, controllers, member = line.split(":", 2)
        unified = identity == "0" and not controllers
        if not unified and controller not in controllers.split(","):
            continue
        membership = Path(member)
        if not membership.is_absolute() or ".." in membership.parts:
            raise ValueError("training_cgroup_membership_invalid")
        mapped = False
        for mount in mounts:
            before, after = mount.split(" - ", 1)
            fields, filesystem = before.split(), after.split()
            if filesystem[0] != ("cgroup2" if unified else "cgroup"):
                continue
            if not unified and controller not in filesystem[2].split(","):
                continue
            root, mountpoint = (_mount_path(fields[index]) for index in (3, 4))
            if not membership.is_relative_to(root):
                continue
            directory = mountpoint / membership.relative_to(root)
            parents = [directory]
            while directory != mountpoint:
                directory = directory.parent
                parents.append(directory)
            scope = TrainingCgroupScope(unified, tuple(parents))
            if scope not in scopes:
                scopes.append(scope)
            mapped = True
        if not mapped:
            raise ValueError("training_cgroup_scope_unmappable")
    return tuple(scopes)


def _mount_path(value: str) -> Path:
    path = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value))
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("training_cgroup_mount_invalid")
    return path


__all__ = [
    "ReadTrainingCgroupScopes",
    "TrainingCgroupScope",
]
