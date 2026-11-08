"""Resolve the training cadence from the published history and bundle identities."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import calendar as month_calendar
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from trader.download.domain.history_control import HistoryPublishedControlState, HistorySnapshot
from trader.download.domain.current_industry import CurrentIndustrySnapshot
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
from trader.training.domain.training_input_identity import BuildTrainingInputHash
from trader.training.infra.history.history_training_expectation import HistoryTrainingExpectation
from trader.training.domain.model_publication import HeadTrainingStatus, ModelPublicationIdentity
from trader.training.domain.training_due import (
    CalculateTrainingCacheInvalidationDates,
    CalculateTrainingDue,
    TrainingDueRequest,
    TrainingDueState,
)
from trader.training.infra.history.training_maintenance_index import TrainingMaintenanceError, TrainingMaintenanceIndex
from trader.training.infra.model_bundles.bundle_codec import ModelBundleContractError, ModelBundleIntegrityError
from trader.training.infra.model_bundles.bundle_snapshot import (
    PublishedHeadBundle,
)
from trader.training.infra.model_bundles.contracts import TrainedProfileContract
from trader.training.infra.model_bundles.profile_publication import (
    InspectPublishedProfileBundles,
    ProfilePublicationBusyError,
    ReadPublishedProfileIdentities,
)
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE


@dataclass(frozen=True)
class HistoryTrainingDueEvaluation:
    history_root: Path
    published_snapshot: HistorySnapshot
    state: TrainingDueState
    bundle: PublishedHeadBundle | None
    revised_dates: tuple[date, ...]
    invalidated_cache_dates: tuple[date, ...]


@dataclass(frozen=True)
class HistoryTrainingDueQuery:
    history_root: Path
    training_root: Path
    observed_at: datetime
    profile: TrainedProfileContract
    strategy: Strategy = Strategy.TOMORROW
    maintenance_path: Path = field(kw_only=True)
    industry_snapshot: CurrentIndustrySnapshot = field(kw_only=True)
    expected_training_contract_hash: str = field(kw_only=True)
    persist: bool = True
    inspected: tuple[PublishedHeadBundle, ...] | None = None
    inspected_invalid: bool = False

    def __post_init__(self) -> None:
        value = self.expected_training_contract_hash
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("training_maintenance_contract_hash_invalid")


def EvaluateHistoryTrainingDue(query: HistoryTrainingDueQuery, *, sqlite_policy: SqliteIoPolicy) -> HistoryTrainingDueEvaluation | None:
    """Read and persist one immutable due observation.

    A missing published snapshot is intentionally returned as ``None``: callers
    project that condition as ``history_manifest_unavailable`` instead of
    fabricating a cadence baseline.
    """

    history_root = query.history_root
    profile = query.profile
    strategy = query.strategy
    expected_training_contract_hash = query.expected_training_contract_hash
    control = HistoryControlIndex(history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy)
    maintenance = TrainingMaintenanceIndex(query.maintenance_path, sqlite_policy=sqlite_policy)
    try:
        state = control.load_state()
    except HistoryControlError:
        return None
    current_snapshot = state.published_snapshot
    if current_snapshot is None:
        return None
    calendar = next((item for item in state.calendars if item.content_hash == current_snapshot.calendar_hash), None)
    universe = next((item for item in state.universes if item.content_hash == current_snapshot.universe_hash), None)
    if calendar is None or universe is None:
        return None
    industry = query.industry_snapshot.project(tuple(item.code for item in universe.securities))
    training_input_hash = BuildTrainingInputHash(current_snapshot.content_hash, industry.content_hash)

    bundle, bundle_invalid = (
        (next((item for item in query.inspected if item.strategy is strategy), None), query.inspected_invalid)
        if query.inspected is not None
        else _current_bundle(query.training_root, profile, strategy)
    )
    current_label_cutoff = _mature_label_cutoff(calendar.open_dates, current_snapshot.data_cutoff, profile, strategy)
    if bundle_invalid:
        due_identity = _due_identity(query, training_input_hash, None, invalid=True)
        due = CalculateTrainingDue(
            TrainingDueRequest(
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
            due = _persist_due_observation(maintenance, due)
        return HistoryTrainingDueEvaluation(history_root, current_snapshot, due, None, (), ())
    baseline_cutoff = bundle.label_cutoff if bundle is not None else None
    baseline = (
        next((item for item in state.snapshots if item.content_hash == bundle.price_snapshot_hash), None)
        if bundle is not None
        else None
    )
    data_complete = bundle is None or baseline is not None
    revised_dates: tuple[date, ...] = ()
    if bundle is not None and baseline is not None and baseline.content_hash != current_snapshot.content_hash:
        try:
            revised_dates = _revised_dates_since_bundle(
                history_root,
                baseline,
                current_snapshot,
                calendar.open_dates,
                bundle.label_cutoff, sqlite_policy=sqlite_policy,
            )
        except (HistorySnapshotReadError, OSError, ValueError):
            data_complete = False
    industry_revision = False
    baseline_industry_hash = None
    if bundle is not None and baseline is not None:
        baseline_universe = next((item for item in state.universes if item.content_hash == baseline.universe_hash), None)
        if baseline_universe is None:
            data_complete = False
        else:
            baseline_industry = query.industry_snapshot.project(tuple(item.code for item in baseline_universe.securities))
            baseline_industry_hash = baseline_industry.content_hash
            industry_revision = (
                BuildTrainingInputHash(bundle.price_snapshot_hash, baseline_industry.content_hash) != bundle.training_input_hash
            )
    input_revision = bool(revised_dates) or industry_revision
    training_contract_changed = bundle is not None and bundle.training_contract_hash != expected_training_contract_hash
    invalidated_dates = CalculateTrainingCacheInvalidationDates(
        calendar.open_dates,
        revised_dates,
        dependency_sessions=(
            profile.history_sessions if profile.algorithm == "tcn" else profile.history_sessions - 1
        ),
    )
    due_identity = _due_identity(
        query, training_input_hash, bundle.metadata_hash if bundle else None,
        baseline_industry_hash=baseline_industry_hash,
    )
    due_identity += ":" + CanonicalResearchHash((data_complete, revised_dates, current_label_cutoff))[:16]
    due = CalculateTrainingDue(
        TrainingDueRequest(
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
    previous = maintenance.read_due_state(due.due_identity)
    if previous is None and query.persist:
        maintenance.save_due_state(due)
    elif previous is not None:
        due = previous
    return HistoryTrainingDueEvaluation(
        history_root,
        current_snapshot,
        due,
        bundle,
        revised_dates,
        invalidated_dates,
    )


def _persist_due_observation(control: TrainingMaintenanceIndex, due: TrainingDueState) -> TrainingDueState:
    previous = control.read_due_state(due.due_identity)
    if previous is not None:
        return previous
    control.save_due_state(due)
    return due


def _due_identity(
    query: HistoryTrainingDueQuery, training_input_hash: str, metadata_hash: str | None,
    *, invalid: bool = False, baseline_industry_hash: str | None = None,
) -> str:
    return (
        "due-"
        + CanonicalResearchHash(
            (
                query.profile.profile_id,
                query.strategy.value,
                training_input_hash,
                baseline_industry_hash,
                metadata_hash,
                query.expected_training_contract_hash,
                invalid,
            )
        )[:32]
    )


def _revised_dates_since_bundle(
    history_root: Path,
    baseline: HistorySnapshot,
    current_snapshot: HistorySnapshot,
    calendar_dates: tuple[date, ...],
    baseline_label_cutoff: date, *, sqlite_policy: SqliteIoPolicy,
) -> tuple[date, ...]:
    start = calendar_dates[0]
    end = min(baseline_label_cutoff, baseline.data_cutoff, current_snapshot.data_cutoff)
    if start > end:
        return ()
    baseline_months = {(item.calendar_year, item.calendar_month): item for item in baseline.partitions}
    current_months = {(item.calendar_year, item.calendar_month): item for item in current_snapshot.partitions}
    history = HistorySnapshotReader(history_root, sqlite_policy=sqlite_policy)
    revisions: set[date] = set()
    for year, month in RouteHistoryMonths(start, end):
        if (year, month) not in baseline_months or (year, month) not in current_months:
            raise HistorySnapshotReadError("history snapshot comparison month is missing")
        month_start = max(start, date(year, month, 1))
        month_end = min(end, date(year, month, month_calendar.monthrange(year, month)[1]))
        baseline_reference = baseline_months[(year, month)]
        physical_reference = current_months[(year, month)]
        if month_start > month_end or baseline_reference == physical_reference:
            continue
        revisions.update(
            history.revised_dates(
                month_start,
                month_end,
                HistoryPartitionRevisionComparison(
                    physical_reference,
                    baseline.sequence,
                    current_snapshot.sequence,
                ),
            )
        )
    return tuple(sorted(revisions))


def _current_bundle(
    training_root: Path,
    profile: TrainedProfileContract,
    strategy: Strategy,
) -> tuple[PublishedHeadBundle | None, bool]:
    try:
        return next(
            item for item in InspectPublishedProfileBundles(training_root, profile) if item.strategy is strategy
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


def _baseline_industry_hash(
    query: HistoryTrainingDueQuery,
    history: HistoryPublishedControlState,
    bundle: PublishedHeadBundle | None,
    control: HistoryControlIndex,
) -> str | None:
    if bundle is None:
        return None
    baseline_universe = history.universe if bundle.price_snapshot_hash == history.snapshot.content_hash else None
    if baseline_universe is None:
        try:
            state = control.load_state()
        except HistoryControlError:
            return None
        baseline = next((item for item in state.snapshots if item.content_hash == bundle.price_snapshot_hash), None)
        if baseline is not None:
            baseline_universe = next((item for item in state.universes if item.content_hash == baseline.universe_hash), None)
    if baseline_universe is None:
        return None
    return query.industry_snapshot.project(tuple(item.code for item in baseline_universe.securities)).content_hash


def _metadata_due(
    query: HistoryTrainingDueQuery,
    history: HistoryPublishedControlState,
    bundle: PublishedHeadBundle | None,
    *,
    baseline_industry_hash: str | None,
    invalid: bool,
) -> TrainingDueState | None:
    """Resolve known changes without comparing any price partition rows."""
    industry = query.industry_snapshot.project(tuple(item.code for item in history.universe.securities))
    same_price = bundle is None or bundle.price_snapshot_hash == history.snapshot.content_hash
    if bundle is not None and baseline_industry_hash is None:
        return None
    industry_revision = bundle is not None and baseline_industry_hash is not None and (
        BuildTrainingInputHash(bundle.price_snapshot_hash, baseline_industry_hash) != bundle.training_input_hash
    )
    contract_changed = bundle is not None and bundle.training_contract_hash != query.expected_training_contract_hash
    if not same_price and not industry_revision and not contract_changed:
        return None
    current_cutoff = _mature_label_cutoff(history.calendar.open_dates, history.snapshot.data_cutoff, query.profile, query.strategy)
    identity = _due_identity(
        query,
        BuildTrainingInputHash(history.snapshot.content_hash, industry.content_hash),
        bundle.metadata_hash if bundle else None,
        invalid=invalid,
        baseline_industry_hash=baseline_industry_hash,
    )
    if not invalid:
        identity += ":" + CanonicalResearchHash((True, (), current_cutoff))[:16]
    return CalculateTrainingDue(TrainingDueRequest(
        due_identity=identity,
        baseline_label_cutoff=bundle.label_cutoff if bundle else None,
        current_label_cutoff=current_cutoff,
        calendar_dates=history.calendar.open_dates,
        input_revision=industry_revision,
        observed_at=query.observed_at,
        data_complete=current_cutoff is not None,
        training_contract_changed=contract_changed or invalid,
    ))


def EvaluateAllHistoryTrainingDue(
    history_root: Path,
    training_root: Path,
    observed_at: datetime,
    *,
    maintenance_path: Path,
    load_expectation: Callable[[TrainedProfileContract, datetime], HistoryTrainingExpectation],
    selected_profile: TrainedProfileContract | None = None, sqlite_policy: SqliteIoPolicy,
    persist: bool = True,
) -> tuple[HeadTrainingStatus, ...]:
    results: list[HeadTrainingStatus] = []
    for profile in (selected_profile,) if selected_profile is not None else (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE):
        heads, failure = _inspect_profile(training_root, profile)
        try:
            expectation = load_expectation(profile, observed_at)
        except (OSError, RuntimeError, TypeError, ValueError, ImportError):
            expectation = None
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
                        maintenance_path=maintenance_path,
                        industry_snapshot=expectation.industry_snapshot,
                        expected_training_contract_hash=expectation.contract_hash(strategy),
                        persist=persist,
                        inspected=heads,
                        inspected_invalid=failure not in (None, "missing"),
                    ), sqlite_policy=sqlite_policy
                )
                if failure != "publication_busy" and expectation is not None
                else None
            )
            results.append(
                HeadTrainingStatus(
                    profile.profile_id,
                    strategy,
                    _publication_identity(profile, bundle) if bundle else None,
                    due.state if due else None,
                    failure if failure == "publication_busy"
                    else "training_inputs_unavailable" if expectation is None else failure,
                )
            )
    return tuple(results)


class PublishedTrainingState:
    """In-memory maintenance observations; explicit refresh never replaces loaded models."""

    def __init__(
        self,
        history_root: Path,
        training_root: Path,
        loaded: tuple[ModelPublicationIdentity, ...],
        *,
        maintenance_path: Path,
        owned_profile: ScoringProfileId,
        load_expectation: Callable[[TrainedProfileContract, datetime], HistoryTrainingExpectation],
        wall_clock: Callable[[], datetime], sqlite_policy: SqliteIoPolicy,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self._history_root = history_root
        self._training_root = training_root
        self._maintenance_path = maintenance_path
        self._loaded = loaded
        self._owned_profile = owned_profile
        self._load_expectation = load_expectation
        self._clock = wall_clock
        self._lock = threading.Lock()
        self._initialization_lock = threading.Lock()
        self._initialized = False
        profile_ids: tuple[ScoringProfileId, ...] = (
            "v2", "v3", *(("v5",) if owned_profile == "v5" else ()), *(("v1",) if owned_profile == "v1" else ())
        )
        self._status = tuple(
            HeadTrainingStatus(
                profile_id,
                strategy,
                None,
                None,
                "training_not_initialized",
                next((item for item in loaded if item.profile_id == profile_id and item.strategy is strategy), None),
                profile_id == owned_profile,
                "publication_not_observed",
            )
            for profile_id in profile_ids
            for strategy in (Strategy.TOMORROW, Strategy.D25)
        )

    def initialize(self) -> None:
        with self._initialization_lock:
            if self._initialized:
                return
            self._initialized = True
            self._initialize()

    def refresh(self) -> None:
        """Refresh outside status reads and scoring; keep startup-loaded model identities."""
        with self._initialization_lock:
            self._initialize()
            self._initialized = True

    def _initialize(self) -> None:
        control = HistoryControlIndex(self._history_root / "catalog.sqlite3", sqlite_policy=self._sqlite_policy)
        try:
            history = control.load_published_state()
        except HistoryControlError:
            history = None
        observed_at = self._clock()
        results: list[HeadTrainingStatus] = []
        for profile in self._profiles():
            heads, failure = _inspect_profile(self._training_root, profile)
            try:
                expectation = self._load_expectation(profile, observed_at)
            except (OSError, RuntimeError, TypeError, ValueError, ImportError):
                expectation = None
            for strategy in (Strategy.TOMORROW, Strategy.D25):
                bundle = next((item for item in heads if item.strategy is strategy), None)
                due = None
                if history is not None and failure != "publication_busy" and expectation is not None:
                    query = HistoryTrainingDueQuery(
                        self._history_root,
                        self._training_root / profile.output_directory,
                        observed_at,
                        profile,
                        strategy,
                        maintenance_path=self._maintenance_path,
                        industry_snapshot=expectation.industry_snapshot,
                        expected_training_contract_hash=expectation.contract_hash(strategy),
                        persist=False,
                        inspected=heads,
                        inspected_invalid=failure not in (None, "missing"),
                    )
                    industry = expectation.industry_snapshot.project(tuple(item.code for item in history.universe.securities))
                    baseline_industry_hash = _baseline_industry_hash(query, history, bundle, control)
                    identity = _due_identity(
                        query,
                        BuildTrainingInputHash(history.snapshot.content_hash, industry.content_hash),
                        bundle.metadata_hash if bundle else None,
                        invalid=failure not in (None, "missing"),
                        baseline_industry_hash=baseline_industry_hash,
                    )
                    try:
                        due = TrainingMaintenanceIndex(self._maintenance_path, sqlite_policy=self._sqlite_policy).read_due_state(
                            identity, latest=True
                        )
                    except TrainingMaintenanceError:
                        due = None
                    if due is None:
                        try:
                            due = _metadata_due(
                                query, history, bundle, baseline_industry_hash=baseline_industry_hash,
                                invalid=failure not in (None, "missing"),
                            )
                        except (OSError, ValueError):
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
                        failure if failure == "publication_busy"
                        else "training_inputs_unavailable" if expectation is None
                        else failure or ("training_due_unavailable" if due is None else None),
                        loaded,
                        profile.profile_id == self._owned_profile,
                    )
                )
        if self._owned_profile == "v1":
            identities, failure = self._v1_publications(lambda: False)
            results.extend(
                HeadTrainingStatus(
                    "v1",
                    strategy,
                    next((item for item in identities if item.strategy is strategy), None),
                    None,
                    "training_cadence_unsupported",
                    next((item for item in self._loaded if item.profile_id == "v1" and item.strategy is strategy), None),
                    True,
                    failure,
                )
                for strategy in (Strategy.TOMORROW, Strategy.D25)
            )
        with self._lock:
            self._status = tuple(results)

    def _profiles(self) -> tuple[TrainedProfileContract, ...]:
        return (
            V2_TRAINING_PROFILE,
            V3_TRAINING_PROFILE,
            *((V5_TRAINING_PROFILE,) if self._owned_profile == "v5" else ()),
        )

    def _v1_publications(
        self, cancelled: Callable[[], bool]
    ) -> tuple[tuple[ModelPublicationIdentity, ...], str | None]:
        from trader.training.infra.profile.v1.models import ReadV1PublicationIdentities

        try:
            return ReadV1PublicationIdentities(self._training_root, cancel_requested=cancelled), None
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return (), f"model_observation:{type(exc).__name__}"

    def observe_publication(self, cancelled: Callable[[], bool]) -> None:
        """Read publication metadata only; never replace predictors or recompute samples."""
        if cancelled() or not self._initialization_lock.acquire(blocking=False):
            return
        try:
            for profile in self._profiles():
                if cancelled():
                    return
                try:
                    identities = ReadPublishedProfileIdentities(
                        self._training_root / profile.output_directory,
                        profile,
                        cancel_requested=cancelled,
                    )
                    failure = None
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    identities, failure = (), f"model_observation:{type(exc).__name__}"
                self._record_publication_observation(profile.profile_id, identities, failure, cancelled)
            if self._owned_profile == "v1" and not cancelled():
                identities, failure = self._v1_publications(cancelled)
                self._record_publication_observation("v1", identities, failure, cancelled)
        finally:
            self._initialization_lock.release()

    def _record_publication_observation(
        self,
        profile_id: ScoringProfileId,
        identities: tuple[ModelPublicationIdentity, ...],
        failure: str | None,
        cancelled: Callable[[], bool],
    ) -> None:
        if cancelled():
            return
        with self._lock:
            self._status = tuple(
                replace(
                    item,
                    disk=next((identity for identity in identities if identity.strategy is item.strategy), None)
                    if failure is None else item.disk,
                    publication_observation_failure=failure,
                )
                if item.profile_id == profile_id else item
                for item in self._status
            )

    def status(self) -> tuple[HeadTrainingStatus, ...]:
        with self._lock:
            return self._status


def _publication_identity(profile: TrainedProfileContract, bundle: PublishedHeadBundle) -> ModelPublicationIdentity:
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
) -> tuple[tuple[PublishedHeadBundle, ...], str | None]:
    try:
        return InspectPublishedProfileBundles(training_root / profile.output_directory, profile), None
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
