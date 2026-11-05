"""Paths owned by the tool checkout, independent of the selected product."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def ExternalPath(value: Path | None, option: str) -> Path | None:
    if value is None:
        return None
    if not value.expanduser().is_absolute():
        raise ValueError(f"{option} must be an absolute path outside the repository")
    resolved = value.expanduser().resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise ValueError(f"{option} must be an absolute path outside the repository")
    return resolved
