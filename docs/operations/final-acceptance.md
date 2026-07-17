# Final Acceptance Evidence

Contract: `docs/need.md` section 25

This checklist separates repeatable repository gates from evidence that can
only be collected during a real A-share trading day. A release is not accepted
while any external row remains pending.

| Acceptance criterion | Repeatable evidence | Release status |
| --- | --- | --- |
| Active TopK quote P95 age is at most 10 seconds | `/api/status` exposes `dependencies.market_data.topk_quote_age` overall and per active strategy; integration tests cover pass/fail semantics. | pending live-day evidence |
| Fixed 68/32 fusion and 83.40 vector | Domain fusion and settings tests. | repository gate complete |
| Configured DeepSeek with eligible candidates makes a physical call | Pipeline/component tests assert a real client attempt and atomic budget record; status exposes `physical_call_acceptance`. | pending real-key smoke |
| DeepSeek stage outcome or explicit miss reasons | Status records candidates, phase, batch result, error, budget statuses and zero-call reason. | pending live-day summary |
| Frozen input reproduces filtering, scoring, risks, veto and ranking | New freezes embed the full-market preselection inputs, targeted inputs and validated reviews; `trader-cli verify-freeze` performs deterministic replay. | repository gate complete for new freezes |
| Data-source or DeepSeek failure does not block local/Web | Market, pipeline, fusion and read-only Web degradation tests. | repository gate complete |
| Late events cannot mutate freezes and manifest hash matches | Timeline, persistence recovery/conflict and recorded full-day shadow tests. | repository gate complete |
| Desktop UI has no blank page, overlap, overflow or visible jitter | Browser acceptance at 1280x720, 1440x900 and 1920x1080. | pending release screenshots |
| Only `src/trader` contains active product code | Architecture AST/root-layout and tracked-artifact contracts. | repository gate complete |
| External wheel install provides server, CLI and assets | `make package` followed by repository-external install/import/entrypoint/resource smoke. | rerun for every release |
| All quality, concurrency, build and UI gates pass | `make format-check`, `make lint`, `make type-check`, `make test`, `make package`, external wheel and browser evidence. | rerun for every release |

For each frozen strategy, run:

```bash
trader-cli --config /absolute/path/to/config/v2/runtime.json \
  verify-freeze --snapshot /absolute/path/to/.runtime/v2/frozen/today/YYYY-MM-DD/SNAPSHOT.json
```

The command is read-only and replays the frozen policy, full-market preselection generation,
requested-code order, targeted features and validated reviews without loading
the current configuration, opening SQLite or making network calls. It must
return `status=verified` and non-zero market/candidate input counts. Legacy v2
snapshots created before replay inputs were introduced remain readable but
cannot satisfy the final replay gate.
