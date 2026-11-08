"""Semantic identity of price history grouped by one frozen current industry cut."""

from trader.infra.serialization.canonical_codec import CanonicalDigest

TRAINING_INDUSTRY_POLICY = "latest_snapshot"


def BuildTrainingInputHash(price_snapshot_hash: str, industry_content_hash: str) -> str:
    if any(
        len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        for value in (price_snapshot_hash, industry_content_hash)
    ):
        raise ValueError("training_input_identity_invalid")
    return CanonicalDigest((TRAINING_INDUSTRY_POLICY, price_snapshot_hash, industry_content_hash))


__all__ = ["BuildTrainingInputHash", "TRAINING_INDUSTRY_POLICY"]
