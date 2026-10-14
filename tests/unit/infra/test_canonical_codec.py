"""Fixed identities measured from the pre-migration implementations (dd15c15d)."""

from datetime import date

from tests.unit.domain.test_decision_identity import decision
from trader.infra.serialization.canonical_codec import canonical_digest
from trader.training.domain.evaluation.research_identity import canonical_research_hash
from trader.training.domain.evaluation.specification import HISTORICAL_RESEARCH_SPEC


def test_codec_migration_preserves_download_research_and_published_decision_identities():
    assert (
        canonical_digest({"code": "600001", "day": date(2026, 8, 11), "name": "审计", "bars": ((10.0, 12.0),)})
        == "73d65693a285e632233a5492d3ec00ee7b23ec8a150542030018597bc8da939f"
    )
    assert (
        canonical_research_hash(HISTORICAL_RESEARCH_SPEC)
        == "271148e641b6ff6199181de70bd41ab4869477ac52fb97cf348d8a6747142926"
    )
    assert decision().content_hash == "835482fe5f80bb015b562d6853ff16513997a8e7e0303d811d4a39b8b17000c8"
