"""Progress follows committed work without extending the download plan."""

from datetime import timedelta

import pytest

from tests.helpers.download.company.company_evidence import NOW
from trader.download.domain.company_evidence import (
    CompanyFinancial,
    CompanyOperationProgress,
)


def _financial(code, disclosed):
    return CompanyFinancial(code, NOW.date() - timedelta(days=30), disclosed, 1, 2, 3, 4, 5, 6, 7, 8)














@pytest.mark.parametrize("counts", ((None, 3, "pages"), (2, 1, "pages"), (True, 2, "pages"), (1, 2, None)))
def test_operation_units_reject_partial_or_invalid_counts(counts):
    with pytest.raises(ValueError, match="operation_units"):
        CompanyOperationProgress(1, "ocr", "running", 0, completed_units=counts[0], total_units=counts[1], unit=counts[2])


@pytest.mark.parametrize("codes", (("600001", "600001"), ("600002", "600001"), ("invalid",)))
def test_operation_related_codes_require_ordered_unique_stock_codes(codes):
    with pytest.raises(ValueError):
        CompanyOperationProgress(1, "download", "completed", 0, related_codes=codes)
