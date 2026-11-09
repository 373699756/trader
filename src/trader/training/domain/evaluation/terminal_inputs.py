"""Pure identities and pre-read admission for terminal outcome partitions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.training.domain.evaluation.research_plan import ResearchExecutionPlan
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutParentState, TerminalStrategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.training.domain.evaluation.research_registration import ResearchNotReadyError, ResearchStateRegistration


@dataclass(frozen=True)
class TerminalHoldoutPartition:
    strategy: TerminalStrategy
    parent_hash: str
    candidate_hash: str
    trade_dates: tuple[date, ...]
    rows_sha256: str
    row_count: int
    profile_id: ScoringProfileId
    registration_hash: str
    state_registration: ResearchStateRegistration | None

    def __post_init__(self) -> None:
        if (
            self.strategy not in {"tomorrow", "d25"}
            or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (self.parent_hash, self.candidate_hash, self.rows_sha256))
            or not self.trade_dates or self.trade_dates != tuple(sorted(set(self.trade_dates)))
            or len(self.trade_dates) > 20_000 or type(self.row_count) is not int
            or not len(self.trade_dates) <= self.row_count <= 2_000_000
            or self.profile_id not in SCORING_PROFILE_IDS or re.fullmatch(r"[0-9a-f]{64}", self.registration_hash) is None
        ):
            raise ValueError("terminal partition metadata is invalid")


@dataclass(frozen=True)
class TerminalHoldoutEvidence:
    research_plan_hash: str
    tomorrow: TerminalHoldoutPartition
    d25: TerminalHoldoutPartition

    @property
    def content_hash(self) -> str:
        return CanonicalDigest(self)

    def validate(
        self,
        plan: ResearchExecutionPlan,
        parents: tuple[TerminalHoldoutParentState, TerminalHoldoutParentState],
    ) -> None:
        if self.research_plan_hash != plan.content_hash or (self.tomorrow.strategy, self.d25.strategy) != ("tomorrow", "d25"):
            raise ValueError("terminal evidence does not match the configured research plan")
        profile_id = parents[0].profile_id
        if parents[1].profile_id != profile_id or any(
            partition.profile_id != profile_id for partition in (self.tomorrow, self.d25)
        ):
            raise ValueError("terminal_strategy_profile_pair_mismatch")
        for partition, parent in zip((self.tomorrow, self.d25), parents, strict=True):
            if parent.ready:
                plan.validate_terminal_parent(partition.strategy, parent)
                if parent.registration is None or partition.registration_hash != parent.registration.content_hash:
                    raise ValueError("terminal_profile_or_registration_mismatch")
                state = partition.state_registration
                if state is None:
                    raise ResearchNotReadyError("terminal_state_registration_missing")
                if (
                    state.qualification_hash != parent.registration.qualification_hash
                    or tuple(item.trade_date for item in state.days) != partition.trade_dates
                ):
                    raise ValueError("terminal_state_qualification_or_dates_mismatch")
                if parent.date_split is None or partition.trade_dates != parent.date_split.terminal_holdout_dates:
                    raise ValueError("terminal dates differ from the sealed unseen parent partition")
            if (
                partition.parent_hash != parent.parent_hash or partition.candidate_hash != parent.candidate_hash
                or len(partition.trade_dates) < plan.settings.terminal_holdout_sessions
            ):
                raise ValueError("terminal evidence does not match its sealed parents or date budget")


@dataclass(frozen=True)
class TerminalInputFootprint:
    row_count: int
    trade_dates: int
    file_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.row_count) is not int or not 1 <= self.row_count <= 2_000_000
            or type(self.trade_dates) is not int or not 1 <= self.trade_dates <= 20_000
            or type(self.file_bytes) is not int or not 0 < self.file_bytes <= min(4 * 1024**3, self.row_count * 16_384)
        ):
            raise ValueError("terminal_input_footprint_invalid")


def EstimateTerminalWorkingSet(footprints: tuple[TerminalInputFootprint, ...]) -> int:
    if not footprints or len(footprints) > 2:
        raise ValueError("terminal_input_footprints_invalid")
    # Codecs bound industry to 256 Unicode characters and the remaining strings
    # to enums/codes. 8KiB/row includes objects, float vectors and retained
    # strings, plus sort/group/index references. JSON is decoded one line at a
    # time; bounded metadata and the bootstrap's lists get separate headroom.
    return (
        128 * 1024**2
        + sum(item.row_count * 8192 + item.trade_dates * 4096 for item in footprints)
        + 16_384 * 8 + 10_000 * 3 * 64
    )
