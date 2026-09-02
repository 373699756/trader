import json
from datetime import date

import pytest

from trader.domain.research.historical_effective_facts import (
    HistoricalEffectiveFactsProbe,
    build_historical_effective_facts_audit,
)
from trader.infra.research.historical_effective_facts import (
    HistoricalEffectiveFactsArtifactConflictError,
    HistoricalEffectiveFactsArtifactStore,
)


def _report():
    return build_historical_effective_facts_audit(
        (HistoricalEffectiveFactsProbe("baostock", date(2018, 1, 1), False, False, False, False),)
    )


def test_effective_facts_artifact_is_idempotent_and_rejects_tampering(tmp_path) -> None:
    store = HistoricalEffectiveFactsArtifactStore(tmp_path)
    report = _report()

    assert store.write(report) == report
    assert store.write(report) == report
    payload_path = tmp_path / "historical-effective-facts-capability.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["status"] = "historical_effective_facts_ready"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HistoricalEffectiveFactsArtifactConflictError, match="schema or hash"):
        store.verify()
