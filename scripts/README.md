# Scripts

`scripts/diagnose_runtime.py` is the only public runtime diagnostic command.
Its profile implementations live under `scripts/runtime_diagnostics/` and
must return bounded, sanitized summaries. A new runtime probe is added as a
module and profile there; it is not added as another top-level script.

`--profile download-performance` runs three rounds on disposable synthetic SQLite databases outside the repository.
It reports company write/publication/cold and warm reads, unchanged publication, and equivalent per-stock versus batch gap queries.
It makes zero HTTP requests and cannot establish full-market supplier download speed.

## Retained tools

This table is the sole declaration of the eight retained top-level tools.
`check_refactor_quality.py` checks its complete fields, unique inventory and
agreement with the approved script manifest. Declarations describe actual
behavior; they do not grant permission to write active data.

| Tool | Owner | Network | Writes | Output | Resource boundary |
| --- | --- | --- | --- | --- | --- |
| `check_refactor_quality.py` | Repository quality gate | None | None; Ruff runs with `--no-cache` | stdout success; stderr violations/failure | Two sequential Ruff subprocesses, 60 s timeout each; finite repository scan, no hard RSS/total I/O limit |
| `check_tomorrow_training_memory.py` | Training evidence gate; training owns the execution | None | Models/reports/sample data under required `--train-root`; atomic result at `--output`, default `data/historyless/training-memory-result.json` | Result JSON to stdout and result file; progress to stderr | Two sequential full rebuilds; 2 compute threads; default 2048 MiB RSS acceptance threshold measured after execution, not an enforced memory cap; stage durations are recorded, with no artificial total deadline |
| `diagnose_runtime.py` | Unified diagnostics; owning probes in `runtime_diagnostics/` | Selected Web/vendor profiles use real HTTP; `market-factor-compare` only reads local qfq/industry databases plus explicitly selected live sources; `long-watchlist` reads financial/announcement caches and explicit historical eligibility databases offline; `browser` uses an isolated local fixture; `performance`, `history-sqlite` and `qfq-sqlite` are offline | Combined report to stdout or external `--output`; history revision benchmarks and qfq integrity checks write only disposable system-temporary targets, while active qfq data remains read-only; optional stock evidence report requires external path; Long audit rejects network and cache writes | Sanitized aggregate JSON; market-factor profile defaults to readable terminal text; child raw output and stock identities are not forwarded | Sequential children, default 180 s timeout each; market-factor per-source test loop at most 120 s and whole task at most 600 s; Long audit uses at most 8 concurrent stocks; SQLite page samples 1–100, query rounds 1–9, revision samples 1–5000; qfq copies and scans at most eight bounded board shards and emits aggregate counts; child capture has no fixed byte/RSS cap |
| `generate_long_watchlist_asset.py` | Packaged Long build asset | None | Without `--check`, writes only `src/trader/web/static/long_watchlist_data.js` from `config/long_watchlist.json`; `--check` is read-only | Stale-asset message to stdout; silent success | One JSON document and one asset; no hard byte/RSS/deadline cap; does not fetch quotes |
| `migrate_runtime_data.py` | Isolated data-layout migration | None | Explicit external source/target/backup paths; `build-references` converts a copied eligibility database; `build-freeze` flattens copied V2/V3 daily record paths without changing JSON bytes and verifies identities/checkpoints; `verify` is read-only; build/rollback use sibling lock, staging, previous and failed directories, and may remove previous staging/backup/failed copies | Aggregate manifest/status JSON to stdout | Sequential full tree copy/hash and SQLite integrity scan; hashing reads each file in full; no hard RSS/disk/deadline cap; freeze conversion rejects uncheckpointed journals, pending/corrupt records and orphaned record files, and cannot activate repository data |
| `repack_history_partitions.py` | Explicit history SQLite maintenance; download owns coordination | None | Source/target state under maintenance lock; build/activate/rollback/finalize may replace paths and finalize releases old files; defaults are repository data paths | Aggregate status JSON to stdout; build progress to stderr | Sequential monthly compaction/verification; SQLite busy timeout 30 s; no hard RSS/disk/whole-job deadline |
| `rename_qfq_shards.py` | Offline qfq layout migration; download owns the format | None | Default inspects only; `--apply --target-root` builds a separate external target under the source qfq directory's `.maintenance.lock` (including `--source-root`), independent of history maintenance; source is never replaced/deleted, failed staging is retained | Aggregate planned/built_verified/failed JSON to stdout; no activation | Sequential two-profile conversion, source fingerprints, per-stock equality and SQLite integrity checks; 5 s SQLite busy timeout, no hard RSS/disk/whole-job deadline |
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

Tencent qfq supplier and actual updater measurements use the unified CLI:

