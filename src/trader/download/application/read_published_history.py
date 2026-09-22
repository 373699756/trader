"""Read-only use case for the active immutable history snapshot."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow


class PublishedHistoryReadPort(Protocol):
    def manifest(self) -> PublishedHistoryManifest | None: ...

    def iter_windows(
        self,
        manifest: PublishedHistoryManifest,
        *,
        sessions: int,
    ) -> Iterator[PublishedHistoryWindow]: ...

    def read_windows(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        *,
        sessions: int,
    ) -> tuple[PublishedHistoryWindow, ...]: ...


@dataclass(frozen=True)
class ReadPublishedHistoryUseCase:
    publication: PublishedHistoryReadPort

    def manifest(self) -> PublishedHistoryManifest | None:
        return self.publication.manifest()

    def iter_windows(
        self,
        manifest: PublishedHistoryManifest,
        *,
        sessions: int,
    ) -> Iterator[PublishedHistoryWindow]:
        return self.publication.iter_windows(manifest, sessions=sessions)

    def read_windows(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        *,
        sessions: int,
    ) -> tuple[PublishedHistoryWindow, ...]:
        return self.publication.read_windows(manifest, codes, sessions=sessions)


__all__ = ["PublishedHistoryReadPort", "ReadPublishedHistoryUseCase"]
