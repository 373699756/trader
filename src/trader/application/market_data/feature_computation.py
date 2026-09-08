"""Build deterministic grouped computation plans from the domain feature catalog."""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.market.feature_contracts import (
    FEATURE_SPEC_CATALOG,
    FeatureId,
    FeatureSpec,
    FeatureSpecCatalog,
    FeatureVectorManifest,
)


@dataclass(frozen=True)
class FeatureComputationStage:
    calculator_group: str
    output_ids: tuple[FeatureId, ...]
    dependency_ids: tuple[FeatureId, ...]

    def __post_init__(self) -> None:
        if not self.calculator_group or not self.output_ids or len(set(self.output_ids)) != len(self.output_ids):
            raise ValueError("feature computation stage is invalid")


@dataclass(frozen=True)
class FeatureComputationPlan:
    manifest: FeatureVectorManifest
    stages: tuple[FeatureComputationStage, ...]
    required_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.stages or len(set(self.required_fact_ids)) != len(self.required_fact_ids):
            raise ValueError("feature computation plan is invalid")

    @property
    def output_names(self) -> tuple[str, ...]:
        return self.manifest.names


def build_feature_computation_plan(
    manifest: FeatureVectorManifest,
    catalog: FeatureSpecCatalog = FEATURE_SPEC_CATALOG,
) -> FeatureComputationPlan:
    if manifest.catalog_hash != catalog.content_hash:
        raise ValueError("feature manifest does not belong to the selected catalog")
    ordered_specs, facts = _FeatureDependencyResolver(catalog).resolve(manifest.feature_ids)
    return FeatureComputationPlan(manifest, _group_stages(ordered_specs), tuple(item.value for item in facts))


class _FeatureDependencyResolver:
    def __init__(self, catalog: FeatureSpecCatalog) -> None:
        self._catalog = catalog
        self._ordered_specs: list[FeatureSpec] = []
        self._facts: list[FeatureId] = []
        self._known_facts: set[FeatureId] = set()
        self._visited: set[FeatureId] = set()
        self._visiting: set[FeatureId] = set()

    def resolve(self, feature_ids: tuple[FeatureId, ...]) -> tuple[tuple[FeatureSpec, ...], tuple[FeatureId, ...]]:
        for feature_id in feature_ids:
            self._visit(feature_id)
        return tuple(self._ordered_specs), tuple(self._facts)

    def _visit(self, feature_id: FeatureId) -> None:
        if feature_id in self._visited:
            return
        if feature_id in self._visiting:
            raise ValueError("feature catalog contains a dependency cycle")
        self._visiting.add(feature_id)
        spec = self._catalog.require(feature_id)
        for dependency in spec.dependencies:
            self._visit_dependency(dependency)
        self._visiting.remove(feature_id)
        self._visited.add(feature_id)
        self._ordered_specs.append(spec)

    def _visit_dependency(self, dependency: FeatureId) -> None:
        if self._catalog.contains(dependency):
            self._visit(dependency)
        elif dependency not in self._known_facts:
            self._known_facts.add(dependency)
            self._facts.append(dependency)


def _group_stages(ordered_specs: tuple[FeatureSpec, ...]) -> tuple[FeatureComputationStage, ...]:
    group_order: list[str] = []
    grouped: dict[str, list[FeatureSpec]] = {}
    for spec in ordered_specs:
        if spec.calculator_group not in grouped:
            grouped[spec.calculator_group] = []
            group_order.append(spec.calculator_group)
        grouped[spec.calculator_group].append(spec)
    stages = tuple(
        FeatureComputationStage(
            group,
            tuple(spec.feature_id for spec in grouped[group]),
            tuple(dict.fromkeys(dependency for spec in grouped[group] for dependency in spec.dependencies)),
        )
        for group in group_order
    )
    return stages


__all__ = [
    "FeatureComputationPlan",
    "FeatureComputationStage",
    "build_feature_computation_plan",
]
