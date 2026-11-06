"""Bounded company batches preserve checkpoints, document identity and publications."""

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_evidence import DisclosurePage

NOW = fixtures.NOW


def _seed(index, count=4):
    partition = next(p for p in index.partitions(("sse",), NOW) if p.start == NOW.date())
    items = tuple(fixtures._disclosure(str(i), "关于收到立案告知书的公告") for i in range(count))
    index.commit_page(partition, DisclosurePage(items, count, False), NOW)
    return items
