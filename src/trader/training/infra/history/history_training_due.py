"""Resolve the training cadence from the active history and bundle identities."""

from __future__ import annotations

import calendar as month_calendar
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from trader.download.domain.history_control import (
    CalculateHistoryTrainingCacheInvalidationDates,
    CalculateHistoryTrainingDue,
    HistoryActiveSnapshot,
    HistoryTrainingDueRequest,
    HistoryTrainingDueState,
)
from trader.download.infra.history_control_index import (
    HistoryControlError,
    HistoryControlIndex,
)
from trader.download.infra.history_snapshot_reader import (
    HistoryPartitionRevisionComparison,
    HistorySnapshotReader,
    HistorySnapshotReadError,
    RouteHistoryMonths,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.evaluation.research_identity import CanonicalResearchHash
from trader.training.domain.model_publication import HeadTrainingStatus, ModelPublicationIdentity
from trader.training.infra.model_bundles.bundle_codec import ModelBundleContractError, ModelBundleIntegrityError
from trader.training.infra.model_bundles.bundle_snapshot import (
    ActiveHeadBundle,
)
from trader.training.infra.model_bundles.contracts import TrainedProfileContract
from trader.training.infra.model_bundles.profile_publication import (
    InspectActiveProfileBundles,
    ProfilePublicationBusyError,
)
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE


@dataclass(frozen=True)
class HistoryTrainingDueEvaluation:
    archive_root: Path
    active_snapshot: HistoryActiveSnapshot
    state: HistoryTrainingDueState
    bundle: ActiveHeadBundle | None
    revised_dates: tuple[date, ...]
    invalidated_cache_dates: tuple[date, ...]


@dataclass(frozen=True)
class HistoryTrainingDueQuery:
    archive_root: Path
    training_root: Path
    observed_at: datetime
    profile: TrainedProfileContract
    strategy: Strategy = Strategy.TOMORROW
    expected_training_contract_hash: str | None = None
    persist: bool = True
    inspected: tuple[ActiveHeadBundle, ...] | None = None
    inspected_invalid: bool = False


def EvaluateHistoryTrainingDue(query: HistoryTrainingDueQuery) -> HistoryTrainingDueEvaluation | None:
    """Read and persist one immutable due observation.

    A missing active snapshot is intentionally returned as ``None``: callers
    project that condition as ``history_manifest_unavailable`` instead of
    fabricating a cadence baseline.
    """

    archive_root = query.archive_root
    profile = query.profile
    strategy = query.strategy
    expected_training_contract_hash = query.expected_training_contract_hash
    control = HistoryControlIndex(archive_root / "catalog.sqlite3")
    try:
        state = control.load_state()
    except HistoryControlError:
        return None
    active = state.active_snapshot
    if active is None:
        return None
    calendar = next((item for item in state.calendars if item.content_hash == active.calendar_hash), None)
    if calendar is None:
        return None

    bundle, bundle_invalid = (
        (next((item for item in query.inspected if item.strategy is strategy), None), query.inspected_invalid)
        if query.inspected is not None
        else _active_bundle(query.training_root, profile, strategy)
    )
    current_label_cutoff = _mature_label_cutoff(calendar.open_dates, active.data_cutoff, profile, strategy)
    if bundle_invalid:
        due_identity = _due_identity(query, active.content_hash, None, invalid=True)
        due = CalculateHistoryTrainingDue(
            HistoryTrainingDueRequest(
                due_identity=due_identity,
                baseline_label_cutoff=None,
                current_label_cutoff=current_label_cutoff,
                calendar_dates=calendar.open_dates,
                input_revision=False,
                observed_at=query.observed_at,
                data_complete=current_label_cutoff is not None,
                training_contract_changed=True,
            )
        )
        if query.persist:
            due = _persist_due_observation(control, due)
        return HistoryTrainingDueEvaluation(archive_root, active, due, None, (), ())
    baseline_cutoff = bundle.label_cutoff if bundle is not None else None
    baseline = (
        next((item for item in state.snapshots if item.content_hash == bundle.training_input_hash), None)
        if bundle is not None
        else None
    )
    data_complete = bundle is None or baseline is not None
    revised_dates: tuple[date, ...] = ()
    if bundle is not None and baseline is not None and baseline.content_hash != active.content_hash:
        try:
            revised_dates = _revised_dates_since_bundle(
                archive_root,
                baseline,
                active,
                calendar.open_dates,
                bundle.label_cutoff,
            )
        except (HistorySnapshotReadError, OSError, ValueError):
            data_complete = False
    input_revision = bool(revised_dates)
    training_contract_changed = bundle is not None and (
        expected_training_contract_hash is not None and bundle.training_contract_hash != expected_training_contract_hash
    )
    invalidated_dates = CalculateHistoryTrainingCacheInvalidationDates(
        calendar.open_dates,
        revised_dates,
        dependency_sessions=profile.history_sessions - 1,
    )
    due_identity = _due_identity(query, active.content_hash, bundle.metadata_hash if bundle else None)
    due_identity += ":" + CanonicalResearchHash((data_complete, revised_dates, current_label_cutoff))[:16]
    due = CalculateHistoryTrainingDue(
        HistoryTrainingDueRequest(
            due_identity=due_identity,
            baseline_label_cutoff=baseline_cutoff,
            current_label_cutoff=current_label_cutoff,
            calendar_dates=calendar.open_dates,
            input_revision=input_revision,
            observed_at=query.observed_at,
            data_complete=data_complete,
            training_contract_changed=training_contract_changed,
        )
    )
    previous = next((item for item in state.due_states if item.due_identity == due.due_identity), None)
    if previous is None and query.persist:
        control.save_due_state(due)
    elif previous is not None:
        due = previous
    return HistoryTrainingDueEvaluation(
        archive_root,
        active,
        due,
        bundle,
        revised_dates,
        invalidated_dates,
    )


def _persist_due_observation(control: HistoryControlIndex, due: HistoryTrainingDueState) -> HistoryTrainingDueState:
    previous = control.read_due_state(due.due_identity)
    if previous is not None:
        return previous
    control.save_due_state(due)
    return due


def _due_identity(
    query: HistoryTrainingDueQuery, history_hash: str, metadata_hash: str | None, *, invalid: bool = False
) -> str:
    return (
        "due-"
        + CanonicalResearchHash(
            (
                query.profile.profile_id,
                query.strategy.value,
                history_hash,
                metadata_hash,
                query.expected_training_contract_hash,
                invalid,
            )
        )[:32]
    )


def _revised_dates_since_bundle(
    archive_root: Path,
    baseline: HistoryActiveSnapshot,
    active: HistoryActiveSnapshot,
    calendar_dates: tuple[date, ...],
    baseline_label_cutoff: date,
) -> tuple[date, ...]:
    start = calendar_dates[0]
    end = min(baseline_label_cutoff, baseline.data_cutoff, active.data_cutoff)
    if start > end:
        return ()
    baseline_months = {(item.calendar_year, item.calendar_month): item for item in baseline.partitions}
    active_months = {(item.calendar_year, item.calendar_month): item for item in active.partitions}
    archive = HistorySnapshotReader(archive_root)
    revisions: set[date] = set()
    for year, month in RouteHistoryMonths(start, end):
        if (year, month) not in baseline_months or (year, month) not in active_months:
            raise HistorySnapshotReadError("history snapshot comparison month is missing")
        month_start = max(start, date(year, month, 1))
        month_end = min(end, date(year, month, month_calendar.monthrange(year, month)[1]))
        baseline_reference = baseline_months[(year, month)]
        physical_reference = active_months[(year, month)]
        if month_start > month_end or baseline_reference == physical_reference:
            continue
        revisions.update(
            archive.revised_dates(
                month_start,
                month_end,
                HistoryPartitionRevisionComparison(
                    physical_reference,
                    baseline.sequence,
                    active.sequence,
                ),
            )
        )
    return tuple(sorted(revisions))


def _active_bundle(
    training_root: Path,
    profile: TrainedProfileContract,
    strategy: Strategy,
) -> tuple[ActiveHeadBundle | None, bool]:
    try:
        return next(
            item for item in InspectActiveProfileBundles(training_root, profile) if item.strategy is strategy
        ), False
    except FileNotFoundError:
        return None, False
    except (OSError, ProfilePublicationBusyError, TypeError, ValueError):
        return None, True


def _mature_label_cutoff(
    calendar_dates: tuple[date, ...],
    data_cutoff: date,
    profile: TrainedProfileContract,
    strategy: Strategy,
) -> date | None:
    contract = profile.head_for_strategy(strategy)
    positions = {day: position for position, day in enumerate(calendar_dates)}
    position = positions.get(data_cutoff)
    if position is None or position < contract.maturity_sessions:
        return None
    return calendar_dates[position - contract.maturity_sessions]


def EvaluateAllHistoryTrainingDue(
    history_root: Path, training_root: Path, observed_at: datetime
) -> tuple[HeadTrainingStatus, ...]:
    results: list[HeadTrainingStatus] = []
    for profile in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE):
        heads, failure = _inspect_profile(training_root, profile)
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            bundle = next((item for item in heads if item.strategy is strategy), None)
            due = (
                EvaluateHistoryTrainingDue(
                    HistoryTrainingDueQuery(
                        history_root,
                        training_root / profile.output_directory,
                        observed_at,
                        profile,
                        strategy,
                        inspected=heads,
                        inspected_invalid=failure not in (None, "missing"),
                    )
                )
                if failure != "publication_busy"
                else None
            )
            results.append(
                HeadTrainingStatus(
                    profile.profile_id,
                    strategy,
                    _publication_identity(profile, bundle) if bundle else None,
                    due.state if due else None,
                    failure,
                )
            )
    return tuple(results)