`--profile history-industry --source-timeout-seconds 15 --command-timeout-seconds 90 --output -` measures real login, calendar, and two dated industry queries, followed by a fresh supplier-process run reusing the same temporary query cache. It reports cold/warm time, queried/cached dates, and interval equality. It uses the published cutoff and up to three eligible codes for projection; supplier industry requests are market-wide, including all pages. It never fetches price history, updates active data, or measures the full multi-year preparation phase. Interrupted probes may leave an isolated temporary directory.

`--profile history-space` inventories published/pending/seal files using inode-deduplicated allocated bytes; it never deletes files or infers that pending files are obsolete. `--profile history-source-compare --codes 600519 300750 688981 --history-days 251 --source-timeout-seconds 20 --command-timeout-seconds 180 --output -` compares identical active-calendar/filtered-universe inputs through BaoStock and Tencent-plus-gap pipelines. It uses real network requests and disposable system-temporary outputs only, including monthly writing, sealing and publication. Limits are 1–10 codes, 1–2000 sessions and 1–45 seconds per attempt; the outer diagnostic deadline bounds the child. A timeout may leave an isolated temporary directory for diagnosis. Neither probe modifies active history or authorizes a production source switch; a short-window win does not prove 2000-day coverage or full-market speed.

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile tencent-download \
  --download-sizes 10 50 100 --history-workers 8 --output -
```

This profile fetches the official universe and Tencent calendar, writes only
disposable temporary V2/V3 caches, measures paired publication and restart
idempotency, and reads any available history for a bounded shadow comparison.
Sizes are limited to three samples of 1–100 stocks and workers to 1–12.
Missing trading dates remain pending. Supplier socket timeouts are inactivity
limits; the diagnostic child deadline bounds the whole child command.
The report never authorizes replacing complete history with Tencent.

Audit active qfq paired publication and checkpoint state:

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile qfq-sqlite \
  --output -
```

The probe copies the current V2/V3 board shards without taking SQLite read
transactions on active files, rejects a copy if file or journal identity changes,
and runs `quick_check` only on the disposable system-temporary copies. It reports
aggregate shard/code/row counts, `quick_check`,
journal modes, hot journals, identity shape, V2/V3 source/cutoff/calendar
pairing, and both directions of the database/checkpoint difference. It does not
emit stock identities or prove supplier availability. Use `tencent-download`
separately when a bounded disposable supplier/update probe is required.

Build a persistent migration candidate with:

```bash
.venv/bin/python3 scripts/rename_qfq_shards.py
.venv/bin/python3 scripts/rename_qfq_shards.py --apply \
  --target-root /absolute/outside/repository/qfq-ready
```

The target must not exist. Success is `built_verified`, with `activated=false`;
all windows, source identities and source fingerprints have been compared.
A failure retains the sibling `.building` directory for inspection. This tool
does not switch active data. A separately authorized switch must stop readers
and writers, retain the full old directory, verify a common filesystem, rename
the full verified candidate into `data/qfq`, and restart against the new layout.
Keep the old directory outside the repository for recovery. Routine downloads
then use `./run.sh download_qfq`; old `index.json` layouts fail explicitly until
migrated. Neither databases nor backup data belong in Git.

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

All HTTP diagnostics obtain the project transport from `bootstrap.OpenExternalHttp`;
raw Requests/urllib calls and mutable response hooks are confined to the shared driver.
Responses are immutable and already closed before parsing. The driver has no automatic
retries or redirects and ignores environment proxy settings. Its default response cap
is 16 MiB; official files/WebDriver use 32 MiB, PDFs/research samples 4 MiB, and Web health
1 MiB. Timeout/cancellation checks run between raw reads and compressed content is also
bounded after decompression. Supplier parsers belong to the product adapters; the
Eastmoney spot diagnostic does not add a production source route.

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

It reads monthly SQLite headers, samples page classes, benchmarks latest-row,
training-projection and active-gap queries, and measures bounded revision writes
and sealing only in a disposable temporary database. It does not compact or switch the active history database. Maintenance rules
are in [the download guide](../docs/4_下载训练.md), section 3.4;
training performance is explained in [the training guide](../docs/4_下载训练.md),
section 5. The repack tool remains a separate explicit maintenance operation;
its default paths do not authorize active-data changes. During refactoring,
rehearse only on explicit repository-external copies as required by the
[architecture contract](../docs/2_工程设计.md), section 14.

Test commands and fast/full boundaries are defined by Makefile and documented
in the architecture contract, section 13. Default `make test` runs the fast
set; release acceptance also requires `make test-full`. Work status and pending
optimizations are recorded only in `docs/3_工程实施.md`.


完整history当前仅从空库重建，不提供旧格式转换。只读诊断`--profile tencent-history-window --codes 600519`核对整窗2000条实际返回；`--profile history-write --download-sizes 8 --history-days 2000`在仓库外临时目录对合成数据测量首建、日更、隔周与同日重跑，报告目标设备类型、分阶段耗时、物理写入量与峰值RSS，不写活动历史。隔离fixture和真实供应商/全市场下载耗时分别判断。
