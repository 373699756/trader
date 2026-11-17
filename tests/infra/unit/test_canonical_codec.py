"""Current content identities include real input provenance without hash exceptions."""

from dataclasses import dataclass, field, replace

from trader.infra.serialization.canonical_codec import CanonicalDigest, EncodeCanonicalValue


@dataclass(frozen=True)
class _ResearchInput:
    measured_value: float
    research_identity: str = field(metadata={"exclude_from_legacy_hash": True})
    spec_hash: str = field(metadata={"exclude_from_legacy_hash": True})
    derived_hash: str = field(init=False, default="derived")


def test_research_identity_and_spec_content_cannot_be_excluded_from_current_hash() -> None:
    original = _ResearchInput(2.5, "research", "a" * 64)
    identity_changed = replace(original, research_identity="another_research")
    spec_changed = replace(original, spec_hash="b" * 64)
    assert len({CanonicalDigest(original), CanonicalDigest(identity_changed), CanonicalDigest(spec_changed)}) == 3
    assert EncodeCanonicalValue(original) == {
        "measured_value": 2.5, "research_identity": "research", "spec_hash": "a" * 64,
    }


def test_derived_fields_remain_an_explicit_encoding_choice() -> None:
    original = _ResearchInput(2.5, "research", "a" * 64)
    assert "derived_hash" not in EncodeCanonicalValue(original)
    assert EncodeCanonicalValue(original, include_derived=True) == {
        "measured_value": 2.5, "research_identity": "research", "spec_hash": "a" * 64, "derived_hash": "derived",
    }
    assert CanonicalDigest(original) != CanonicalDigest(original, include_derived=True)
