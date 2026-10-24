# Scripts

`scripts/diagnose_runtime.py` is the only public runtime diagnostic command.
Its profile implementations live under `scripts/runtime_diagnostics/` and
must return bounded, sanitized summaries. A new runtime probe is added as a
module and profile there; it is not added as another top-level script.

`--profile download-performance` runs three rounds on disposable synthetic SQLite databases outside the repository.
It reports company write/publication/cold and warm reads, unchanged publication, and equivalent per-stock versus batch gap queries.
It makes zero HTTP requests and cannot establish full-market supplier download speed.

`--profile company-resume --company-replay-directory /absolute/external/path`
backs up `--company-evidence-database` through SQLite, then resumes the failed
date slices against real CNInfo only in that external copy. The first run interrupts
after two successful page responses; later runs verify cursor recovery and completed
coverage, with at most 32 pending PDFs sampled per run. Structured sources are skipped.
Use `--command-timeout-seconds 2400` for a bounded long replay; the child receives
30 seconds less for its logical budget. The directory retains the resumable copy,
selection and sanitized `report.json`; the unified `--output` remains the report entrypoint.
The active database stays read-only. A passed replay covers selected slices only,
and does not mean the whole year or all pending PDFs are complete.

Adding `--company-query-audit` to that profile performs a read-only count/filter audit instead of a backup or replay.
It samples up to three failed dates and three failed child cursors, at most 192 logical page queries and 300 seconds (transport retries remain bounded). Reports contain counts and issuer-match diagnostics, never stock identities or source payloads. Matching sampled totals alone does not prove full date coverage.

## Retained tools

This table is the sole declaration of the nine retained top-level tools.
`check_refactor_quality.py` checks its complete fields, unique inventory and
agreement with the approved script manifest. Declarations describe actual
behavior; they do not grant permission to write active data.

| Tool | Owner | Network | Writes | Output | Resource boundary |
| --- | --- | --- | --- | --- | --- |
| `check_joint_training_performance.py` | Training performance evidence gate | None | Required external benchmark workspace and output; isolated training artifacts only | Aggregate benchmark JSON to explicit external output and stdout; progress to stderr | Three sequential V2/V3 fits against the same external history copy; accepts parallel fit only when at least 10% faster than best serial fit and process peak RSS at most 5 GiB; no hard deadline |
| `check_refactor_quality.py` | Repository quality gate | None | None; Ruff runs with `--no-cache` | stdout success; stderr violations/failure | Two sequential Ruff subprocesses, 60 s timeout each; finite repository scan, no hard RSS/total I/O limit |
| `check_tomorrow_training_memory.py` | Training evidence gate; training owns the execution | None | Models and samples under required `--train-root` and `--sample-root`; explicit external `--output` | Result JSON to stdout and result file; progress to stderr | Two sequential full rebuilds; 2 compute threads; default 2048 MiB RSS acceptance threshold measured after execution, not an enforced memory cap; stage durations are recorded, with no artificial total deadline |
| `diagnose_runtime.py` | Unified diagnostics; owning probes in `runtime_diagnostics/` | Selected Web/vendor profiles use real HTTP; `market-factor-compare` only reads local qfq/industry databases plus explicitly selected live sources; `long-watchlist` reads financial/announcement caches and explicit historical eligibility databases offline; `browser` uses an isolated local fixture; `performance`, `history-sqlite` and `qfq-sqlite` are offline | Combined report to stdout or external `--output`; history revision benchmarks and qfq integrity checks write only disposable system-temporary targets, while active qfq data remains read-only; optional stock evidence report requires external path; Long audit rejects network and cache writes | Sanitized aggregate JSON; market-factor profile defaults to readable terminal text; child raw output and stock identities are not forwarded | Sequential children, default 180 s timeout each; market-factor per-source test loop at most 120 s and whole task at most 600 s; Long audit uses at most 8 concurrent stocks; SQLite page samples 1–100, query rounds 1–9, revision samples 1–5000; qfq audits one shared bounded database and emits aggregate counts; child capture has no fixed byte/RSS cap |
| `generate_long_watchlist_asset.py` | Packaged Long build asset | None | Without `--check`, writes only `src/trader/web/static/long_watchlist_data.js` from `config/long_watchlist.json`; `--check` is read-only | Stale-asset message to stdout; silent success | One JSON document and one asset; no hard byte/RSS/deadline cap; does not fetch quotes |
| `migrate_runtime_data.py` | Explicit isolated layout migration; codecs in `runtime_migration.py` | None | Required external stopped workspace copy and a new target; rejects live WAL, symlinks, conflicting identities and existing targets/staging; failed staging is retained | Aggregate candidate manifest; `verify` is read-only; no active switch | Streaming file hashes, SQLite integrity, byte-preserved decisions/checkpoints, eligibility validation, budget union, sequence high-water preservation and research routing; regenerable history/windows/models/samples are listed explicitly |
| `repack_history_partitions.py` | Explicit annual history compaction | None | Required source `history` and sibling target `history-repacked`; external maintenance lock and activation journal; build preserves snapshot identity, explicit activation swaps whole directories, rollback retains candidate, finalize requires training evidence | Aggregate status JSON; progress to stderr | Each annual file compacted once, every monthly logical proof verified; no automatic production execution |
| `runtime_migration.py` | Internal codecs for `migrate_runtime_data.py`; no standalone command | None | Called by migration command on its isolated candidate only; no independent target selection | Typed migration results and errors to caller; no direct CLI output | Sequential SQLite and file-hash operations under the migration command; no independent hard RSS or deadline cap |
| `verify_wheel_install.py` | Isolated installed-wheel release gate | Local wheel install with `--no-deps` and version check disabled; no supplier requests; pip index access is not explicitly disabled | Temporary external virtualenv and dependency `.pth`, removed on exit; input wheel/config/resources read-only | Aggregate verification JSON to stdout; exceptions can produce stderr traceback | One wheel and sequential CLI/resource checks; subprocesses and virtualenv creation have no timeout; no hard RSS/disk cap |

