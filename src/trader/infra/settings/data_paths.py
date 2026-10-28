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
    def daily_window(self) -> Path:
        return self.root / "market/daily_window.sqlite3"

    @property
    def reference(self) -> Path:
        return self.root / "market/reference"

    @property
    def reference_database(self) -> Path:
        return self.reference / "company_st.sqlite3"

    @property
    def company(self) -> Path:
        return self.root / "company/company_facts.sqlite3"

    @property
    def company_collection(self) -> Path:
        return self.root / "company/raw/company_collection.sqlite3"

    @property
    def company_documents(self) -> Path:
        return self.root / "company/raw/text"

    @property
    def models(self) -> Path:
        return self.root / "training/models"

    @property
    def samples(self) -> Path:
        return self.root / "training/samples"

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