class PublishedTrainingState:
    """Memory projection of the existing due owner; observers never calculate or persist due state."""

    def __init__(
        self,
        history_root: Path,
        training_root: Path,
        loaded: tuple[ModelPublicationIdentity, ...],
        *,
        owned_profile: ScoringProfileId,
        wall_clock: Callable[[], datetime],
    ) -> None:
        self._history_root = history_root
        self._training_root = training_root
        self._loaded = loaded
        self._owned_profile = owned_profile
        self._clock = wall_clock
        self._lock = threading.Lock()
        self._status: tuple[HeadTrainingStatus, ...] = ()

    def observe_publication(self) -> None:
        control = HistoryControlIndex(self._history_root / "catalog.sqlite3")
        try:
            history = control.load_published_state()
        except HistoryControlError:
            history = None
        results: list[HeadTrainingStatus] = []
        for profile in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE):
            heads, failure = _inspect_profile(self._training_root, profile)
            for strategy in (Strategy.TOMORROW, Strategy.D25):
                bundle = next((item for item in heads if item.strategy is strategy), None)
                due = None
                if history is not None and failure != "publication_busy":
                    query = HistoryTrainingDueQuery(
                        self._history_root,
                        self._training_root / profile.output_directory,
                        self._clock(),
                        profile,
                        strategy,
                        persist=False,
                    )
                    identity = _due_identity(
                        query,
                        history.snapshot.content_hash,
                        bundle.metadata_hash if bundle else None,
                        invalid=failure not in (None, "missing"),
                    )
                    try:
                        due = control.read_due_state(identity, latest=True)
                    except HistoryControlError:
                        due = None
                loaded = next(
                    (
                        item
                        for item in self._loaded
                        if item.profile_id == profile.profile_id and item.strategy is strategy
                    ),
                    None,
                )
                disk = _publication_identity(profile, bundle) if bundle else None
                results.append(
                    HeadTrainingStatus(
                        profile.profile_id,
                        strategy,
                        disk,
                        due,
                        failure or ("training_due_unavailable" if due is None else None),
                        loaded,
                        profile.profile_id == self._owned_profile,
                    )
                )
        with self._lock:
            # A busy publisher provides no evidence that the prior disk identity disappeared.
            previous = {(item.profile_id, item.strategy): item for item in self._status}
            self._status = tuple(
                replace(previous[(item.profile_id, item.strategy)], failure=item.failure)
                if item.failure == "publication_busy" and (item.profile_id, item.strategy) in previous
                else item
                for item in results
            )

    def status(self) -> tuple[HeadTrainingStatus, ...]:
        with self._lock:
            return self._status


def _publication_identity(profile: TrainedProfileContract, bundle: ActiveHeadBundle) -> ModelPublicationIdentity:
    return ModelPublicationIdentity(
        profile.profile_id,
        bundle.strategy,
        bundle.model_hash,
        bundle.metadata_hash,
        bundle.label_cutoff,
        bundle.training_input_hash,
        bundle.training_run_hash,
    )


def _inspect_profile(
    training_root: Path, profile: TrainedProfileContract
) -> tuple[tuple[ActiveHeadBundle, ...], str | None]:
    try:
        return InspectActiveProfileBundles(training_root / profile.output_directory, profile), None
    except FileNotFoundError:
        return (), "missing"
    except ProfilePublicationBusyError:
        return (), "publication_busy"
    except ModelBundleIntegrityError:
        return (), "integrity_conflict"
    except ModelBundleContractError:
        return (), "contract_conflict"
    except (OSError, TypeError, ValueError):
        return (), "corrupt"


__all__ = [
    "HistoryTrainingDueEvaluation",
    "HistoryTrainingDueQuery",
    "EvaluateHistoryTrainingDue",
    "EvaluateAllHistoryTrainingDue",
    "PublishedTrainingState",
]
