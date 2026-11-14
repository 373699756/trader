"""Read-only recovery evidence for current company failure identities."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy


@dataclass(frozen=True)
class CompanyFailureIdentity:
    source_key: str
    record_hash: str


class CompanyFailureAuditTimeout(TimeoutError):
    """The caller's shared audit deadline expired."""


def ParseCompanyFailureIdentity(diagnostic: str) -> CompanyFailureIdentity | None:
    values: dict[str, str] = {}
    for part in diagnostic.split(" > "):
        key, separator, value = part.partition("=")
        if key not in {"object_id", "revision"}:
            continue
        if not separator or key in values or re.fullmatch(r"[A-Za-z0-9_-]{1,240}", value) is None:
            return None
        values[key] = value
    if set(values) != {"object_id", "revision"}:
        return None
    return CompanyFailureIdentity(values["object_id"], values["revision"])


def AuditCompanyFailureObjects(
    path: Path,
    facts_path: Path,
    events: Sequence[Mapping[str, object]],
    *,
    sqlite_policy: SqliteIoPolicy,
    cancel_requested: Callable[[], bool],
) -> dict[str, object]:
    grouped: Counter[CompanyFailureIdentity] = Counter()
    unidentified = 0
    for event in events:
        if cancel_requested():
            raise CompanyFailureAuditTimeout("company_database_audit_budget_exhausted")
        diagnostic = event.get("diagnostic")
        identity = ParseCompanyFailureIdentity(diagnostic) if isinstance(diagnostic, str) else None
        if event.get("supplier") != "cninfo" or identity is None:
            unidentified += 1
        else:
            grouped[identity] += 1
    objects: list[dict[str, object]] = []
    receipts: list[dict[str, object]] = []
    options = SqliteConnectionOptions(read_only=True, timeout_seconds=5)
    with (
        SqliteSession(path, options, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested) as raw,
        SqliteSession(facts_path, options, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested) as formal,
    ):
        versions = (raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0])
        with SqliteTransaction(raw), SqliteTransaction(formal):
            publication = ReadCompanyPublicationIdentity(formal)
            population = tuple(row[0] for row in raw.run("SELECT code FROM company_population ORDER BY code"))
            population_hash = hashlib.sha256("\n".join(population).encode()).hexdigest()
            for identity, count in sorted(grouped.items(), key=lambda pair: (pair[0].source_key, pair[0].record_hash)):
                if cancel_requested():
                    raise CompanyFailureAuditTimeout("company_database_audit_budget_exhausted")
                row = raw.run(
                    "SELECT id,record_hash,body_hash,body_checked FROM disclosures WHERE source='cninfo' AND source_key=?",
                    (identity.source_key,),
                ).fetchone()
                state = "source_missing"
                if row is not None and row[1] != identity.record_hash:
                    state = "revision_changed"
                elif row is not None:
                    codes = tuple(code[0] for code in raw.run(
                        "SELECT c.code FROM disclosure_codes c JOIN company_population p ON p.code=c.code "
                        "WHERE c.disclosure_id=? ORDER BY c.code", (row[0],),
                    ))
                    state = "outside_population" if not codes else "pending_processing" if not row[3] or row[2] is None else "pending_publication"
                    matched = 0
                    if codes and publication is not None and row[3] and row[2] is not None:
                        for code in codes:
                            receipt = formal.run(
                                "SELECT source,source_key,code,record_hash,outcome,policy_id,published_at FROM company_input_receipt "
                                "WHERE source='cninfo' AND source_key=? AND code=? AND record_hash=? AND extraction_hash=? "
                                "AND body_hash IS ? AND outcome='valid'",
                                (identity.source_key, code, identity.record_hash, publication.extraction_hash, row[2]),
                            ).fetchone()
                            if receipt is not None:
                                receipts.append(dict(zip(
                                    ("source", "source_key", "code", "record_hash", "outcome", "policy", "published_at"),
                                    tuple(receipt), strict=True,
                                )))
                                matched += 1
                        if matched == len(codes):
                            state = "published"
                objects.append({"source_key": identity.source_key, "record_hash": identity.record_hash,
                                "events": count, "state": state})
        stable = versions == (raw.run("PRAGMA data_version").require_one()[0], formal.run("PRAGMA data_version").require_one()[0])
    if not stable:
        for item in objects:
            item["state"] = "unconfirmed"
    return {
        "available": True, "stable_read": stable, "events": len(events), "unidentified_events": unidentified,
        "population_stocks": len(population), "population_hash": population_hash,
        "publication_hash": publication.content_hash.hex() if publication is not None else None,
        "extraction_hash": publication.extraction_hash.hex() if publication is not None else None,
        "object_count": len(objects), "states": dict(Counter(str(item["state"]) for item in objects)),
        "objects": objects, "receipts": receipts,
    }
