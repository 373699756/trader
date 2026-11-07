"""Company failure classification and bounded source circuits."""

from __future__ import annotations

import re
from dataclasses import dataclass

from trader.download.domain.company_evidence import (
    CompanyDocumentFailure, CompanySourceFailure, IsTransientCompanyDocumentFailure,
)

@dataclass
class CompanySourceCircuit:
    transport_failures: int = 0
    validation_failures: int = 0

    def success(self) -> None:
        self.transport_failures = self.validation_failures = 0

    def failed(self, exc: BaseException, *, validation: bool = False) -> bool:
        if isinstance(exc, CompanySourceFailure) and exc.category in {"access_denied", "rate_limited"}:
            return True
        if IsCompanyTransportFailure(exc):
            self.transport_failures += 1
            self.validation_failures = 0
        elif validation:
            self.transport_failures = 0
            self.validation_failures += 1
        else:
            self.success()
        return max(self.transport_failures, self.validation_failures) >= 3


def CompanyFailureCode(exc: BaseException) -> str:
    # Suppliers expose bounded categories, never raw URLs, documents or transport error text.
    if isinstance(exc, CompanyDocumentFailure):
        return exc.reason
    if isinstance(exc, CompanySourceFailure):
        if exc.category == "http_error" and exc.status_code is not None:
            return f"http_{exc.status_code}"
        return exc.category
    if isinstance(exc, OSError) and str(exc) in {
        "http_transport_failed", "http_compressed_body_invalid", "http_content_encoding_unsupported",
    }:
        return str(exc)
    if isinstance(exc, (ValueError, TimeoutError, RuntimeError)) and re.fullmatch(
        r"(?:cninfo|company|disclosure|structured|financial|pledge|news)_[a-z_]{1,60}", str(exc)
    ):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "timeout"
    return type(exc).__name__


def IsTransientCompanyFailure(exc: BaseException) -> bool:
    return IsTransientCompanyDocumentFailure(CompanyFailureCode(exc))


def IsCompanyTransportFailure(exc: BaseException) -> bool:
    failure = CompanyFailureCode(exc)
    return failure in {"timeout", "HttpConnectionError", "HttpError"} or failure.startswith("http_")


def CompanyFailureRequiresRetry(failure: str) -> bool:
    scope, _, reason = failure.partition(":")
    return scope != "risk_documents" or reason in {"pending", "source_stopped", "company_raw_capacity_exhausted"} or IsTransientCompanyDocumentFailure(reason)