Repeated download, training, scoring, or status workflows belong to
`trader-cli` or a business `entrypoints/commands.py`, not to this directory.
Every retained or newly added tool must have a bounded input/output contract,
an explicit network/write declaration, and an entry in this table before it is
used by a gate or documented command. Missing limits must be stated explicitly;
an RSS acceptance threshold, SQLite busy timeout or HTTP socket timeout must not
be described as a hard whole-job resource bound. Migration requires a
repository-external destination; for repack acceptance, pass explicit isolated
external paths. For training evidence, also explicitly
set external `--history-root`, `--train-root` and `--output`.

## Skill evaluation (opt-in internal module)

`skill_evaluation/` owns synthetic agent evaluations, separate from runtime
diagnostics and the nine retained top-level scripts. It is not a product entry
and is never invoked by default tests or quality gates. Explicit `run` makes
Codex model requests using the current CLI login and model configuration;
`compare`, help and offline unit tests make no requests.

| Tool | Owner | Network | Writes | Output | Resource boundary |
| --- | --- | --- | --- | --- | --- |
| `python3 -m scripts.skill_evaluation` | Repository skill execution evidence | `run` invokes Codex model inference; agent shell network disabled, no live supplier requests | Required new absolute external directory, one synthetic repository and local bare origin per case; no active data or original remote copied | Versioned aggregate `summary.json`, readable `summary.md`, external per-case raw JSONL/stderr; compare prints a table | Eight sequential cases, repeat 1–3 (default 1); 600 s and 20 MiB combined child output per case, process group terminated on limit; local Git calls 15 s; no hard RSS/disk limit on agent-created files |

```bash
.venv/bin/python3 -m scripts.skill_evaluation run --suite all --repeat 1 --output-dir /tmp/trader-skills-before
.venv/bin/python3 -m scripts.skill_evaluation compare --before /tmp/trader-skills-before --after /tmp/trader-skills-after
```

`--suite` also accepts one case name for a focused repeat. Use new output
directories for each run. `--skill-source` accepts a previously saved skill
snapshot; each run copies its inputs once and records both skill and evaluator
digests, so editing the checkout cannot mix versions within a suite.
The disposable venv contains the existing Ruff package and binary, with no
editable product installation. Only the fixture's own Git metadata and local
bare origin are explicitly added to the agent's writable roots.
If Codex cannot execute any commands, subsequent cases are marked not started
instead of repeating a known infrastructure failure. Exit `0` means all required evidence passed, `1`
means an observed behavior failure, `2` means an execution error or evidence
gap. Unknown JSONL events, missing final claims and unavailable model identity
remain explicit; null identity means Codex did not report it, not an inferred
model match. Raw traces may contain local paths and are retained only outside
the checkout. These fixtures do not prove production recovery or a stable
success rate. The current checks recognize shell reads and static task shapes;
alternate tool formats or equivalent code outside those shapes need review.

