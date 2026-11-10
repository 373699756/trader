"""Validate one head inside an already selected immutable profile generation."""

from __future__ import annotations

import tempfile
import json
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import islice
from pathlib import Path

from trader.recommendation.domain.publication.models import Strategy
from trader.infra.regular_file import ReadRegularFile
from trader.infra.cache_contracts import BoundedCache, CacheIdentity, RequestKey
from trader.training.domain.model_publication import ModelPublicationIdentity
from trader.training.infra.model_bundles.bundle_codec import (
    DecodeHeadPublicationIdentity, LoadHeadBundle, TrainedHeadModelBundle, ModelBundleContractError,
)
from trader.training.infra.model_bundles.contracts import TrainedProfileContract


@dataclass(frozen=True)
class PublishedHeadBundle:
    model_path: Path
    strategy: Strategy
    training_input_hash: str
    source_identity_hash: str
    label_cutoff: date
    training_contract_hash: str
    model_hash: str
    report_hash: str
    training_input_document_hash: str
    metadata_hash: str
    training_run_hash: str
    price_snapshot_hash: str = field(kw_only=True)


@dataclass(frozen=True)
class LoadedPublishedHeadBundle(PublishedHeadBundle):
    model: TrainedHeadModelBundle


@dataclass(frozen=True)
class _ValidatedMetadataIdentity:
    contract: TrainedProfileContract
    identity: ModelPublicationIdentity


def MakeBundleStagingDirectory(profile_root: Path) -> Path:
    profile_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".bundle-staging.", dir=profile_root))


def InspectPublishedHeadBundle(
    head_root: Path,
    strategy: Strategy,
    profile: TrainedProfileContract,
) -> LoadedPublishedHeadBundle:
    if not head_root.exists() and not head_root.is_symlink():
        raise FileNotFoundError(head_root)
    if not head_root.is_dir() or head_root.is_symlink():
        raise ValueError("trained head directory is invalid")
    model_path = head_root / "model.json"
    files = {path.name for path in head_root.iterdir()}
    if files < {"model.json", "metadata.json"}:
        raise FileNotFoundError("trained head fixed files are missing")
    if files != {"model.json", "metadata.json"}:
        raise ValueError("trained head fixed files are incomplete")
    for name in ("model.json", "metadata.json"):
        path = head_root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("trained head fixed file is invalid")
    bundle = LoadHeadBundle(model_path, strategy, profile)
    metadata = bundle.metadata_hash
    return LoadedPublishedHeadBundle(
        model_path,
        strategy,
        bundle.training_input_hash,
        bundle.source_identity_hash,
        bundle.label_cutoff,
        bundle.training_contract_hash,
        bundle.content_hash,
        bundle.report_hash,
        bundle.training_input_document_hash,
        metadata,
        bundle.training_run_hash,
        bundle,
        price_snapshot_hash=bundle.price_snapshot_hash,
    )


def ReadPublishedHeadIdentity(
    head_root: Path,
    strategy: Strategy,
    profile: TrainedProfileContract,
    *,
    cancel_requested: Callable[[], bool],
    metadata_cache: BoundedCache[object] | None = None,
    observed_at: datetime | None = None,
) -> ModelPublicationIdentity:
    """Read only bounded publication metadata inside a caller-owned profile lock."""
    if cancel_requested():
        raise InterruptedError("model_publication_observation_cancelled")
    if not head_root.exists() and not head_root.is_symlink():
        raise FileNotFoundError(head_root)
    if not head_root.is_dir() or head_root.is_symlink():
        raise ValueError("trained head directory is invalid")
    files = {path.name for path in islice(head_root.iterdir(), 3)}
    if files < {"model.json", "metadata.json"}:
        raise FileNotFoundError("trained head fixed files are missing")
    if files != {"model.json", "metadata.json"}:
        raise ValueError("trained head fixed files are incomplete")
    model_path = head_root / "model.json"
    if model_path.is_symlink() or not model_path.is_file():
        raise ValueError("trained head fixed file is invalid")
    metadata_path = head_root / "metadata.json"
    fingerprint = _metadata_fingerprint(metadata_path)
    cache_identity = None
    if metadata_cache is not None:
        if observed_at is None or observed_at.utcoffset() is None:
            raise ValueError("metadata cache requires an aware observation time")
        cache_identity = CacheIdentity(
            "model_publication_metadata", "published_models", str(metadata_path.absolute()),
            RequestKey({"fingerprint": fingerprint, "strategy": strategy.value}), observed_at.date().isoformat(), "all_day",
            profile.algorithm, profile.profile_id, "head_metadata",
        )
        cached = metadata_cache.get(cache_identity)
        if cached is not None and cached.state == "fresh" and isinstance(cached.value, _ValidatedMetadataIdentity):
            if cached.value.contract == profile:
                if cancel_requested():
                    raise InterruptedError("model_publication_observation_cancelled")
                if _metadata_fingerprint(metadata_path) != fingerprint:
                    raise ModelBundleContractError("publication metadata changed while being observed")
                return cached.value.identity
    document = json.loads(ReadRegularFile(metadata_path, maximum_bytes=8 * 1024 * 1024))
    if cancel_requested():
        raise InterruptedError("model_publication_observation_cancelled")
    identity = DecodeHeadPublicationIdentity(document, strategy, profile)
    if _metadata_fingerprint(metadata_path) != fingerprint:
        raise ModelBundleContractError("publication metadata changed while being observed")
    if cancel_requested():
        raise InterruptedError("model_publication_observation_cancelled")
    if metadata_cache is not None and cache_identity is not None and observed_at is not None:
        metadata_cache.put(
            cache_identity, _ValidatedMetadataIdentity(profile, identity),
            data_version=identity.metadata_hash, source_time=observed_at,
        )
    return identity


def _metadata_fingerprint(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
        raise ValueError("regular_file metadata must be bounded and regular")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


__all__ = [
    "PublishedHeadBundle",
    "InspectPublishedHeadBundle",
    "ReadPublishedHeadIdentity",
    "LoadedPublishedHeadBundle",
    "MakeBundleStagingDirectory",
]
