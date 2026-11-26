"""Bind later local recognition of an official risk to its first formal transaction."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from trader.download.domain.company_events import CompanyRiskType
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_reduction_scope import IsCompanyJudicialReductionNotice
from trader.download.domain.company_regulatory_warning import IsCompanyRegulatorWarningNotice
from trader.download.infra.company_facts_protocol import EncodeCompanyTime, RequireCompanyTime
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnection


COMPANY_RISK_ADOPTION = "formal_risk_adoption:"


@dataclass(frozen=True, slots=True)
class CompanyRiskAdoption:
    code: str
    source_key: str
    record: str
    body: bytes
    signature: bytes


@dataclass(frozen=True, slots=True)
class CompanyRiskAdoptionWitness:
    material: CompanyRiskAdoption
    scope: str
    generation: bytes
    content_hash: bytes
    extraction_hash: bytes
    published_at: datetime

    def __post_init__(self) -> None:
        item = self.material
        if (len(self.scope) != 64 or any(c not in "0123456789abcdef" for c in self.scope)
            or any(not isinstance(value, bytes) or len(value) != 32 for value in
                   (item.body, item.signature, self.generation, self.content_hash, self.extraction_hash))
            or self.published_at.utcoffset() is None):
            raise ValueError("company_risk_adoption_witness_invalid")


def CompanyRiskAdoptionWitnessDigest(witnesses: Sequence[CompanyRiskAdoptionWitness]) -> str:
    return CanonicalDigest(tuple((w.material.code, w.material.source_key, w.material.record,
        w.material.body.hex(), w.material.signature.hex(), w.scope, w.generation.hex(), w.content_hash.hex(),
        w.extraction_hash.hex(), w.published_at) for w in witnesses))


def ReadCompanyRiskAdoptionWitnesses(connection: SqliteConnection) -> tuple[CompanyRiskAdoptionWitness, ...]:
    rows = connection.run("""SELECT r.code,r.source_key,r.record_hash,r.body_hash,r.extraction_hash,
        m.scope,m.generation,m.content_hash,m.extraction_hash,m.published_at,r.source,r.outcome,r.published_at
        FROM company_input_receipt r LEFT JOIN company_normalization_commit m ON m.scope=substr(r.policy_id,?)
        WHERE r.policy_id GLOB ? ORDER BY r.source_key,r.code,r.record_hash,r.extraction_hash""",
        (len(COMPANY_RISK_ADOPTION) + 1, COMPANY_RISK_ADOPTION + "*"))
    result = []
    for r in rows:
        if r[5] is None or r[9] != r[12] or r[10] != "cninfo" or r[11] != "field_attempted":
            raise ValueError("company_risk_adoption_confirmation_mismatch")
        material = CompanyRiskAdoption(r[0], r[1], r[2], r[3], r[4])
        result.append(CompanyRiskAdoptionWitness(material, r[5], r[6], r[7], r[8], RequireCompanyTime(r[9])))
    return tuple(result)


def RestoreCompanyRiskAdoptionWitnesses(connection: SqliteConnection, witnesses: Sequence[CompanyRiskAdoptionWitness]) -> None:
    markers = set()
    for w in witnesses:
        item = w.material
        stamp = EncodeCompanyTime(w.published_at)
        markers.add((w.scope, w.generation, w.content_hash, w.extraction_hash, stamp))
        connection.run("INSERT INTO company_input_receipt VALUES(?,?,?,?,?,?,?,?,?)",
            ("cninfo", item.source_key, item.code, item.record, item.signature, item.body, stamp,
             "field_attempted", COMPANY_RISK_ADOPTION + w.scope))
    connection.run_many("INSERT INTO company_normalization_commit VALUES(?,?,?,?,?)", sorted(markers))


def CompanyRiskAdoptionInputs(connection: SqliteConnection, fact: CompanyFacts, extraction_hash: bytes) -> tuple[CompanyRiskAdoption, ...]:
    inputs = []
    for event in fact.risk_events:
        if event.risk_type not in (CompanyRiskType.REGULATORY_WARNING, CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION):
            continue
        for key in event.evidence_ids:
            row = connection.run("""SELECT d.title,d.record_hash,d.body_checked,r.body_hash FROM company_document_link d
                LEFT JOIN company_input_receipt r ON r.code=d.code AND r.source=d.source AND r.source_key=d.evidence_id
                AND r.record_hash=d.record_hash AND r.extraction_hash=? AND r.outcome='valid'
                WHERE d.code=? AND d.source='cninfo' AND d.evidence_id=?""", (extraction_hash, fact.code, key)).fetchone()
            if row is None or not (IsCompanyRegulatorWarningNotice(row[0]) if event.risk_type is CompanyRiskType.REGULATORY_WARNING
                                   else IsCompanyJudicialReductionNotice(row[0])):
                continue
            if not row[2] or not isinstance(row[3], bytes) or len(row[3]) != 32:
                raise ValueError("company_risk_adoption_material_missing")
            signature = bytes.fromhex(CanonicalDigest((COMPANY_RISK_ADOPTION, event, fact.code, key, row[1], row[3].hex())))
            inputs.append(CompanyRiskAdoption(fact.code, key, row[1], row[3], signature))
    return tuple(inputs)


def ReadCompanyRiskAdoptionTime(connection: SqliteConnection, item: CompanyRiskAdoption) -> datetime | None:
    row = connection.run("""SELECT r.body_hash,r.outcome,r.policy_id,r.published_at,m.published_at
        FROM company_input_receipt r LEFT JOIN company_normalization_commit m
        ON m.scope=substr(r.policy_id,?) WHERE r.source='cninfo' AND r.source_key=? AND r.code=?
        AND r.record_hash=? AND r.extraction_hash=?""",
        (len(COMPANY_RISK_ADOPTION) + 1, item.source_key, item.code, item.record, item.signature)).fetchone()
    if row is None:
        return None
    if row[0] != item.body or row[1] != "field_attempted" or not row[2].startswith(COMPANY_RISK_ADOPTION) or row[3] != row[4]:
        raise ValueError("company_risk_adoption_confirmation_mismatch")
    return RequireCompanyTime(row[3])


def ProjectCompanyRiskAdoptions(connection: SqliteConnection, facts: Sequence[CompanyFacts], extraction_hash: bytes) -> tuple[CompanyFacts, ...]:
    result = []
    for fact in facts:
        available = fact.published_at
        for item in CompanyRiskAdoptionInputs(connection, fact, extraction_hash):
            stamp = ReadCompanyRiskAdoptionTime(connection, item)
            if stamp is None:
                raise ValueError("company_risk_adoption_confirmation_missing")
            available = max(available, stamp)
        result.append(fact if available == fact.published_at else replace(fact, published_at=available))
    return tuple(result)
