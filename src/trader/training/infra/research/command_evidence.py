"""Read and publish the existing immutable research evidence formats."""

from trader.infra.sqlite_policy import SqliteIoPolicy

import json
import re
from pathlib import Path
from trader.infra.regular_file import ReadRegularFile

from trader.infra.settings.parser import RequireExactKeys, RequireMapping, Text
from trader.training.application.terminal_holdout_execution import TerminalHoldoutAlreadyOpenedError
from trader.training.domain.evaluation.research_identity import CanonicalResearchHash

from trader.training.application.capability_completion import CapabilityPublication
from trader.training.application.cross_strategy_conclusion import CrossStrategyConclusion
from trader.training.application.h1_point_in_time_completion import H1ResearchCompletion
from trader.training.domain.evaluation.h1_point_in_time import H1CapabilityAuditReport
from trader.training.domain.evaluation.historical_industry_facts import HistoricalIndustryDatasetReport
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutParentState
from trader.training.infra.research.registration_codec import DecodeCandidateRegistration
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.training.infra.research.cross_strategy_conclusion_reports import CrossStrategyConclusionReportIndex
from trader.training.infra.research.d25_terminal_holdout_reports import D25TerminalHoldoutReportIndex
from trader.training.infra.research.h1_point_in_time_capability import H1CapabilityEvidenceIndex
from trader.training.infra.research.h1_point_in_time_completion import H1ResearchCompletionEvidenceFiles
from trader.training.infra.research.historical_industry_evidence import AuditPublishedHistoricalIndustryFacts
from trader.training.infra.research.historical_label_evidence import HistoricalLabelEvidenceIndex
from trader.training.infra.research.point_in_time_dataset_evidence import DecodePointInTimeDateSplit
from trader.training.infra.research.tomorrow_point_in_time_holdout_reports import (
    TomorrowPointInTimeHoldoutReportIndex,
)


class HistoricalIndustryEvidenceReader:
    def __init__(self, root: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        self._root = root

    def audit(self, *, required_sample_codes: int, tushare_access_points: int) -> HistoricalIndustryDatasetReport:
        return AuditPublishedHistoricalIndustryFacts(
            self._root, required_sample_codes=required_sample_codes, tushare_access_points=tushare_access_points, sqlite_policy=self._sqlite_policy
        )


class CapabilityEvidencePublisher:
    def __init__(self, root: Path) -> None:
        self._root = root

    def publish(self, capability: H1CapabilityAuditReport, completion: H1ResearchCompletion) -> CapabilityPublication:
        H1CapabilityEvidenceIndex(self._root).write(capability)
        HistoricalLabelEvidenceIndex(self._root).write(completion.labels)
        index = H1ResearchCompletionEvidenceFiles(self._root).write(completion)
        return CapabilityPublication(
            index.content_hash, index.residual_terminal_hashes, index.daily_close_selection_hash
        )


class TerminalHoldoutParentsReader:
    def __init__(self, root: Path, opening_path: Path) -> None:
        self._root = root
        self._opening_path = opening_path

    def read(self) -> tuple[TerminalHoldoutParentState, TerminalHoldoutParentState]:
        if self._opening_path.exists() or self._opening_path.is_symlink():
            raise TerminalHoldoutAlreadyOpenedError("terminal_holdout_already_opened")
        path = self._root / "terminal_parents.json"
        encoded = ReadRegularFile(path, maximum_bytes=256 * 1024)
        raw: object = json.loads(encoded)
        if not isinstance(raw, dict):
            raise ValueError("terminal parent metadata must be an object")
        RequireExactKeys(raw, {"tomorrow", "d25", "content_hash"}, "terminal_parents")
        identity = raw.pop("content_hash")
        if not isinstance(identity, str) or CanonicalResearchHash(raw) != identity:
            raise ValueError("terminal parent metadata digest mismatch")
        states = []
        for strategy in ("tomorrow", "d25"):
            item = RequireMapping(raw, strategy)
            RequireExactKeys(
                item, {"candidate_status", "parent_hash", "candidate_hash", "failure_reasons", "date_split", "profile_id", "registration"},
                "terminal_parent",
            )
            status = Text(item, "candidate_status")
            reasons = item["failure_reasons"]
            if (
                status not in {"historical_candidate_ready", "historical_daily_close_proxy_validated",
                               "historical_data_insufficient", "historical_rejected"}
                or not isinstance(reasons, list) or len(reasons) > 64
                or any(not isinstance(value, str) or re.fullmatch(r"[a-z0-9_]{1,96}", value) is None for value in reasons)
                or (status in {"historical_candidate_ready", "historical_daily_close_proxy_validated"}) == bool(reasons)
            ):
                raise ValueError("terminal parent prerequisite state is invalid")
            parent_hash, candidate_hash = Text(item, "parent_hash"), Text(item, "candidate_hash")
            if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (parent_hash, candidate_hash)):
                raise ValueError("terminal parent identities are invalid")
            split = (
                None if item["date_split"] is None
                else DecodePointInTimeDateSplit(dict(RequireMapping(item, "date_split")))
            )
            if status in {"historical_candidate_ready", "historical_daily_close_proxy_validated"} and split is None:
                raise ValueError("ready terminal parent requires its sealed date split")
            profile = item["profile_id"]
            if profile is not None and profile not in SCORING_PROFILE_IDS:
                raise ValueError("terminal_parent_profile_invalid")
            registration = None if item["registration"] is None else DecodeCandidateRegistration(RequireMapping(item, "registration"))
            if status in {"historical_candidate_ready", "historical_daily_close_proxy_validated"} and (
                profile is None or registration is None or registration.profile_id != profile or registration.strategy != strategy
            ):
                raise ValueError("ready terminal parent requires its profile registration")
            states.append(TerminalHoldoutParentState(
                status, parent_hash, candidate_hash, tuple(reasons), date_split=split,
                profile_id=profile, registration=registration,
            ))
        if states[0].profile_id != states[1].profile_id:
            raise ValueError("terminal_strategy_profile_pair_mismatch")
        return states[0], states[1]


class TerminalConclusionPublisher:
    def __init__(self, root: Path) -> None:
        self._root = root

    def publish(self, conclusion: CrossStrategyConclusion) -> None:
        TomorrowPointInTimeHoldoutReportIndex(self._root / "tomorrow").write(conclusion.tomorrow)
        D25TerminalHoldoutReportIndex(self._root / "d25").write(conclusion.d25)
        CrossStrategyConclusionReportIndex(self._root / "cross_strategy").write(conclusion)