Makefile checks all of `src/trader`, `tests` and `scripts`, including internal
probes and migration tools. Strict source debt is anchored to rule, relative file
and qualified function, so unchanged aggregate counts cannot hide relocated
violations. Changing a debt owner requires diff review and an explicit baseline
update. This gate does not prove the accuracy of declarations or enforce the
runtime limits of every tool; review and isolated execution evidence remain
necessary.

Tencent qfq supplier and actual updater measurements use the unified CLI:

`--profile history-industry --source-timeout-seconds 15 --command-timeout-seconds 90 --output -` measures real login, calendar, and two dated industry queries, followed by a fresh supplier-process run reusing the same temporary query cache. It reports cold/warm time, queried/cached dates, and interval equality. It uses the published cutoff and up to three eligible codes for projection; supplier industry requests are market-wide, including all pages. It never fetches price history, updates active data, or measures the full multi-year preparation phase. Interrupted probes may leave an isolated temporary directory.

`--profile history-space` inventories published/pending/seal files using inode-deduplicated allocated bytes; it never deletes files or infers that pending files are obsolete. `--profile history-source-compare --codes 600519 300750 688981 --history-days 251 --source-timeout-seconds 20 --command-timeout-seconds 180 --output -` compares identical active-calendar/filtered-universe inputs through BaoStock and Tencent-plus-gap pipelines. It uses real network requests and disposable system-temporary outputs only, including annual writing, sealing and publication. Limits are 1–10 codes, 1–2000 sessions and 1–45 seconds per attempt; the outer diagnostic deadline bounds the child. A timeout may leave an isolated temporary directory for diagnosis. Neither probe modifies active history or authorizes a production source switch; a short-window win does not prove 2000-day coverage or full-market speed.

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

V5的真实小范围训练通过同一入口执行（先安装v5可选CPU依赖）：

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile v5-training \
  --v5-probe-directory /tmp/trader-v5-industry-probe \
  --command-timeout-seconds 600 --output /tmp/trader-v5-industry-report.json
```

输出目录必须位于仓库及活动历史之外且尚不存在。工具只读捕获已发布历史身份，按当前最大行业选择上市最早的最多40股，在输出目录导出真实日线与参考副本；导出核验所选revision身份和行哈希，不冒充全历史完整性扫描。随后按生产20000行行业门槛进行两个epoch的真实CPU双头拟合、校准、发布及重载，报告实际样本、耗时、RSS和失败原因。网络调用及活动数据写入为零；源码、模型与样本都不会自动切到活动目录。该小范围工程探测不证明全市场资源、默认100epoch训练、收益或生产准入通过；超时或取消可能保留外部副本用于诊断。

已有仓库外冻结历史副本后，可用未修改配置的参数核验V1/V2/V3/V5全部八头、样本缺失原因和重载结果：

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile training-alignment \
  --history-root /absolute/external/history \
  --training-probe-directory /absolute/external/new-training-probe \
  --command-timeout-seconds 1800 --output /absolute/external/alignment.json
```

历史和输出均须位于仓库外且彼此独立，输出目录必须尚不存在。报告区分训练工程状态和未验收收益，不切换活动工件；输入副本包含多少股票就只验证该范围。

仅核对V1历史输入可运行`--profile model-inputs --history-root <历史目录> --command-timeout-seconds 360`；该模式只读启动投影，报告窗口/日期缺口、估算覆盖与耗时，不训练、不预测、不写活动数据。

Audit active qfq paired publication and checkpoint state:

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile qfq-sqlite \
  --output -
```

The probe opens the shared daily window read-only, verifies both profile manifests and
price hashes, checks SQLite integrity and WAL mode, and reports aggregate pairing
and checkpoint differences. It checks publication identity before and after the
audit, emits no stock identities, and does not prove supplier availability.

Local configuration, history-maintenance state, research readiness and offline
performance share the existing implementations:

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile local --output -
.venv/bin/python3 scripts/diagnose_runtime.py --profile local --scoring-profile both --output -
```

No service or supplier is required. Invalid configuration marks dependent checks
as unexecuted. Insufficient research evidence is degraded locally; the standalone
research profile remains strict. Performance results are separate for each profile.

