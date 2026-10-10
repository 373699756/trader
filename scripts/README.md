# Scripts

`scripts/diagnose_runtime.py` is the only public runtime diagnostic command.
Its profile implementations live under `scripts/runtime_diagnostics/` and
must return bounded, sanitized summaries. A new runtime probe is added as a
module and profile there; it is not added as another top-level script.

## Retained tools

This table is the sole declaration of the eight retained top-level tools.
`check_refactor_quality.py` checks its complete fields, unique inventory and
agreement with the approved script manifest. Declarations describe actual
behavior; they do not grant permission to write active data.

| Tool | Owner | Network | Writes | Output | Resource boundary |
| --- | --- | --- | --- | --- | --- |
| `check_refactor_quality.py` | Repository quality gate | None | None; Ruff runs with `--no-cache` | stdout success; stderr violations/failure | Two sequential Ruff subprocesses, 60 s timeout each; finite repository scan, no hard RSS/total I/O limit |
| `check_tomorrow_training_memory.py` | Training evidence gate; training owns the execution | None | Models/reports/sample data under required `--train-root`; atomic result at `--output`, default `data/historyless/training-memory-result.json` | Result JSON to stdout and result file; progress to stderr | Two sequential full rebuilds; 2 compute threads; default 2048 MiB RSS acceptance threshold measured after execution, not an enforced memory cap; stage durations are recorded, with no artificial total deadline |
| `convert_baostock_history.py` | Legacy history conversion; download owns the target format | BaoStock SDK gap supplementation by default; `--offline` disables it | Source read-only; target control/month databases and conversion state, default `data/history/baostock`; partial/resume and replacement files under maintenance lock | Aggregate JSON to stdout; progress/errors to stderr; errors may include local evidence details | Single conversion process and one monitored SDK child; default batch 256 rows, 8 MiB SQLite cache per connection, 4 MiB hash chunks, 5 ms throttle and 2048 MiB extra free space; no hard RSS/whole-job deadline |
| `diagnose_runtime.py` | Unified diagnostics; owning probes in `runtime_diagnostics/` | Selected Web/vendor profiles use real HTTP; `long-watchlist` reads financial/announcement caches and explicit historical eligibility databases offline; `browser` uses an isolated local fixture; `performance` and `history-sqlite` are offline | Combined report to stdout or external `--output`; optional stock evidence report requires external path; Long audit rejects network and cache writes, reports incomplete eligibility as pending | Sanitized aggregate JSON; child raw output is not forwarded | Sequential children, default 180 s timeout each; Long audit uses at most 8 concurrent stocks; SQLite page samples 1–100, query rounds 1–9, revision samples 1–5000; child capture has no fixed byte/RSS cap |
| `generate_long_watchlist_asset.py` | Packaged Long build asset | None | Without `--check`, writes only `src/trader/web/static/long_watchlist_data.js` from `config/long_watchlist.json`; `--check` is read-only | Stale-asset message to stdout; silent success | One JSON document and one asset; no hard byte/RSS/deadline cap; does not fetch quotes |
| `migrate_runtime_data.py` | Isolated data-layout migration | None | Explicit external source/target/backup paths; `verify` is read-only; build/rollback use sibling lock, staging, previous and failed directories, and may remove previous staging/backup/failed copies | Aggregate manifest/status JSON to stdout | Sequential full tree copy/hash and SQLite integrity scan; hashing reads each file in full; no hard RSS/disk/deadline cap |
| `repack_baostock_history_archive.py` | Explicit history SQLite maintenance; download owns coordination | None | Source/target state under maintenance lock; build/activate/rollback/finalize may replace paths and finalize releases old files; defaults are repository data paths | Aggregate status JSON to stdout; build progress to stderr | Sequential monthly compaction/verification; SQLite busy timeout 30 s; no hard RSS/disk/whole-job deadline |
| `rename_qfq_shards.py` | One-time qfq shard naming migration; download owns the format | None | Default plans only; `--apply` creates hard links, atomically replaces V2/V3 indexes and removes old names under the history maintenance lock; default repository data paths | Aggregate planned/applied/failed JSON to stdout | Sequential two-profile index/shard scan; no hard RSS/disk/whole-job deadline; does not fetch or rewrite price data |
| `verify_wheel_install.py` | Isolated installed-wheel release gate | Local wheel install with `--no-deps` and version check disabled; no supplier requests; pip index access is not explicitly disabled | Temporary external virtualenv and dependency `.pth`, removed on exit; input wheel/config/resources read-only | Aggregate verification JSON to stdout; exceptions can produce stderr traceback | One wheel and sequential CLI/resource checks; subprocesses and virtualenv creation have no timeout; no hard RSS/disk cap |

Repeated download, training, scoring, or status workflows belong to
`trader-cli` or a business `entrypoints/commands.py`, not to this directory.
Every retained or newly added tool must have a bounded input/output contract,
an explicit network/write declaration, and an entry in this table before it is
used by a gate or documented command. Missing limits must be stated explicitly;
an RSS acceptance threshold, SQLite busy timeout or HTTP socket timeout must not
be described as a hard whole-job resource bound. Legacy conversion and repack
do not enforce repository-external destinations; during refactor acceptance,
pass explicit isolated external paths. For training evidence, also explicitly
set external `--history-root`, `--train-root` and `--output`.

