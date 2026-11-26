"""Share verified maintenance receipts for unchanged V2/V3 D25 directories."""

import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.infra.model_bundles.bundle_snapshot import PublishedHeadBundle
from trader.training.infra.model_bundles.contracts import TrainedProfileContract
from trader.training.infra.model_bundles.profile_publication import (
    InspectPublishedProfileBundles, ProfilePublicationBusyError, ProfilePublicationLock,
)


@dataclass(frozen=True, slots=True)
class _VerifiedProfile:
    root: Path
    generation: tuple[tuple[int, ...], ...]
    contract: TrainedProfileContract
    heads: tuple[PublishedHeadBundle, ...]


class StartupProfileInspections:
    """Keep one lightweight receipt per repeated profile in this system."""

    def __init__(self) -> None:
        self._verified: dict[ScoringProfileId, _VerifiedProfile] = {}
        self._locks = {"v2": threading.Lock(), "v3": threading.Lock()}

    def inspect(self, root: Path, profile: TrainedProfileContract) -> tuple[PublishedHeadBundle, ...]:
        if (os.name != "posix" or profile.profile_id not in self._locks
                or tuple(head.directory_name for head in profile.heads) != ("d25",)):
            return InspectPublishedProfileBundles(root, profile)
        if not root.parent.is_dir():
            raise FileNotFoundError(root)
        deadline = time.monotonic() + 2.0
        lock = self._locks[profile.profile_id]
        if not lock.acquire(timeout=2.0):
            raise ProfilePublicationBusyError("model_publication_busy")
        try:
            return self._inspect(root, profile, deadline)
        finally:
            lock.release()

    def _inspect(
        self, root: Path, profile: TrainedProfileContract, deadline: float,
    ) -> tuple[PublishedHeadBundle, ...]:
        with ProfilePublicationLock(root, max(0.0, deadline - time.monotonic())):
            generation = _generation(root)
            cached = self._verified.get(profile.profile_id)
            if cached is not None and cached.root == root and cached.contract == profile and cached.generation == generation:
                return cached.heads
        loaded = InspectPublishedProfileBundles(root, profile, timeout_seconds=max(0.0, deadline - time.monotonic()))
        heads = tuple(PublishedHeadBundle(
            item.model_path, item.strategy, item.training_input_hash, item.source_identity_hash, item.label_cutoff,
            item.training_contract_hash, item.model_hash, item.report_hash, item.training_input_document_hash,
            item.metadata_hash, item.training_run_hash, price_snapshot_hash=item.price_snapshot_hash,
        ) for item in loaded)
        with ProfilePublicationLock(root, max(0.0, deadline - time.monotonic())):
            if _generation(root) != generation:
                raise ProfilePublicationBusyError("model publication changed during startup inspection")
            self._verified[profile.profile_id] = _VerifiedProfile(root, generation, profile, heads)
        return heads


def _generation(root: Path) -> tuple[tuple[int, ...], ...]:
    directory = root / "d25"
    paths = ((root, True), (directory, True), (directory / "metadata.json", False), (directory / "model.json", False))
    stamps = []
    for path, is_directory in paths:
        info = path.lstat()
        if not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode)):
            raise ValueError("d25_startup_generation_has_invalid_file_type")
        stamps.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return tuple(stamps)
