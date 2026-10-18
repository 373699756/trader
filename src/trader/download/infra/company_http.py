"""Shared, bounded company source HTTP error taxonomy."""

from __future__ import annotations

import requests

from trader.download.domain.company_evidence import CompanySourceFailure


def CheckCompanyResponse(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        if response.status_code == 403:
            raise CompanySourceFailure("access_denied") from exc
        if response.status_code == 429:
            raise CompanySourceFailure("rate_limited") from exc
        raise CompanySourceFailure("http_error") from exc


__all__ = ["CheckCompanyResponse"]
