"""Scoring profile names are allowed only under their actual code and test owners."""

from pathlib import Path

from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS

_PROFILE_DIRECTORY_OWNERS = frozenset(
    {
        "src/trader/recommendation/infra/scoring/profiles",
        "src/trader/recommendation/application/pipeline/local_score",
        "src/trader/recommendation/domain/market",
        "src/trader/training/infra/profile",
        "src/trader/training/domain",
        "src/trader/training/entrypoints",
        "tests/training/unit/profiles",
        "tests/helpers/training/profiles",
        "tests/recommendation/unit/scoring",
        "tests/recommendation/unit/runtime",
    }
)


def IsScoringProfilePath(relative: Path) -> bool:
    for index, part in enumerate(relative.parts):
        if part in SCORING_PROFILE_IDS and Path(*relative.parts[:index]).as_posix() in _PROFILE_DIRECTORY_OWNERS:
            return True
    if relative.parts[0] == "tests":
        if relative.parent.as_posix() == "tests/helpers/recommendation/selection":
            return any(relative.name.startswith(f"{profile}_") for profile in SCORING_PROFILE_IDS)
        return any(relative.name.startswith(f"test_{profile}_") for profile in SCORING_PROFILE_IDS)
    if relative.parent.as_posix() in {"scripts/training/models", "scripts/training/samples"}:
        return any(relative.name.startswith(f"{profile}_") for profile in SCORING_PROFILE_IDS)
    return False
