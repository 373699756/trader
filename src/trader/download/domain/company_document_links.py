"""Immutable evidence metadata published with the company fact identity."""

from __future__ import annotations

from dataclasses import dataclass

from trader.download.domain.company_events import CompanyEvidenceLink


@dataclass(frozen=True, slots=True)
class CompanyPublishedEvidenceLink:
    code: str
    source: str
    record_hash: str
    link: CompanyEvidenceLink

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isascii() or not self.code.isdigit():
            raise ValueError("company_document_link_code_invalid")
        if not self.source or len(self.source) > 80 or self.source != self.source.strip():
            raise ValueError("company_document_link_source_invalid")
        if len(self.record_hash) != 64 or any(char not in "0123456789abcdef" for char in self.record_hash):
            raise ValueError("company_document_link_record_hash_invalid")
        link = self.link
        if (
            not link.evidence_id or len(link.evidence_id) > 240 or len(link.title) > 4000
            or len(link.url) > 8192 or link.published_at.utcoffset() is None
            or type(link.body_checked) is not bool
            or any(ord(char) < 32 for char in link.evidence_id + link.title + link.url)
        ):
            raise ValueError("company_document_link_metadata_invalid")


@dataclass(frozen=True, slots=True)
class CompanyDocumentLinkSnapshot:
    publication_hash: str
    links: tuple[CompanyPublishedEvidenceLink, ...]

    def __post_init__(self) -> None:
        if (
            self.publication_hash and (
                len(self.publication_hash) != 64
                or any(char not in "0123456789abcdef" for char in self.publication_hash)
            )
            or not isinstance(self.links, tuple) or len(self.links) > 120_000
            or not self.publication_hash and self.links
        ):
            raise ValueError("company_document_link_snapshot_invalid")
