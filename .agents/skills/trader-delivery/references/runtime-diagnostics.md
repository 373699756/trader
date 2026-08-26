# Runtime diagnostics

Use the unified read-only entrypoint when one run should scan multiple boundaries:

```bash
.venv/bin/python scripts/diagnose_runtime.py --profile live --output -
```

Profiles:

- `web`, `history`, `tencent`, `tushare`, `browser`, `performance`: run exactly one owning diagnostic module through the unified CLI. Use these after a combined scan has isolated one boundary or when executing its dedicated gate.
- `runtime`: samples the running `/api/v2/status` and Today/Tomorrow/D25 current projections. Use for no-data, funnel, release/schema, projection identity, warmup-state, and restart symptoms.
- `sources`: samples bounded history, Tencent quotes, and configured Tushare daily capability. It performs real supplier requests and consumes applicable quotas.
- `live`: recommended incident profile; runs `runtime` plus all source probes and continues after an individual failure so one report can separate internal pipeline faults from supplier faults.
- `full`: adds the isolated Firefox refresh/patch gate and offline production performance gate. Use for Web delivery, hot-path, release, or explicit full verification; it is not the default response to every defect.

The combined report is `trader-runtime-diagnostics-v1`. It contains per-check status, duration, bounded summaries and findings; it intentionally drops prices, stock-level supplier observations, raw stderr, tokens, and external payloads. `failed` means at least one check could not run or failed its gate; `degraded` means all checks ran but at least one reported a controlled degradation.

Useful options:

```bash
.venv/bin/python scripts/diagnose_runtime.py \
  --profile live \
  --base-url http://127.0.0.1:5000 \
  --runtime-config config/v2/runtime.json \
  --codes 600519 300750 688981 \
  --web-samples 6 \
  --web-interval-seconds 5 \
  --output -
```

Only add `--persistence-runtime-dir /absolute/outside/repository/path` when comparing history transaction behavior. Combined report files must likewise use an explicit repository-external absolute path. Do not create an ad hoc `/tmp` or top-level wrapper script; extend the unified CLI or the owning `scripts/runtime_diagnostics/` module and its tests.

Interpretation order:

1. `web_health` failure with healthy sources: inspect runtime version/release handshake, scheduler/current identity, publication, API/SSE, and browser consumers.
2. Warmup timeout growth with fast `history_sources`: inspect worker waves, queue ownership, cancellation, validation, and persistence, not supplier latency alone.
3. Slow or empty history with healthy Tencent quotes: isolate historical endpoint/adjustment/fallback behavior.
4. Tushare `degraded` with raw daily success: do not treat it as qfq history eligibility; use the reported capability.
5. Browser or performance failure with healthy live data: keep the incident in delivery/render or internal hot-path scope instead of changing supplier and scoring logic.

After source, scheduler, freeze, API, or Web changes, normally restart the real service before the final `runtime`/`live` evidence and confirm that the imported release is current. A fixture-only `full` subcheck does not prove the already-running service loaded new code.
