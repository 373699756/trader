"""Explicit artifact projection and decoding of the adopted current-industry input."""

from collections.abc import Mapping
from datetime import date, datetime
from typing import cast

from trader.download.domain.current_industry import (
    CurrentIndustryGap, CurrentIndustryMember, CurrentIndustryReference, CurrentIndustrySnapshot,
)
from trader.infra.serialization.fields import IsSha256Text
from trader.training.domain.training_input_identity import BuildTrainingInputHash, TRAINING_INDUSTRY_POLICY

_SNAPSHOT_FIELDS = {
    "members", "gaps", "observed_at", "refresh_after", "source", "classification", "publication_hash", "content_hash",
}
_REFERENCE_FIELDS = {"industry_id", "classification", "effective_date", "source", "data_version"}


def ProjectTrainingIndustrySnapshot(snapshot: CurrentIndustrySnapshot) -> dict[str, object]:
    return {
        "members": [{
            "code": member.code,
            "name": member.name,
            "reference": {
                "industry_id": member.reference.industry_id,
                "classification": member.reference.classification,
                "effective_date": member.reference.effective_date.isoformat(),
                "source": member.reference.source,
                "data_version": member.reference.data_version,
            },
        } for member in snapshot.members],
        "gaps": [{"code": gap.code, "reason": gap.reason} for gap in snapshot.gaps],
        "observed_at": snapshot.observed_at.isoformat(),
        "refresh_after": snapshot.refresh_after.isoformat(),
        "source": snapshot.source,
        "classification": snapshot.classification,
        "publication_hash": snapshot.publication_hash,
        "content_hash": snapshot.content_hash,
    }


def ParseTrainingIndustrySnapshot(value: object) -> CurrentIndustrySnapshot:
    raw = _object(value, _SNAPSHOT_FIELDS)
    members = []
    for value in _rows(raw["members"]):
        member = _object(value, {"code", "name", "reference"})
        reference = _object(member["reference"], _REFERENCE_FIELDS)
        members.append(CurrentIndustryMember(
            _text(member, "code"),
            CurrentIndustryReference(
                _text(reference, "industry_id"), _text(reference, "classification"),
                date.fromisoformat(_text(reference, "effective_date")),
                _text(reference, "source"), _text(reference, "data_version"),
            ),
            _text(member, "name"),
        ))
    gaps = []
    for value in _rows(raw["gaps"]):
        gap = _object(value, {"code", "reason"})
        gaps.append(CurrentIndustryGap(_text(gap, "code"), _text(gap, "reason")))
    snapshot = CurrentIndustrySnapshot(
        tuple(members), tuple(gaps),
        datetime.fromisoformat(_text(raw, "observed_at")), datetime.fromisoformat(_text(raw, "refresh_after")),
        _text(raw, "source"), _text(raw, "classification"), _text(raw, "publication_hash"),
    )
    if snapshot.content_hash != _text(raw, "content_hash"):
        raise ValueError("training_industry_content_hash_mismatch")
    return snapshot


def ValidateTrainingInputIdentity(payload: Mapping[str, object]) -> None:
    price_hash, industry_hash, input_hash = (payload.get(key) for key in (
        "price_snapshot_hash", "industry_content_hash", "training_input_hash",
    ))
    if (
        payload.get("industry_policy") != TRAINING_INDUSTRY_POLICY
        or not IsSha256Text(price_hash) or not IsSha256Text(industry_hash) or not IsSha256Text(input_hash)
        or BuildTrainingInputHash(price_hash, industry_hash) != input_hash
    ):
        raise ValueError("training_input_industry_identity_mismatch")


def _object(value: object, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("training_industry_metadata_fields_invalid")
    return cast(dict[str, object], value)


def _text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise ValueError("training_industry_metadata_text_invalid")
    return result


def _rows(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) > 12_000:
        raise ValueError("training_industry_metadata_population_invalid")
    return cast(list[object], value)


__all__ = ["ParseTrainingIndustrySnapshot", "ProjectTrainingIndustrySnapshot", "ValidateTrainingInputIdentity"]