The standalone performance diagnostic defaults to one process concurrently exercising
all eight Tomorrow/D25 heads. Select one profile when measuring an isolated profile:

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile performance --output /tmp/trader-performance.json
.venv/bin/python3 scripts/diagnose_runtime.py --profile performance --scoring-profile v3 --output /tmp/trader-performance-v3.json
```

The explicit selection `--scoring-profile v1,v2,v3,v5` is equivalent to that default;
`both` keeps its existing separate V2/V3 probes, and `local` keeps its configured
single-profile default. Models are loaded through the production contract, never
trained or substituted. Missing/dependency/contract failures retain per-profile
reasons and empty measurements. V1 remains Shadow and no DeepSeek HTTP is issued.

Fixture history and input preparation occur outside model latency samples. The
report separates first computation, exact reuse, one-stock fact changes and cold
score-to-draft wall time, includes a separately profiled native-call count, and
measures actual current RSS across 100 ticks using the same scoring caches.
The process peak is reported separately. Source, complete input, model, strategy,
cost, budget and environment identities bind comparisons; the existing performance
entrypoint's `--baseline` requires matching inputs, models, contracts and environment,
while retaining different source digests to compare implementations. This is a synthetic
scoring workload: history disk scans, shared market-cache occupancy, real suppliers,
browser latency and formal freeze equivalence remain unmeasured, not passed.

`--profile history-write --download-sizes 16 --history-days 2000` also measures
`initial_resume`: it first commits half the stocks without publishing the initial
snapshot, then times the restart against the populated annual files. Reports split
the Tencent pipeline and sealing/publication wall time from cumulative supplier and
writer time. This is an offline synthetic workload; it does not modify active data
or establish full-market download speed. Run comparisons sequentially to avoid
competing for the same disk.

Compare the annual layout with an isolated checkout of the pre-migration monthly
layout (for example, commit `c235ffa3` extracted outside this repository):

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile history-write \
  --baseline-checkout /absolute/external/baseline \
  --download-sizes 8 --command-timeout-seconds 900 --output -
```

The comparison alternates old and new workers for five rounds by default. It
uses 8 synthetic stocks, 100 logical months and 2000 simulated sessions, and
reports medians, connections, transactions, database and auxiliary file counts,
allocated bytes, sampled WAL peak and process RSS. It requires improvement in
the main hot reads, training scan and incremental writes; startup projections
must stay within the 10% regression limit. Files
and databases are disposable; warm operating-system cache and the small fixture
do not establish full-market or physical cold-cache performance.

With an annual-layout baseline, the same command compares shared-window
publication reads and writes instead. `--download-sizes` accepts up to 5000
synthetic securities, and `--history-days` sets the window length (capped at 251).
Each worker publishes bounded batches, reads both profiles through `iter_windows`,
and reports complete identity scans, connections, transactions, elapsed time,
sampled WAL peak, and process RSS. It also measures history batch writes and
narrow training reads, training sample writes/reads, and company publication
writes/reads, using up to 1000 stocks and 20 dates per adapter fixture. Five alternating rounds
must improve reads and keep initial/incremental writes within 10% of the baseline.
For a focused identity-scan measurement use `--download-sizes 5000 --history-days 5`;
this is a synthetic metadata workload, not full-market service acceptance.

Build an isolated migration candidate from a stopped copy containing `data/`
and `.runtime/trader/`:

```bash
.venv/bin/python3 scripts/migrate_runtime_data.py build \
  --source /absolute/external/legacy-workspace \
  --target /absolute/external/data-candidate --default-profile v2
.venv/bin/python3 scripts/migrate_runtime_data.py verify \
  --source /absolute/external/data-candidate
```

The default profile must reflect the old unscoped runtime's actual identity.
Conflicting duplicate identities stop the candidate. Frozen payloads/checkpoints,
permanent eligibility, budget request histories, reserved publication coordinates,
outcomes and valid research evidence are preserved. History, short windows, models
and samples are declared for regeneration; old caches remain cleanup candidates.
The source is fingerprinted before and after; failures retain staging. Active
switching, deletion, scheduler installation and bulk downloading require separate
authorization. The old qfq shard conversion tool has been removed.

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

It reads annual SQLite headers, samples page classes, benchmarks latest-row,
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
