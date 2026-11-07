"""Page observations preserve the real OCR execution and terminal boundaries."""

import pytest
from dataclasses import replace

from trader.download.application.company_operations import CompanyOperations
from tests.helpers.download.company.company_evidence import _disclosure


@pytest.mark.parametrize("failure", [False, True])
def test_page_progress_keeps_operation_age_and_preserves_last_real_page_on_failure(failure):
    clock = [1.0]
    messages = []
    operations = CompanyOperations(messages.append, lambda: clock[0], (), population=("600001",))

    def recognize(report):
        report(0, 3)
        clock[0] = 4
        report(1, 3)
        clock[0] = 7
        if failure:
            raise ValueError("fixture_page_failed")
        report(3, 3)
        return "recognized"

    future = operations.submit_pages(recognize, None, document=_disclosure())
    if failure:
        with pytest.raises(ValueError, match="fixture_page_failed"):
            future.result()
    else:
        assert future.result() == "recognized"
    events = [message.operation for message in messages]
    assert all(event.at == 1 for event in events[:-1])
    assert events[-1].at == 7
    assert events[-1].state == ("failed" if failure else "completed")
    assert (events[-1].completed_units, events[-1].total_units, events[-1].unit) == (
        1 if failure else 3, 3, "pages",
    )
    assert len({event.token for event in events}) == 1


@pytest.mark.parametrize("population,expected", [
    (("600001", "600002"), ("600001", "600002")),
    (("600002",), ("600002",)),
    ((), ()),
])
def test_shared_announcement_only_reports_its_related_eligible_stocks(population, expected):
    messages = []
    operations = CompanyOperations(messages.append, lambda: 0, (), population=population)
    document = replace(_disclosure(), codes=("600003", "600002", "600001", "600002"))
    result = operations.submit_pages(lambda report: "shared", None, document=document)
    assert result.result() == "shared"
    assert {event.operation.related_codes for event in messages} == {expected}
    assert all("600003" not in event.operation.related_codes for event in messages)


def test_display_names_do_not_expand_the_operation_population():
    messages = []
    operations = CompanyOperations(messages.append, lambda: 0, (("600003", "显示名称"),), population=("600002",))
    event = operations.queued("download", document=replace(_disclosure(), codes=("600003", "600002")))
    assert event.related_codes == ("600002",)
