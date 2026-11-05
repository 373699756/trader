"""Reject company replay outputs that overlap protected runtime locations."""

from collections.abc import Sequence
from pathlib import Path


def RequireIsolatedReplay(directory: Path, protected_roots: Sequence[Path]) -> None:
    target = directory.expanduser().resolve()
    for root in protected_roots:
        source = root.expanduser().resolve()
        if target == source or target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError("company_replay_overlaps_project_or_current_data")
