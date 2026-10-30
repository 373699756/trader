"""Read-only use case for the active immutable history snapshot."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow


class PublishedHistoryReadPort(Protocol):
    def manifest(self) -> PublishedHistoryManifest | None: ...

    def read_range(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        start: date,
        end: date,
        *,
        check: Callable[[], None] | None = None,
    ) -> tuple[PublishedHistoryWindow, ...]: ...

    def iter_windows(
        self,
        manifest: PublishedHistoryManifest,
        *,
        sessions: int,
        check: Callable[[], None] | None = None,
    ) -> Iterator[PublishedHistoryWindow]: ...

    def read_windows(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        *,
        sessions: int,
        check: Callable[[], None] | None = None,
    ) -> tuple[PublishedHistoryWindow, ...]: ...


@dataclass(frozen=True)
class ReadPublishedHistoryUseCase:
    publication: PublishedHistoryReadPort

    def manifest(self) -> PublishedHistoryManifest | None:
        return self.publication.manifest()

    def read_range(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        start: date,
        end: date,
        *,
        check: Callable[[], None] | None = None,
    ) -> tuple[PublishedHistoryWindow, ...]:
        return self.publication.read_range(manifest, codes, start, end, check=check)

    def iter_windows(
        self,
        manifest: PublishedHistoryManifest,
        *,
        sessions: int,
        check: Callable[[], None] | None = None,
    ) -> Iterator[PublishedHistoryWindow]:
        return self.publication.iter_windows(manifest, sessions=sessions, check=check)

    def read_windows(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        *,
        sessions: int,
        check: Callable[[], None] | None = None,
    ) -> tuple[PublishedHistoryWindow, ...]:
        return self.publication.read_windows(manifest, codes, sessions=sessions, check=check)


__all__ = ["PublishedHistoryReadPort", "ReadPublishedHistoryUseCase"]
