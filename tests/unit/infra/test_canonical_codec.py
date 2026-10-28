"""Fixed identities measured from the pre-migration implementations (dd15c15d)."""

from datetime import date

from tests.unit.domain.test_freeze_identity import freeze
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.training.domain.evaluation.research_identity import CanonicalResearchHash
from trader.training.domain.evaluation.specification import HISTORICAL_RESEARCH_SPEC


def test_canonical_codec_has_stable_download_research_and_freeze_identities():
    assert (
        CanonicalDigest({"code": "600001", "day": date(2026, 8, 11), "name": "审计", "bars": ((10.0, 12.0),)})
        == "73d65693a285e632233a5492d3ec00ee7b23ec8a150542030018597bc8da939f"
    )
    assert (
        CanonicalResearchHash(HISTORICAL_RESEARCH_SPEC)
        == "271148e641b6ff6199181de70bd41ab4869477ac52fb97cf348d8a6747142926"
    )
    assert freeze().content_hash == "2725ee6b66927deeb9dcdffaf9420e077d73656821586bb70b3bb32c08aea7d3"
