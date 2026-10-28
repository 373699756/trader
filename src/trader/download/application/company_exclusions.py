"""Explicit write use case for confirmed non-ST exclusion observations."""

from collections.abc import Sequence
from typing import Protocol

from trader.download.domain.company_exclusions import CompanyExclusion


class CompanyExclusionPort(Protocol):
    def record_exclusions(self, facts: Sequence[CompanyExclusion]) -> int: ...

    def read_exclusions(self) -> tuple[CompanyExclusion, ...]: ...


class PublishCompanyExclusions:
    def __init__(self, index: CompanyExclusionPort) -> None:
        self._index = index

    def execute(self, facts: Sequence[CompanyExclusion]) -> int:
        return self._index.record_exclusions(tuple(facts))

    def read(self) -> tuple[CompanyExclusion, ...]:
        return self._index.read_exclusions()


__all__ = ["CompanyExclusionPort", "PublishCompanyExclusions"]
