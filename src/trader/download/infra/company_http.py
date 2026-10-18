"""Shared, bounded company source HTTP error taxonomy."""

from __future__ import annotations

from trader.download.domain.company_evidence import CompanySourceFailure
from trader.infra.http_contracts import HttpResponse, HttpStatusError


def CheckCompanyResponse(response: HttpResponse) -> None:
    try:
        response.raise_for_status()
    except HttpStatusError as exc:
        if response.status_code == 403:
            raise CompanySourceFailure("access_denied") from exc
        if response.status_code == 429:
            raise CompanySourceFailure("rate_limited") from exc
        raise CompanySourceFailure("http_error") from exc


__all__ = ["CheckCompanyResponse"]
