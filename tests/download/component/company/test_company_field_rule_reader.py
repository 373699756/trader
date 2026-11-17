"""Old field proofs are rejected without losing independently published risk facts."""

import time
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company_resources import SQLiteCompanyEvidenceReaderFixture
from tests.helpers.recommendation.company_research import PublishedCompanyResearchFixture
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.infra.sqlite_connection import SqliteSession


@pytest.mark.parametrize("marker", (None, b"x" * 32))
def test_old_or_missing_global_rule_marker_never_relabels_fields_or_hides_existing_risks(tmp_path, marker):
    index = fixtures._index(tmp_path)
    case = fixtures._disclosure("case", "关于收到立案告知书的公告")
    repurchase = fixtures._disclosure("repurchase", "首次回购股份公告")
    partition = DisclosurePartition("sse", fixtures.NOW.date(), fixtures.NOW.date())
    index.commit_page(partition, DisclosurePage((case, repurchase), 2, False), fixtures.NOW)
    index.commit_document(case, fixtures._extraction("本公司收到证监会立案告知书，因涉嫌信息披露违法违规被立案。"), fixtures.NOW)
    index.commit_document(repurchase, fixtures._extraction(
        "证券代码：600001\n本公司首次回购股份，占公司总股本的0.1%，回购股份全部用于注销。"
    ), fixtures.NOW)
    index.publish(("600001",), fixtures.NOW)
    reader = SQLiteCompanyEvidenceReaderFixture(index.facts_path)
    before = reader.read(("600001",))
    fields = reader.read_fields(("600001",))
    assert before[0].risk_events and fields.results
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    index._facts.publish((), {}, parent, fixtures.NOW + timedelta(seconds=1),
                         field_results=fields.results, field_rules_hash=marker)
    with pytest.raises(ValueError, match="company_field_rules_unreproved"):
        reader.read_fields(("600001",))
    assert reader.read(("600001",)) == before
    with SqliteSession(index.facts_path) as formal:
        assert ReadCompanyFieldResults(formal, ("600001",)) == fields.results
    research = PublishedCompanyResearchFixture(
        reader, wall_clock=lambda: fixtures.NOW + timedelta(seconds=1), open_dates=lambda: (fixtures.NOW.date(),),
        monotonic=time.monotonic,
    )
    research.initialize(("600001",), cancelled=lambda: False)
    observation = research.cached(("600001",), include_structured=True)["600001"]
    assert len(observation.corporate_risk_facts) == 1
