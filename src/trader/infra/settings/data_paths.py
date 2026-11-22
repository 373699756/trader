"""Immutable data layout resolved at the configuration boundary, without I/O."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DataPaths:
    root: Path

    @property
    def history(self) -> Path:
        return self.root / "market/history"

    @property
    def reference(self) -> Path:
        return self.root / "market/reference"

    @property
    def reference_database(self) -> Path:
        return self.reference / "company_st.sqlite3"

    @property
    def company(self) -> Path:
        return self.root / "company/company_published.sqlite3"

    @property
    def company_sources(self) -> Path:
        return self.root / "company/company_sources.sqlite3"

    @property
    def company_raw(self) -> Path:
        return self.root / "company/raw"

    @property
    def company_documents(self) -> Path:
        return self.company_raw / "text"

    @property
    def retired_company_databases(self) -> tuple[Path, ...]:
        return (
            self.root / "company/company_facts.sqlite3",
            self.root / "company/company_normalized.sqlite3",
            self.root / "company/company_inputs.sqlite3",
            self.company_raw / "company_collection.sqlite3",
        )

    @property
    def company_sync(self) -> Path:
        return self.root / "company/company_sync.sqlite3"

    @property
    def models(self) -> Path:
        return self.root / "training/models"

    @property
    def samples(self) -> Path:
        return self.root / "training/samples"

    @property
    def training_maintenance(self) -> Path:
        return self.root / "training/maintenance.sqlite3"

    def recommendation(self, profile: str) -> Path:
        _validate_profile(profile)
        return self.root / "recommendation" / profile

    def research(self, profile: str) -> Path:
        _validate_profile(profile)
        return self.root / "research" / profile


def _validate_profile(profile: str) -> None:
    if profile not in ("v1", "v2", "v3", "v5"):
        raise ValueError("data_profile_invalid")


__all__ = ["DataPaths"]
