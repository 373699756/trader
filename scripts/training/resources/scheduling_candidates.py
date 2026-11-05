"""Measured-priority CPU admission experiments for isolated training diagnostics."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import Condition, current_thread, main_thread
from typing import Literal

from trader.infra.disk_types import DiskKind
from trader.infra.serialization.canonical import ContentHash
from trader.infra.settings.parser import Integer, Number, ReadJsonObject, RequireExactKeys, Text
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.training.application.resources import TrainingCpuAdmission
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.execution_timing import TrainingExecutionMeasurements

SchedulingCandidate = Literal["baseline", "fixed", "long_task_first", "borrowing"]


@dataclass(frozen=True)
class ProfileFitEstimate:
    profile: ScoringProfileId
    fit_seconds: float


@dataclass(frozen=True)
class SchedulingEvidence:
    available_cpus: int
    cpu_capacity: int
    native_threads: int
    estimates: tuple[ProfileFitEstimate, ...]
    content_hash: str


def LoadSchedulingEvidence(
    path: Path,
    *,
    source_hash: str,
    configuration_hash: str,
    implementation_hash: str,
    profiles: tuple[ScoringProfileId, ...],
    medium: DiskKind,
) -> SchedulingEvidence:
    raw = ReadJsonObject(path)
    RequireExactKeys(raw, {
        "schema_version", "source_hash", "configuration_hash", "implementation_hash", "medium",
        "available_cpus", "cpu_capacity", "native_threads", "profiles",
    }, "training_scheduling")
    if (
        raw["schema_version"] != "training-scheduling-evidence"
        or raw["source_hash"] != source_hash
        or raw["configuration_hash"] != configuration_hash
        or raw["implementation_hash"] != implementation_hash
        or raw["medium"] != medium or medium == "unknown"
    ):
        raise ValueError("training_scheduling_evidence_identity_mismatch")
    rows = raw["profiles"]
    if not isinstance(rows, list):
        raise ValueError("training_scheduling_profiles_invalid")
    estimates = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("training_scheduling_profiles_invalid")
        RequireExactKeys(row, {"profile", "fit_seconds"}, "training_scheduling.profile")
        profile = Text(row, "profile")
        if profile not in SCORING_PROFILE_IDS:
            raise ValueError("training_scheduling_profiles_invalid")
        estimates.append(ProfileFitEstimate(profile, Number(row, "fit_seconds", minimum=0.000001)))
    if tuple(item.profile for item in estimates) != profiles or len(set(profiles)) != len(profiles):
        raise ValueError("training_scheduling_profiles_mismatch")
    available = Integer(raw, "available_cpus", minimum=1)
    capacity = Integer(raw, "cpu_capacity", minimum=1, maximum=available)
    return SchedulingEvidence(
        available, capacity, Integer(raw, "native_threads", minimum=1, maximum=capacity), tuple(estimates), ContentHash(raw)
    )


@dataclass(frozen=True)
class SchedulingGrant:
    profile: ScoringProfileId
    requested_threads: int
    granted_threads: int
    charged_threads: int
    borrowed_threads: int


@dataclass
class SchedulingExperiment:
    candidate: SchedulingCandidate
    evidence: SchedulingEvidence
    grants: list[SchedulingGrant] = field(default_factory=list)
    jobs: list["_CpuSchedule"] = field(default_factory=list)


class _CpuSchedule:
    def __init__(
        self,
        budget: TrainingCpuBudget,
        profiles: tuple[ScoringProfileId, ...],
        evidence: SchedulingEvidence,
        experiment: SchedulingExperiment,
    ) -> None:
        if (budget.available_cpus, budget.capacity, budget.native_threads) != (
            evidence.available_cpus, evidence.cpu_capacity, evidence.native_threads
        ):
            raise ValueError("training_scheduling_machine_budget_mismatch")
        self.budget = budget
        self.experiment = experiment
        self.estimates = {item.profile: item.fit_seconds for item in evidence.estimates}
        if not profiles or not set(profiles).issubset(self.estimates):
            raise ValueError("training_scheduling_profiles_mismatch")
        self.fixed_threads = max(budget.native_threads, budget.capacity // len(profiles))
        self.condition = Condition()
        self.waiting: list[tuple[float, int]] = []
        self.reserved = 0
        self.sequence = 0

    @contextmanager
    def reserve(self, profile: ScoringProfileId, requested: int, heartbeat: Callable[[], None]) -> Iterator[int]:
        if type(requested) is not int or requested < 1:
            raise ValueError("training_cpu_request_invalid")
        limit = min(requested, self.budget.capacity)
        if self.experiment.candidate == "fixed":
            limit = min(limit, self.fixed_threads)
        with self.condition:
            priority = 0.0 if self.experiment.candidate == "fixed" else -self.estimates[profile]
            ticket = (priority, self.sequence)
            self.sequence += 1
            self.waiting.append(ticket)
            try:
                while True:
                    heartbeat()
                    available = self.budget.capacity - self.reserved
                    threads = min(limit, available) if self.experiment.candidate == "borrowing" else limit
                    required = max(threads, self.budget.native_threads)
                    if ticket == min(self.waiting) and threads > 0 and required <= available:
                        self.reserved += required
                        break
                    self.condition.wait(timeout=0.25)
            finally:
                self.waiting.remove(ticket)
                self.condition.notify_all()
        try:
            # The original budget remains the final safety boundary and owns native limits.
            with self.budget.reserve(threads, heartbeat) as granted:
                with self.condition:
                    self.experiment.grants.append(SchedulingGrant(
                        profile, requested, granted, required, max(0, granted - self.fixed_threads)
                    ))
                yield granted
        finally:
            with self.condition:
                self.reserved -= required
                self.condition.notify_all()


@dataclass(frozen=True)
class _CandidateAdmission:
    profile: ScoringProfileId
    schedule: _CpuSchedule

    def reserve(self, requested_threads: int, heartbeat: Callable[[], None]) -> AbstractContextManager[int]:
        return self.schedule.reserve(self.profile, requested_threads, heartbeat)


@contextmanager
def UseSchedulingCandidate(candidate: SchedulingCandidate, evidence: SchedulingEvidence) -> Iterator[SchedulingExperiment]:
    if candidate not in {"fixed", "long_task_first", "borrowing"}:
        raise ValueError("training_scheduling_candidate_invalid")
    if current_thread() is not main_thread():
        raise ValueError("training_scheduling_candidate_requires_main_worker")
    experiment = SchedulingExperiment(candidate, evidence)
    original = TrainingExecutionMeasurements.cpu_admission
    schedules: dict[TrainingExecutionMeasurements, _CpuSchedule] = {}

    def _admission(
        self: TrainingExecutionMeasurements, profile: ScoringProfileId, admission: TrainingCpuAdmission
    ) -> TrainingCpuAdmission:
        budget = admission
        if not isinstance(budget, TrainingCpuBudget):
            raise ValueError("training_scheduling_requires_job_cpu_budget")
        if self not in schedules:
            profiles = tuple(item.profile for item in self.snapshot().profiles)
            schedule = _CpuSchedule(budget, profiles, evidence, experiment)
            schedules[self] = schedule
            experiment.jobs.append(schedule)
        elif schedules[self].budget is not budget:
            raise ValueError("training_scheduling_job_cpu_budget_changed")
        return original(self, profile, _CandidateAdmission(profile, schedules[self]))

    try:
        TrainingExecutionMeasurements.cpu_admission = _admission  # type: ignore[method-assign]
        yield experiment
    finally:
        TrainingExecutionMeasurements.cpu_admission = original  # type: ignore[method-assign]


def ProjectSchedulingExperiment(experiment: SchedulingExperiment) -> dict[str, object]:
    return {
        "name": experiment.candidate,
        "evidence_hash": experiment.evidence.content_hash,
        "candidate_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "profile_estimates": [{"profile": item.profile, "fit_seconds": item.fit_seconds} for item in experiment.evidence.estimates],
        "production_enabled": False,
        "performance_evidence": False,
        "pending_requests": sum(len(job.waiting) for job in experiment.jobs),
        "reserved_threads": sum(job.reserved for job in experiment.jobs),
        "grants": [{
            "profile": item.profile, "requested_threads": item.requested_threads,
            "granted_threads": item.granted_threads, "charged_threads": item.charged_threads,
            "borrowed_threads": item.borrowed_threads,
        } for item in experiment.grants],
    }
