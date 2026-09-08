"""Typed feature identities, manifests, and pure price-feature calculators."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

FeatureDirection = Literal["higher", "lower", "mixed", "diagnostic"]
FeatureMissingPolicy = Literal["reject_model_input", "preserve_missing"]
FeatureVisibility = Literal["completed_session", "decision_anchor", "same_anchor_cross_section"]

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


@dataclass(frozen=True, order=True)
class FeatureId:
    value: str

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.value) is None:
            raise ValueError("feature id must be a stable snake-case identifier")


@dataclass(frozen=True)
class FeatureSpec:
    feature_id: FeatureId
    unit: str
    family: str
    direction: FeatureDirection
    visibility: FeatureVisibility
    missing_policy: FeatureMissingPolicy
    dependencies: tuple[FeatureId, ...]
    calculator_group: str

    def __post_init__(self) -> None:
        if (
            not self.unit
            or _IDENTIFIER.fullmatch(self.family) is None
            or _IDENTIFIER.fullmatch(self.calculator_group) is None
            or len(set(self.dependencies)) != len(self.dependencies)
            or self.feature_id in self.dependencies
        ):
            raise ValueError("feature specification is invalid")


@dataclass(frozen=True)
class FeatureValue:
    feature_id: FeatureId
    value: float | None

    def __post_init__(self) -> None:
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("feature value must be finite when present")


@dataclass(frozen=True)
class FeatureVector:
    manifest_hash: str
    values: tuple[float | None, ...]
    missing_mask: tuple[bool, ...]

    def __post_init__(self) -> None:
        if len(self.manifest_hash) != 64 or len(self.values) != len(self.missing_mask):
            raise ValueError("feature vector identity is invalid")
        if any(value is not None and not math.isfinite(value) for value in self.values):
            raise ValueError("feature vector values must be finite when present")
        if self.missing_mask != tuple(value is None for value in self.values):
            raise ValueError("feature vector missing mask does not match values")

    def require_complete(self) -> tuple[float, ...]:
        if any(self.missing_mask):
            raise ValueError("feature vector contains missing model inputs")
        return tuple(float(value) for value in self.values if value is not None)


@dataclass(frozen=True)
class FeatureVectorManifest:
    feature_ids: tuple[FeatureId, ...]
    units: tuple[str, ...]
    missing_policies: tuple[FeatureMissingPolicy, ...]
    catalog_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        width = len(self.feature_ids)
        if (
            width < 1
            or len(set(self.feature_ids)) != width
            or len(self.units) != width
            or len(self.missing_policies) != width
            or len(self.catalog_hash) != 64
        ):
            raise ValueError("feature vector manifest is invalid")
        object.__setattr__(
            self,
            "content_hash",
            _content_hash(
                (
                    "feature_vector_manifest",
                    self.catalog_hash,
                    *(item.value for item in self.feature_ids),
                    *self.units,
                    *self.missing_policies,
                )
            ),
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.value for item in self.feature_ids)

    def bind(self, values: tuple[FeatureValue, ...]) -> FeatureVector:
        by_id = {item.feature_id: item.value for item in values}
        if len(by_id) != len(values) or set(by_id) != set(self.feature_ids):
            raise ValueError("feature values do not match the vector manifest")
        ordered = tuple(by_id[feature_id] for feature_id in self.feature_ids)
        return FeatureVector(self.content_hash, ordered, tuple(value is None for value in ordered))


@dataclass(frozen=True)
class FeatureSpecCatalog:
    specs: tuple[FeatureSpec, ...]
    content_hash: str = field(init=False)
    _by_id: MappingProxyType[FeatureId, FeatureSpec] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.specs, key=lambda item: item.feature_id))
        if not ordered or len({item.feature_id for item in ordered}) != len(ordered):
            raise ValueError("feature catalog identities must be present and unique")
        object.__setattr__(self, "specs", ordered)
        object.__setattr__(self, "_by_id", MappingProxyType({item.feature_id: item for item in ordered}))
        parts: list[str] = ["feature_spec_catalog"]
        for spec in ordered:
            parts.extend(
                (
                    spec.feature_id.value,
                    spec.unit,
                    spec.family,
                    spec.direction,
                    spec.visibility,
                    spec.missing_policy,
                    spec.calculator_group,
                    *(item.value for item in spec.dependencies),
                    "dependency_boundary",
                )
            )
        object.__setattr__(self, "content_hash", _content_hash(tuple(parts)))

    def contains(self, feature_id: FeatureId) -> bool:
        return feature_id in self._by_id

    def require(self, feature_id: FeatureId | str) -> FeatureSpec:
        identity = feature_id if isinstance(feature_id, FeatureId) else FeatureId(feature_id)
        try:
            return self._by_id[identity]
        except KeyError as exc:
            raise KeyError(f"unknown feature: {identity.value}") from exc

    def manifest(self, feature_ids: tuple[FeatureId, ...]) -> FeatureVectorManifest:
        if len(set(feature_ids)) != len(feature_ids):
            raise ValueError("feature manifest identities must be unique")
        specs = tuple(self.require(feature_id) for feature_id in feature_ids)
        return FeatureVectorManifest(
            feature_ids,
            tuple(item.unit for item in specs),
            tuple(item.missing_policy for item in specs),
            self.content_hash,
        )


@dataclass(frozen=True)
class QfqPriceAnchors:
    current_close: float | None
    lagged_closes: tuple[tuple[int, float | None], ...]

    def __post_init__(self) -> None:
        horizons = tuple(item[0] for item in self.lagged_closes)
        if any(value < 1 for value in horizons) or len(set(horizons)) != len(horizons):
            raise ValueError("qfq price anchor horizons must be positive and unique")
        object.__setattr__(self, "lagged_closes", tuple(sorted(self.lagged_closes)))

    def lag(self, sessions: int) -> float | None:
        return dict(self.lagged_closes).get(sessions)


class DailyReturnFeatureCalculator:
    """Calculate current-anchor returns in decimal-return units."""

    feature_ids = tuple(FeatureId(f"qfq_return_{horizon}d") for horizon in (1, 3, 5))

    @classmethod
    def calculate(cls, anchors: QfqPriceAnchors) -> tuple[FeatureValue, ...]:
        return tuple(
            FeatureValue(feature_id, decimal_return(anchors.current_close, anchors.lag(horizon)))
            for feature_id, horizon in zip(cls.feature_ids, (1, 3, 5), strict=True)
        )


class SkipRecentMomentumFeatureCalculator:
    """Calculate D-5 over D-lookback momentum, preserving current production semantics."""

    feature_ids = tuple(FeatureId(f"qfq_momentum_{horizon}d_skip5") for horizon in (20, 40, 60))

    @classmethod
    def calculate(cls, anchors: QfqPriceAnchors) -> tuple[FeatureValue, ...]:
        recent = anchors.lag(5)
        return tuple(
            FeatureValue(feature_id, decimal_return(recent, anchors.lag(horizon)))
            for feature_id, horizon in zip(cls.feature_ids, (20, 40, 60), strict=True)
        )


def decimal_return(end: float | None, start: float | None) -> float | None:
    if end is None or start is None or not math.isfinite(end) or not math.isfinite(start) or end <= 0.0 or start <= 0.0:
        return None
    return end / start - 1.0


def calculate_tomorrow_qfq_alpha(anchors: QfqPriceAnchors) -> tuple[FeatureValue, ...]:
    return (*DailyReturnFeatureCalculator.calculate(anchors), *SkipRecentMomentumFeatureCalculator.calculate(anchors))


@dataclass(frozen=True)
class _FeatureSemantics:
    family: str
    direction: FeatureDirection
    visibility: FeatureVisibility
    missing_policy: FeatureMissingPolicy
    calculator_group: str


def _feature(
    name: str,
    *,
    semantics: _FeatureSemantics,
    dependencies: tuple[str, ...],
    unit: str = "decimal_return",
) -> FeatureSpec:
    return FeatureSpec(
        FeatureId(name),
        unit,
        semantics.family,
        semantics.direction,
        semantics.visibility,
        semantics.missing_policy,
        tuple(FeatureId(item) for item in dependencies),
        semantics.calculator_group,
    )


def _content_hash(parts: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


_RETURN_SEMANTICS = _FeatureSemantics("short_return", "mixed", "decision_anchor", "reject_model_input", "daily_return")
_MOMENTUM_SEMANTICS = _FeatureSemantics(
    "residual_momentum", "mixed", "completed_session", "preserve_missing", "skip_recent_momentum"
)
_RESIDUAL_SEMANTICS = _FeatureSemantics(
    "residual_momentum", "mixed", "same_anchor_cross_section", "reject_model_input", "cross_section_residual"
)

_RETURN_SPECS = tuple(
    _feature(
        f"qfq_return_{horizon}d",
        semantics=_RETURN_SEMANTICS,
        dependencies=("qfq_close_current", f"qfq_close_lag_{horizon}"),
    )
    for horizon in (1, 3, 5)
)
_MOMENTUM_SPECS = tuple(
    _feature(
        f"qfq_momentum_{horizon}d_skip5",
        semantics=_MOMENTUM_SEMANTICS,
        dependencies=("qfq_close_lag_5", f"qfq_close_lag_{horizon}"),
    )
    for horizon in (20, 40, 60)
)
_RESIDUAL_SPECS = tuple(
    _feature(
        f"qfq_residual_momentum_{horizon}d_skip5",
        semantics=_RESIDUAL_SEMANTICS,
        dependencies=(
            f"qfq_momentum_{horizon}d_skip5",
            "market_cross_section",
            "board_cross_section",
            "industry_cross_section",
            "qfq_average_amount_20d",
        ),
    )
    for horizon in (20, 40, 60)
)
_RESEARCH_FEATURE_NAMES = (
    "residual_reversal_1d",
    "residual_reversal_3d",
    "residual_reversal_5d",
    "residual_momentum_20_5",
    "residual_momentum_40_5",
    "residual_momentum_60_5",
    "overnight_gap",
    "intraday_return",
    "morning_return",
    "afternoon_return",
    "tail_return_30m",
    "close_location",
    "tail_amount_share",
)
_RESEARCH_SPECS = tuple(
    _feature(
        name,
        unit="ratio" if name in {"close_location", "tail_amount_share"} else "decimal_return",
        semantics=_FeatureSemantics(
            (
                "residual_reversal"
                if name.startswith("residual_reversal")
                else "residual_momentum"
                if name.startswith("residual_momentum")
                else "overnight"
                if name == "overnight_gap"
                else "tail"
                if name in {"tail_return_30m", "close_location", "tail_amount_share"}
                else "intraday"
            ),
            "mixed",
            "same_anchor_cross_section" if name.startswith("residual_") else "decision_anchor",
            "preserve_missing",
            "point_in_time_research",
        ),
        dependencies=("point_in_time_feature_facts",),
    )
    for name in _RESEARCH_FEATURE_NAMES
)

FEATURE_SPEC_CATALOG = FeatureSpecCatalog((*_RETURN_SPECS, *_MOMENTUM_SPECS, *_RESIDUAL_SPECS, *_RESEARCH_SPECS))
TOMORROW_RAW_ALPHA_FEATURE_MANIFEST = FEATURE_SPEC_CATALOG.manifest(
    tuple(item.feature_id for item in (*_RETURN_SPECS, *_MOMENTUM_SPECS))
)
TOMORROW_MODEL_FEATURE_MANIFEST = FEATURE_SPEC_CATALOG.manifest(
    tuple(item.feature_id for item in (*_RETURN_SPECS, *_RESIDUAL_SPECS))
)
TOMORROW_RESIDUAL_MOMENTUM_FEATURE_MANIFEST = FEATURE_SPEC_CATALOG.manifest(
    tuple(item.feature_id for item in _RESIDUAL_SPECS)
)
TOMORROW_RESEARCH_FEATURE_MANIFEST = FEATURE_SPEC_CATALOG.manifest(
    tuple(FeatureId(name) for name in _RESEARCH_FEATURE_NAMES)
)


__all__ = [
    "DailyReturnFeatureCalculator",
    "FEATURE_SPEC_CATALOG",
    "FeatureId",
    "FeatureSpec",
    "FeatureSpecCatalog",
    "FeatureValue",
    "FeatureVector",
    "FeatureVectorManifest",
    "QfqPriceAnchors",
    "SkipRecentMomentumFeatureCalculator",
    "TOMORROW_MODEL_FEATURE_MANIFEST",
    "TOMORROW_RAW_ALPHA_FEATURE_MANIFEST",
    "TOMORROW_RESEARCH_FEATURE_MANIFEST",
    "TOMORROW_RESIDUAL_MOMENTUM_FEATURE_MANIFEST",
    "calculate_tomorrow_qfq_alpha",
    "decimal_return",
]
