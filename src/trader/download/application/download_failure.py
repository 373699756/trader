"""Explicit failure observation port; recording never determines retry eligibility."""

from collections.abc import Callable

from trader.download.domain.download_failure import DownloadFailureContext

DownloadFailurePort = Callable[[DownloadFailureContext, BaseException], None]