Makefile checks all of `src/trader`, `tests` and `scripts`, including internal
probes and migration tools. Strict source debt is anchored to rule, relative file
and qualified function, so unchanged aggregate counts cannot hide relocated
violations. Changing a debt owner requires diff review and an explicit baseline
update. This gate does not prove the accuracy of declarations or enforce the
runtime limits of every tool; review and isolated execution evidence remain
necessary.

## BaoStock rate experiments

Use the existing public diagnostic entrypoint for one-session measurements:

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile baostock-concurrency \
  --baostock-serial-only --baostock-intervals 2 1.5 1 --baostock-sizes 10 \
  --baostock-rounds 3 --history-days 400 --source-timeout-seconds 15 \
  --command-timeout-seconds 450 --output /absolute/outside/repository/rate-screen.json
```

The serial experiment uses the production start-to-start limiter, one active SDK
session and no retries. Sizes are bounded to 1–100, intervals to at most three
unique values of at least one second, and repetitions to 1–3. The socket default
timeout applies only inside the diagnostic process and is restored afterwards;
the public child-command deadline is the hard whole-experiment limit. A failed
experiment stops the remaining rate/size/round matrix. Empty responses, invalid dates,
and duplicate or mismatched raw/qfq dates fail the experiment. This does not verify
every expected trading date or price value. Auto-discovery selects active
A-shares across main, growth and STAR boards; explicit `--codes` fix a comparison
population when at least the largest requested size is supplied.

Reports include interval, repetition, raw/qfq row counts, bounded error categories,
elapsed time and successful stocks per minute. They contain no stock-level prices
or external payloads, never access the history archive/checkpoints, and always
retain `production_eligible=false`. Throughput measures supplier reads only;
neither a small successful sample nor zero observed errors proves full-market
stability, retry/resume behavior or safe qfq request skipping. Production parameters
are not changed by this command. Compare promising intervals with
`--baostock-sizes 50 100 --baostock-rounds 3` before selecting a deployment candidate.

## Research commands

Research execution belongs to `trader-cli`; the four former standalone research
scripts have been removed. Pass `--config /absolute/path/config/runtime.json`
before the subcommand. Dependency construction and HTTP session lifetime belong
to `bootstrap.py`; the use cases live under `training/application/`.

| Command | Network | Writes and output | Resource boundary |
| --- | --- | --- | --- |
| `research-h1-capability` | Tencent and Eastmoney sample requests | Reads `--h1-runtime-dir`; seals existing H1 formats into explicit external `--artifact-dir`; summary to stdout or external `--output` | One code, two attempts, no retries/redirects/proxy fallback; default 8 s per request, maximum 60 s; deadline from first request at twice that timeout, checked between chunks with finite socket timeouts; 4 MiB per response; two strategies |
| `research-data-qualification` | Same two sample requests | Explicit `--history-root` read-only; summary only; no archive download/write | Same HTTP limits; archive validation and industry audit scan the full existing snapshot |
| `research-industry-audit` | None; `--tushare-access-points` is metadata | Explicit `--history-root` read-only; stdout aggregation or external report; details require external `--output` | One sequential full-snapshot audit; sample count is an eligibility threshold, not a scan cap |
| `research-terminal-holdout` | None | Reads external `--parent-artifact-dir`; writes immutable Tomorrow/D25/conclusion reports into separate external `--output-dir`; summary to stdout or external `--output` | Two strategy evaluations; preserves insufficient-parent closure, hash verification, conflict and repeat-run semantics |

Exit codes: `0` qualified/validated, `1` insufficient evidence, `2` invalid
arguments/evidence or execution/output failure. Execution failures emit a bounded
error report to stdout, including when the requested report path cannot be written;
the failed destination is never retried. Summary paths must be separate from
evidence input and artifact directories. The capability closure produces
only insufficient evidence, so its normal exit code remains `1`. No command
grants production authority. Ordinary CLI help/config checks do not load these
research implementations. The HTTP deadline is checked during transport; socket
timeouts and deadline checks do not guarantee a hard wall-clock limit. Full archive
audits and report I/O have no hard RSS or total offline execution limit.
Use isolated external copies for refactor acceptance, not active data.

History SQLite performance is available through the unified read-only diagnostic:

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile history-sqlite --output -
```

It reads monthly SQLite headers, samples page classes, benchmarks latest-row
queries, and writes a bounded revision fixture only to a disposable temporary
database. It does not compact or switch the active history database. Maintenance rules
are in [the architecture contract](../docs/项目重构详细.md), section 3.5.1.1;
training performance is explained in [the training guide](../docs/04_策略回溯.md),
section 12.4. The repack tool remains a separate explicit maintenance operation;
its default paths do not authorize active-data changes. During refactoring,
rehearse only on explicit repository-external copies as required by section 10.5
of the architecture contract.

Test commands and fast/full boundaries are defined by Makefile and documented
in the architecture contract, section 10.2.1. Default `make test` runs the fast
set; release acceptance also requires `make test-full`. Work status and pending
optimizations are recorded only in `docs/03_工程实施.md`.
