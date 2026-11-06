# Scripts

全部工具的唯一公开入口是 `scripts/tools.py`。先运行 `--list` 查找业务、模块和动作；运行时必须同时明确 `--business`、`--module`、`--action`。无参数不执行动作，旧`--check`、`--profile`及live/full/sources/runtime组合与别名已删除。内部文件只提供函数，不提供独立命令或兼容转发。

当前catalog有57项公开动作：保留原49项功能，另统一资源生成、迁移build/rollback、历史分区四种维护动作和公司真实同步计量。检查与写入能力都由所属业务持有，清单不代表已获准实际执行。

```bash
.venv/bin/python3 scripts/tools.py --list
.venv/bin/python3 scripts/tools.py --business infra --module disk --action profile
.venv/bin/python3 scripts/tools.py --business recommendation --module web --action health --scoring-profile v3
.venv/bin/python3 scripts/tools.py --business download --module company --action coverage --company-database-audit
.venv/bin/python3 scripts/tools.py --business recommendation --module assets --action consistency
```

根`--help`展示中立公共参数，选中动作的`--help`只加载所属业务解析器，并展示其参数和副作用。业务检查沿用原有具名参数和上限；不会因选择某项动作而运行其它业务。训练、真实网络、浏览器、性能探针、隔离样本构建及维护都需要明确选中并满足原授权和隔离要求。`recommendation/pipeline/snapshot`明确选择配置、维护状态、研究就绪和离线性能四项；配置失败后其它三项报告skipped。

荐股公共原始因子复用候选只在显式连续回放中选择，默认`baseline`，不修改生产服务。D在冻结版本上使用相同配置、模型、股票数和tick数分别运行基线及下列候选；删去`--performance-factor-candidate raw_factor_reuse`即为基线。它使用现有共享`quote_factor_inputs`容量和`feature_batches`字节预算，合并完全相同的报价、日线、历史摘要、horizons和基础配置；命中后返回独立可变副本，风险/证据/分钟及模型组装仍走原流程。非有限输入不写此候选缓存，交原计算处理，原异常也继续传播。`raw_factor_computations`仅统计可规范序列化输入的候选原计算完成次数（包括缓存拒写），基线为null；不把缓存命中计数当作省下的函数调用。`feature_group_max_bytes`包含基础、候选和横截面的整个特征组，仍需另看实际RSS。

```bash
.venv/bin/python3 scripts/tools.py --business recommendation --module pipeline --action latency \
  --runtime-config /external/frozen/config/runtime.json --scoring-profile v1,v2,v3,v5 \
  --performance-replay-ticks 5 --performance-factor-candidate raw_factor_reuse \
  --command-timeout-seconds 1800 --report /external/raw-factor-candidate.json
```

先比较每档特征、输入版本及双头`result_hash`，再比较完整阶段耗时、原函数调用数、缓存占用、RSS及取消/恢复；CPU独立探针以`raw_factor_requests`计原算术owner调用、`raw_factor_cache_requests`计候选入口调用，后者仍有身份生成和查询成本。减少原始计算次数不代表总耗时改善。此回放使用合成报价和真实加载模型，不覆盖真实采集、盘中轮次、历史预载或Web；功能夹具预测器不算真实模型验收。候选未取得D同输入时延/资源证据前不接入生产。

连续回放用`timing_contract`区分候选质量检查的计时口径；旧口径报告不能直接用作新口径基线。每档`stage_ms`单列候选并集规划、共享刷新特征和逐头特征/质量检查，质量仍由生产`PrepareNativeQualityBatch`只计算一次。每头`candidate_preparation`投影动态过滤、候选规划和质量检查的实际计数及原阶段耗时；它们嵌套在对应外层操作中，不与`stage_ms`再次相加。共享刷新特征只在档位计一次，不重复分摊至双头；阶段3仍仅测已取得报价的因子组装，采集/分钟/研究I/O、预载、正式冻结和Web继续排除。

业务目录直接位于`scripts/`下，按recommendation、download、training、infra、engineering归属。`core/catalog.py`是选择关系的唯一可执行来源；`core`只持有中立选择、执行、报告和公共类型，不导入业务参数或默认值。各业务的`cli.py`解析和校验输入，`options.py`持有不可变业务类型，`commands.py`构造明确选中的命令；没有包含全部业务字段的全局Options对象。未选worker不导入，根帮助和清单不加载产品、下载或训练实现。

JSON输出为两空格缩进、中文直显，schema为`trader-tools`。顶层selection保存business/module/action，逐项报告passed、degraded、failed、timed_out、cancelled、skipped、empty；超时和取消不能算通过。工程debt只记录退出码及输出字节数；显式transform/skills另保留受限的工具报告文本，供审阅命名清单、预览或评测比较，不转发行情与供应商原文。原生动作的`-- --help`或`-- -h`直接显示参数帮助，不运行业务。单个子进程默认180秒，公共超时上限沿业务校验；原生工程参数路径前使用`--`。捕获输出合计上限20 MiB；普通诊断超时或取消后，POSIX先发送SIGINT并最多留5秒让worker的finally回收其自行创建的session，再强制清理自有进程组并wait；这段回收时间另计，正常完成也回收自有进程组。Windows清理直接子进程，不宣称通过后代清理验收。

默认公共报告写标准输出，`--report`只接受`-`或显式仓库外绝对路径的新文件；现有文件、目录和链接均拒绝，最终写入也独占创建，避免覆盖配置、输入或旧报告。`--report-format`选择auto/json/text；auto在市场因子检查输出到终端时使用text，保存报告时使用json。业务原有`--output`保留工件含义，不是公共报告别名。公共报告必须与原生工件文件、skills输出目录及维护source/target/backup目录分离；相同路径及任一方向的祖先关系都在执行前拒绝。检查不写活动数据；样本、布局比较和修复演练只使用显式外部副本或既有隔离fixture。网络检查输出聚合计数、阶段耗时、失败类别及来源年龄，不输出行情、证券清单、密钥或供应商原文。磁盘profile是只读盘点，unknown不能当作SSD/HDD性能证据；两种介质分别记录，工具整理的启动耗时也不能充当硬盘吞吐结论。

历史发布分段沿用`download/history/write`，只有显式`--history-publication-profile`才安装进程内观测钩子。无`--history-root`时观察既有合成首建、增量、无变化和恢复；指定已发布历史时，先验证原凭据，再在系统临时目录逐年复制并重放封存，峰值副本为一个年库或catalog，不联网、不改源数据库/凭据。非空WAL、缺凭据或源文件变化拒绝本轮；临时目录应位于实际测量介质，复制会预热页缓存，报告不会声称冷盘或完整真实同步已验收。

```bash
.venv/bin/python3 scripts/tools.py --business download --module history --action write \
  --history-publication-profile --history-root /external/frozen-data/market/history \
  --runtime-config /external/frozen/config/runtime.json --command-timeout-seconds 3600 \
  --report /external/history-publication-profile.json
```

`publication.phases`分别列出变更探测、writer关闭、年度封存、catalog封存及文件摘要；`parent_id`表示包含关系，不能把父子耗时相加。`primitive_seconds`按底层execute/fetch、SQLite关闭、文件读取、SHA-256更新和fsync的自身耗时分类；fsync只计Python显式调用，SQLite内部同步仍包含在相应驱动调用中。`operation_calls`与逐文件摘要可识别重复封存。另记录进程CPU、实际/逻辑I/O和RSS，源核验/副本准备/副本复核均计入真实重放总墙钟；合成模式的`sync_seconds`与包含结果复核的`seconds`分列。cProfile开销已计入，`performance_acceptance=false`；需另以关闭profiler的同输入对照校准开销并验收收益，不把诊断轮次当作提速通过。

## 显式工程与资源检查

```bash
.venv/bin/python3 scripts/tools.py --business engineering --module quality --action debt
.venv/bin/python3 scripts/tools.py --business engineering --module quality --action transform -- --naming-audit --all --json
.venv/bin/python3 scripts/tools.py --business engineering --module packaging --action install -- --dist-dir /external/dist
.venv/bin/python3 scripts/tools.py --business engineering --module migration --action verify -- --source /external/candidate --config /external/runtime.json
.venv/bin/python3 scripts/tools.py --business engineering --module skills --action evaluate -- compare --before /external/before --after /external/after
.venv/bin/python3 scripts/tools.py --business training --module resources --action memory -- --help
```

quality/transform 保留明确的 `--format check|preview|write` 与 `--rename check|preview|write`，仍须用户单独授权，普通继续、验证和本次回归不会自动执行这些分支及触发用例。仅命名审计不写文件；改名要求绑定完整路径、内容摘要、基准提交的 manifest，拒绝猜测动态消费者。quality/debt 只运行 Ruff 规则检查及清点，不运行格式检查。

packaging/install 在隔离 venv 安装指定 wheel，验证资源、公开命令和依赖；需要明确选择，不在帮助、清单或默认回归中安装。training/resources/memory 是受限 V3 训练与重复身份/RSS验收，会消耗计算资源并写外部结果，必须显式选中并提供全部参数。skills/evaluate 的 compare 只读报告；run 会调用付费 Codex 推理、创建新外部合成仓库，不能由普通测试或质量门禁间接触发。停用的 trader-delivery 不纳入自动 skill 输入，也不会被复制或执行。

isolated比较可读取明确指定的旧checkout；只在隔离演练内处理旧持久化格式，不给产品增加双读或兼容层。数据库和样本比较使用当前绝对路径工具入口及明确的旧产品PYTHONPATH，避免调用旧checkout中的入口；不尝试旧命令fallback。

公司coverage原生用途已公开为具名业务参数：`--company-pacing-audit`比较真实PDF/目录的共享与分源限速，`--company-material-keys <源键...>`核对1至6个唯一材料键。两者与既有审计模式互斥；pacing模式必须提供`--company-replay-directory`仓库外绝对目录，材料事实由显式`--company-evidence-database`或所选runtime正式facts路径提供，不用空数据回退。`--company-audit-budget-seconds`范围30至10800秒，子进程deadline至少多留30秒回收，例如预算120秒时配置`--command-timeout-seconds 150`。公司I/O候选及其新增消费者由B独立交付，不纳入本批候选或功能验收。

公司`--company-database-audit`在本次期限回调已触发且SQLite报告`interrupted`时，输出`failed`和`company_database_audit_budget_exhausted`，保留`complete=false`及质量未验证标记，原生与公共入口均非0退出，不继续压实。仅时间已过或其它SQL错误不改判为此预算类别；该报告不代表全库审计完成，预算与dbstat检查均不因此放宽或跳过。

字段格式升级模式及`--company-field-migration-audit`参数已删除。程序不兼容或转换旧下载数据库；结构不匹配时删除相关下载资料后重新下载，诊断不自动清库，也不将模型、正式冻结记录或请求预算当作可重下资料。

已有原文的发布复查也走coverage，选择`--company-field-replay-keys <1至6个cninfo材料键> --company-replay-directory <新仓库外目录>`，与其它专用模式互斥。工具只接受当前结构及合法凭据，直接备份采集与正式库、核验并复制有界保留文本，再两次启动真实发布者及字段reader，不转换格式；报告分别核对备份代际、正文读取、字段尝试、原有效回执和发布身份。缺材料或来源文件变化明确降级，原始文件/WAL/凭据hash按实际比较，不以空WAL差异归一化为通过；不联网、不改源库、不签全市场质量或性能。

公司sources的普通与字段模式均检查全市场来源，不按证券清单过滤；另以`--field-sources-only`选择字段观测，`--field-source-rows`限定样本，`--field-source-schema`和`--field-source-identities`仅在字段模式明确选择。`--annual-income-period`查询单个年末期间，必须配合单个`--codes`，不能与字段模式混用；其它模式拒绝codes。每种模式都消费所选`--runtime-config`并报告`runtime_configuration_sha256`，身份摘要不表示字段质量、覆盖或生产采用已合格。

Web health支持可重复的`--strategy tomorrow|d25`，连续空结果阈值`--consecutive-zero-threshold`须在样本数以内，默认min(3,样本数)。浏览器动作另支持`--strategy`与带时区ISO时间`--simulated-start`；Long admission的`--strategy`表示策略JSON路径，`--workers`范围1至8。这些参数只属于各自选中的动作，不能跨动作隐式生效。

`download/company/live-sync`是前台真实同步计量，会调用现有同步owner，并通过原生`--measurement-output`写外部计量工件；它与普通只读coverage分开选择，沿用真实同步的授权、配置和发布边界。该工件必须与公共`--report`分开。以下帮助不启动真实同步：

```bash
.venv/bin/python3 scripts/tools.py --business download --module company --action live-sync -- --help
```

## 显式生成与维护动作

全部生成和维护能力也由统一入口选择。以下仅展示帮助，不执行写入：

```bash
.venv/bin/python3 scripts/tools.py --business recommendation --module assets --action generate -- --help
.venv/bin/python3 scripts/tools.py --business engineering --module migration --action build -- --help
.venv/bin/python3 scripts/tools.py --business engineering --module migration --action rollback -- --help
.venv/bin/python3 scripts/tools.py --business download --module history --action repack-build -- --help
```

assets/consistency只读核对，assets/generate显式重建打包JS资源；公司账本浏览器验收归recommendation/web/company-ledger。migration/verify只读检查外部候选，build要求外部停止的旧工作区副本和全新target，rollback要求明确target/backup。history/repack-build、repack-activate、repack-rollback、repack-finalize沿用原协调器与训练证据围栏。维护动作在前台运行，不套用普通诊断的180秒deadline；成功还必须满足各动作的最终状态，pending/blocked/failed不能仅因进程退出0而通过。本轮工具迁移没有授权实际生成、数据切换、重排、回滚或发布。

## Retained tools

| Tool | Owner | Network | Writes | Output | Resource boundary |
| --- | --- | --- | --- | --- | --- |
| `tools.py` | 公共选择、执行与报告；所属业务实现动作 | 仅显式选中的来源/浏览器/skill run | 标准输出或`--report`指定的仓库外报告 | 白名单聚合JSON | 普通诊断默认180秒；20MiB子输出；限定进程生命周期 |

## 全量文件去留与消费者清点

下表只按本批隔离候选逐文件核对用途、I/O围栏和消费者；路径相对scripts/，测试消费者相对tests/，不收B/C未提交的产品、脚本或新测试。消费者依据静态导入及非docstring的注册/命令字符串核对；按catalog动态加载的业务解析器/命令另标所属处理器。所有动作的有效组合只由core/catalog.py的TOOLS声明，本表不成为第二份路由配置。

根目录的Python执行入口只有tools.py；包初始化不提供命令；旧diagnose_runtime.py、generate_long_watchlist_asset.py、migrate_runtime_data.py、runtime_migration.py、repack_history_partitions.py和checks/均删除。原资源生成与检查合并到recommendation/assets，迁移操作和codec归engineering/migration，历史重排归download/history/repack.py；无旧路径转发、双实现或旧入口fallback。原runtime_diagnostics/、code_quality/、skill_evaluation/也没有活动Python源码；历史工程记录和停用skill只读保留。

| 当前文件 | 用途 | I/O边界 | 直接消费者 |
| --- | --- | --- | --- |
| `core/__init__.py` | Internal checks selected by scripts/tools.py. | 包初始化；导入不执行动作 | 所属包导入 |
| `core/catalog.py` | Immutable public actions and private worker allowlisting. | 中立路由/类型/校验/投影 | `training/commands.py`、`recommendation/pipeline/commands.py`、`recommendation/web/commands.py`、`recommendation/long/commands.py`、`download/history/commands.py`、`download/market/commands.py`、`download/company/commands.py`、`infra/database/commands.py`、`infra/disk/commands.py`、`core/cli.py`、`core/projections.py`、`helpers/shared/tool_cli.py`、`system/contract/test_reusable_diagnostics_contract.py`、`training/unit/scripts/test_v5_diagnostic.py`、`training/unit/scripts/test_training_alignment.py`、`training/unit/scripts/test_training_resources.py`、`training/unit/scripts/test_training_performance.py`、`tooling/unit/scripts/test_database_baseline.py`、`tooling/unit/scripts/test_tools_maintenance.py`、`download/unit/scripts/test_market_factor_compare.py`、`recommendation/unit/scripts/test_company_ledger_tool.py`、`tooling/unit/scripts/test_disk_diagnostic.py`、`download/unit/scripts/test_company_document_download_audit.py`、`training/unit/scripts/test_v5_lite.py`、`recommendation/unit/scripts/test_long_watchlist.py`、`tooling/unit/scripts/test_diagnose_runtime.py`、`download/unit/scripts/test_history_publication_audit.py`、`download/unit/scripts/test_qfq_sqlite.py`、`training/unit/scripts/test_v5_samples.py`、`tooling/unit/scripts/test_check_entrypoint.py`、`download/unit/scripts/test_company_resume_diagnostic.py` |
| `core/cli.py` | Route one explicit tool action; unselected business modules stay unloaded. | 中立路由/类型/校验/投影 | `tools.py`、`helpers/shared/tool_cli.py`、`download/unit/scripts/test_company_live_sync_tool.py`、`tooling/unit/scripts/test_tools_maintenance.py`、`tooling/unit/scripts/test_check_entrypoint.py` |
| `core/contracts.py` | Immutable execution and output values for the common tool router. | 中立路由/类型/校验/投影 | `training/commands.py`、`training/reporting.py`、`training/jobs/training_performance.py`、`engineering/migration/cli.py`、`engineering/packaging/reporting.py`、`recommendation/pipeline/commands.py`、`recommendation/pipeline/reporting.py`、`recommendation/web/commands.py`、`recommendation/web/reporting.py`、`recommendation/assets/cli.py`、`recommendation/long/commands.py`、`recommendation/long/reporting.py`、`download/history/repack.py`、`download/history/commands.py`、`download/history/reporting.py`、`download/market/commands.py`、`download/market/reporting.py`、`download/company/live_sync.py`、`download/company/projections.py`、`download/company/commands.py`、`download/company/reporting.py`、`infra/database/database_workspace.py`、`infra/database/commands.py`、`infra/database/reporting.py`、`infra/disk/commands.py`、`infra/disk/reporting.py`、`core/execution.py`、`core/cli.py`、`core/projection_values.py`、`core/projections.py`、`helpers/shared/tool_cli.py`、`training/unit/scripts/test_training_resources.py`、`training/unit/scripts/test_training_performance.py`、`download/unit/scripts/test_company_publication_audit.py`、`tooling/unit/scripts/test_database_baseline.py`、`tooling/unit/scripts/test_company_material_reporting.py`、`tooling/unit/scripts/test_company_source_reporting.py`、`tooling/unit/scripts/test_tools_maintenance.py`、`download/unit/scripts/test_market_factor_compare.py`、`tooling/unit/scripts/test_disk_diagnostic.py`、`download/unit/scripts/test_company_document_download_audit.py`、`tooling/unit/scripts/test_diagnose_runtime.py`、`download/unit/scripts/test_history_publication_audit.py`、`download/unit/scripts/test_qfq_sqlite.py`、`tooling/unit/scripts/test_check_entrypoint.py`、`download/unit/scripts/test_company_resume_diagnostic.py` |
| `core/execution.py` | Execute selected commands with explicit preparation and dependency contracts. | 显式所选命令；中立prepare钩子 | `core/cli.py`、`tooling/unit/scripts/test_database_baseline.py`、`tooling/unit/scripts/test_tools_maintenance.py`、`tooling/unit/scripts/test_diagnose_runtime.py`、`tooling/unit/scripts/test_check_entrypoint.py` |
| `core/paths.py` | Paths owned by the tool checkout, independent of the selected product. | 中立路由/类型/校验/投影 | `training/cli.py`、`training/options.py`、`training/models/v5_training.py`、`training/models/v5_lite.py`、`training/samples/v5_samples.py`、`training/samples/sample_comparison_worker.py`、`training/samples/training_read_comparison.py`、`training/samples/training_samples.py`、`training/jobs/training_performance.py`、`training/jobs/training_alignment.py`、`training/resources/training_resources.py`、`training/resources/memory.py`、`engineering/migration/cli.py`、`engineering/migration/operations.py`、`engineering/skills/workspace.py`、`engineering/packaging/cli.py`、`engineering/quality/debt.py`、`engineering/quality/cli.py`、`recommendation/pipeline/cli.py`、`recommendation/pipeline/options.py`、`recommendation/web/browser_refresh.py`、`recommendation/web/company_ledger_browser.py`、`recommendation/web/cli.py`、`recommendation/web/options.py`、`recommendation/assets/generation.py`、`recommendation/long/cli.py`、`recommendation/long/options.py`、`recommendation/long/long_watchlist.py`、`download/history/history_sources.py`、`download/history/download_performance.py`、`download/history/history_write_benchmark.py`、`download/history/cli.py`、`download/history/tencent_download.py`、`download/history/repack.py`、`download/history/history_daily_capability.py`、`download/history/baostock_concurrency.py`、`download/history/history_sqlite_performance.py`、`download/history/options.py`、`download/history/history_source_compare.py`、`download/history/baostock_qfq_shadow.py`、`download/history/price_repair_audit.py`、`download/history/history_industry_preparation.py`、`download/market/exchange_security_master.py`、`download/market/tushare_daily.py`、`download/market/cli.py`、`download/market/options.py`、`download/market/tencent_quotes.py`、`download/company/company_field_audit.py`、`download/company/company_pagination_audit.py`、`download/company/company_query_audit.py`、`download/company/live_sync.py`、`download/company/cli.py`、`download/company/options.py`、`download/company/company_pipeline_audit.py`、`download/company/company_resume.py`、`download/company/commands.py`、`download/company/company_ocr_audit.py`、`download/company/company_document_download_audit.py`、`infra/database/database_workspace.py`、`infra/database/database_baseline.py`、`infra/database/database_layout_comparison.py`、`infra/database/cli.py`、`infra/database/options.py`、`infra/disk/cli.py`、`infra/disk/options.py`、`infra/disk/disk_profile.py`、`core/process.py`、`core/cli.py`、`core/catalog.py`、`download/unit/scripts/test_company_live_sync_tool.py`、`tooling/unit/scripts/test_database_baseline.py`、`tooling/unit/scripts/test_tools_maintenance.py`、`tooling/unit/scripts/test_diagnose_runtime.py`、`download/unit/scripts/test_company_source_runtime.py`、`download/unit/scripts/test_history_publication_measurement.py` |
| `core/process.py` | Bounded child processes shared by tools; no business imports. | 限定子进程、超时和20MiB输出 | `core/execution.py`、`tooling/unit/scripts/test_database_baseline.py`、`tooling/unit/scripts/test_tools_maintenance.py`、`tooling/unit/scripts/test_check_entrypoint.py` |
| `core/projection_values.py` | 所属包初始化 | 中立路由/类型/校验/投影 | `training/reporting.py`、`recommendation/pipeline/reporting.py`、`recommendation/web/reporting.py`、`recommendation/long/reporting.py`、`download/history/reporting.py`、`download/market/reporting.py`、`download/company/live_sync.py`、`download/company/projections.py`、`download/company/reporting.py`、`infra/database/reporting.py`、`infra/disk/reporting.py`、`core/execution.py`、`core/projections.py` |
| `core/projections.py` | Generic result classification and selected-owner report composition. | 中立状态归一与所选报告组装 | `recommendation/pipeline/reporting.py`、`core/execution.py`、`core/cli.py`、`training/unit/scripts/test_training_resources.py`、`training/unit/scripts/test_training_performance.py`、`download/unit/scripts/test_company_publication_audit.py`、`tooling/unit/scripts/test_company_material_reporting.py`、`tooling/unit/scripts/test_company_source_reporting.py`、`download/unit/scripts/test_market_factor_compare.py`、`tooling/unit/scripts/test_disk_diagnostic.py`、`download/unit/scripts/test_company_document_download_audit.py`、`tooling/unit/scripts/test_diagnose_runtime.py`、`download/unit/scripts/test_history_publication_audit.py`、`download/unit/scripts/test_qfq_sqlite.py`、`tooling/unit/scripts/test_check_entrypoint.py`、`download/unit/scripts/test_company_resume_diagnostic.py` |
| `core/reporting.py` | Shared output and measurement helpers for internal runtime diagnostics. | 中立状态归一与所选报告组装 | `training/models/v5_training.py`、`training/samples/training_coverage.py`、`training/jobs/training_alignment.py`、`training/resources/training_resources.py`、`recommendation/web/web_health.py`、`recommendation/web/browser_refresh.py`、`recommendation/web/company_ledger_browser.py`、`recommendation/long/long_watchlist.py`、`download/history/history_sources.py`、`download/history/download_performance.py`、`download/history/history_publication_audit.py`、`download/history/qfq_sqlite.py`、`download/history/history_write_benchmark.py`、`download/history/tencent_download.py`、`download/history/history_daily_capability.py`、`download/history/baostock_concurrency.py`、`download/history/history_sqlite_performance.py`、`download/history/history_source_compare.py`、`download/history/baostock_qfq_shadow.py`、`download/history/price_repair_audit.py`、`download/history/history_industry_preparation.py`、`download/market/exchange_security_master.py`、`download/market/tushare_daily.py`、`download/market/market_factor_compare.py`、`download/market/tencent_quotes.py`、`download/market/market_factor_industry.py`、`infra/database/database_baseline.py`、`core/cli.py`、`training/unit/execution/test_selected_training.py`、`training/unit/execution/test_execution_timing.py`、`tooling/unit/scripts/test_runtime_diagnostics_reporting.py` |
| `download/__init__.py` | Internal checks selected by scripts/tools.py. | 包初始化；导入不执行动作 | 所属包导入 |
| `download/company/__init__.py` | Internal checks selected by scripts/tools.py. | 包初始化；导入不执行动作 | 所属包导入 |
| `download/company/cli.py` | Parse only the selected download/company action and validate its inputs. | 业务参数/类型/命令；所选动作持有副作用 | `tooling/unit/scripts/test_tools_business_options.py`、`download/unit/scripts/test_company_source_options.py`、`core/cli.py按catalog动态选择` |
| `download/company/commands.py` | Build only the selected download/company invocation. | 业务参数/类型/命令；所选动作持有副作用 | `tooling/unit/scripts/test_tools_business_options.py`、`download/unit/scripts/test_company_output_isolation.py`、`download/unit/scripts/test_company_source_options.py`、`core/cli.py及tests/helpers/shared/tool_cli.py按catalog动态选择` |
| `download/company/company_document_download_audit.py` | Compare bounded requests for recorded failed PDFs without parsing or active writes. | 显式受限来源请求/外部副本或只读审计 | `download/company/company_resume.py`、`download/unit/scripts/test_company_document_download_audit.py` |
| `download/company/company_field_audit.py` | Read-only field population, source agreement and saved query-union diagnostics. | 显式受限来源请求/外部副本或只读审计 | `download/company/company_publication_audit.py`、`download/company/projections.py`、`download/company/company_resume.py`、`download/unit/scripts/test_company_publication_audit.py` |
| `download/company/company_ocr_audit.py` | Compare OCR candidates on seven failed PDFs without changing the source database. | 显式受限来源请求/外部副本或只读审计 | `download/company/company_resume.py`、`download/unit/scripts/test_company_resume_diagnostic.py` |
| `download/company/company_pagination_audit.py` | Compare complete CNInfo record sets at different page sizes without database writes. | 显式受限来源请求/外部副本或只读审计 | `download/company/company_resume.py`、`download/unit/scripts/test_company_resume_diagnostic.py` |
| `download/company/company_pipeline_audit.py` | Same-document serial/parallel probe with real PDFs and disposable company indexes. | 显式真实sync计量或外部PDF对照；不自动启动 | `download/company/live_sync.py`、`download/company/company_resume.py`、`download/unit/scripts/test_company_live_sync_tool.py`、`download/unit/scripts/test_company_sync_measurement.py`、`download/unit/scripts/test_company_resume_diagnostic.py` |
| `download/company/company_publication_audit.py` | Aggregate local company output and receipt evidence without reading document bodies. | 显式受限来源请求/外部副本或只读审计 | `download/company/company_query_audit.py`、`download/unit/scripts/test_company_publication_audit.py` |
| `download/company/company_query_audit.py` | Bounded real CNInfo count/filter audit; local databases are strictly read-only. | 显式受限来源请求/外部副本或只读审计 | `download/company/company_resume.py`、`download/component/company/test_company_pipeline.py`、`download/unit/scripts/test_company_publication_audit.py`、`download/unit/scripts/test_company_resume_diagnostic.py` |
| `download/company/company_resume.py` | Read-only or isolated company audits; live sync has its own foreground action. | 显式受限来源请求/外部副本或只读审计 | `download/company/commands.py`、`core/catalog.py`、`download/unit/scripts/test_company_sync_measurement.py`、`download/unit/scripts/test_company_resume_diagnostic.py` |
| `download/company/company_structured_field_audit.py` | Bounded, read-only samples of structured alternatives to company PDF extraction. | 显式受限来源请求/外部副本或只读审计 | `download/company/research_source_compare.py`、`download/unit/scripts/test_company_structured_field_audit.py` |
| `download/company/options.py` | Immutable inputs owned by the download/company diagnostic adapter. | 业务参数/类型/命令；所选动作持有副作用 | `download/company/cli.py`、`download/company/commands.py`、`helpers/shared/tool_cli.py`、`download/unit/scripts/test_company_output_isolation.py` |
| `download/company/projections.py` | 所属包初始化 | 所选业务状态/字段投影；无独立I/O | `download/company/reporting.py` |
| `download/company/research_source_compare.py` | Read-only qualification of production company evidence source adapters. | 显式受限来源请求/外部副本或只读审计 | `download/company/commands.py`、`core/catalog.py`、`download/unit/scripts/test_company_structured_field_audit.py`、`download/unit/scripts/test_company_source_runtime.py` |
| `download/history/__init__.py` | Internal checks selected by scripts/tools.py. | 包初始化；导入不执行动作 | 所属包导入 |
| `download/history/baostock_concurrency.py` | Run an isolated BaoStock serial rate-limit experiment. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py`、`tooling/unit/scripts/test_diagnose_runtime.py` |
| `download/history/baostock_qfq_shadow.py` | Read-only BaoStock qfq reuse shadow report for the active history snapshot. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py`、`download/unit/scripts/test_baostock_qfq_shadow.py` |
| `download/history/cli.py` | Parse only the selected download/history action and validate its inputs. | 业务参数/类型/命令；所选动作持有副作用 | `tooling/unit/scripts/test_tools_control_options.py`、`core/cli.py按catalog动态选择` |
| `download/history/commands.py` | Build only the selected download/history invocation. | 业务参数/类型/命令；所选动作持有副作用 | `tooling/unit/scripts/test_tools_control_options.py`、`core/cli.py及tests/helpers/shared/tool_cli.py按catalog动态选择` |
| `download/history/download_performance.py` | Offline download persistence measurements on disposable, synthetic databases. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py` |
| `download/history/history_daily_capability.py` | Audit the configured daily-history sources without writing an archive. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py`、`download/unit/scripts/test_history_daily_capability.py` |
| `download/history/history_fallback.py` | Remote daily-history fallback for the standalone source diagnostic. | 显式受限来源/只读发布或外部fixture | `download/history/history_sources.py`、`helpers/recommendation/market_data.py` |
| `download/history/history_industry_preparation.py` | 行业来源诊断；仅显式中上协当前公告／全表隔离核验，旧BaoStock两日诊断已删除 | 候选只写显式仓库外原件和解析投影，不发布行业／历史 | `download/history/commands.py`、`core/catalog.py` |
| `download/history/history_parquet.py` | Offline Parquet candidate, derived exclusively from a pinned SQLite publication. | 显式受限来源/只读发布或外部fixture | `training/samples/training_read_comparison.py`、`download/unit/scripts/test_training_samples.py` |
| `download/history/history_publication_audit.py` | Read-only verification of published history and durable download receipts. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py`、`download/unit/scripts/test_history_publication_audit.py` |
| `download/history/history_publication_measurement.py` | Scoped publication profiling in an isolated diagnostic worker; never installed in the product. | 显式受限来源/只读发布或外部fixture | `download/history/history_write_benchmark.py`、`download/unit/scripts/test_history_publication_measurement.py` |
| `download/history/history_source_compare.py` | Bounded same-input history pipelines; network and temporary output only. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py` |
| `download/history/history_sources.py` | Sample feature and raw/qfq outcome histories in bounded worker waves. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py`、`download/unit/scripts/test_history_sources.py` |
| `download/history/history_sqlite_performance.py` | Read-only physical layout summary for the published monthly history SQLite databases. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py`、`tooling/unit/scripts/test_diagnose_runtime.py`、`training/unit/research/infra/test_history_training_input.py` |
| `download/history/history_write_benchmark.py` | Repeatable offline first-build and incremental-write measurements outside active data. | 显式受限来源/只读发布或外部fixture | `training/samples/training_read_comparison.py`、`download/history/commands.py`、`infra/database/database_baseline.py`、`infra/database/database_layout_comparison.py`、`core/catalog.py`、`download/unit/scripts/test_history_publication_measurement.py` |
| `download/history/options.py` | Immutable inputs owned by the download/history diagnostic adapter. | 业务参数/类型/命令；所选动作持有副作用 | `download/history/cli.py`、`download/history/commands.py`、`helpers/shared/tool_cli.py` |
| `download/history/price_repair_audit.py` | Export real problem windows, repair isolated SQLite files, verify immutable parents. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py` |
| `download/history/qfq_sqlite.py` | Read-only integrity and paired-publication audit of the shared daily window. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py`、`tooling/unit/scripts/test_diagnose_runtime.py`、`download/unit/scripts/test_qfq_sqlite.py` |
| `download/history/repack.py` | Explicit foreground actions for the fixed-path history partition repack. | 前台维护；保留隔离与训练证据围栏 | `core/catalog.py`、`download/contract/history/test_history_partition_repack_contract.py`、`tooling/unit/scripts/test_tools_maintenance.py` |
| `download/history/tencent_download.py` | Measure the actual Tencent qfq updater and compare history tails without publication. | 显式受限来源/只读发布或外部fixture | `download/history/commands.py`、`core/catalog.py`、`download/component/history/test_qfq_windows.py`、`download/unit/scripts/test_tencent_history_coverage.py`、`tooling/unit/scripts/test_diagnose_runtime.py` |
| `download/market/__init__.py` | Internal checks selected by scripts/tools.py. | 包初始化；导入不执行动作 | 所属包导入 |
| `download/market/cli.py` | Parse only the selected download/market action and validate its inputs. | 业务参数/类型/命令；所选动作持有副作用 | `tooling/unit/scripts/test_tools_control_options.py`、`core/cli.py按catalog动态选择` |
| `download/market/commands.py` | Build only the selected download/market invocation. | 业务参数/类型/命令；所选动作持有副作用 | `tooling/unit/scripts/test_tools_control_options.py`、`core/cli.py及tests/helpers/shared/tool_cli.py按catalog动态选择` |
| `download/market/exchange_security_master.py` | Probe the official SSE/SZSE security-master snapshot and report bounded coverage counts. | 显式受限来源请求/只读输入；不发布 | `download/market/commands.py`、`core/catalog.py`、`download/unit/scripts/test_security_master_probe.py` |
| `download/market/market_factor_audit.py` | Field-level completeness projections for supplier/factor diagnostics. | 显式受限来源请求/只读输入；不发布 | `download/market/market_factor_compare.py`、`download/unit/scripts/test_market_factor_compare.py` |
| `download/market/market_factor_auxiliary.py` | Bounded, read-only probes for inputs unavailable from spot quote endpoints. | 显式受限来源请求/只读输入；不发布 | `download/market/market_factor_compare.py`、`download/unit/scripts/test_market_factor_compare.py` |
| `download/market/market_factor_industry.py` | Current CNINFO industry-reference capability for market-factor comparisons. | 显式受限来源请求/只读输入；不发布 | `download/market/market_factor_compare.py`、`download/unit/scripts/test_market_factor_compare.py` |
| `download/market/market_factor_budget.py` | Hard wall-clock budgets for live market-factor diagnostics. | 显式受限来源请求/只读输入；不发布 | `download/market/market_factor_compare.py`、`download/market/cli.py`、`download/company/cli.py`、`download/unit/scripts/test_market_factor_compare.py` |
| `download/market/market_factor_compare.py` | Bounded live quote comparisons with fixed qfq inputs and isolated production scoring. | 显式受限来源请求/只读输入；不发布 | `download/market/commands.py`、`core/catalog.py`、`download/unit/scripts/test_market_factor_compare.py` |
| `download/market/market_factor_eastmoney.py` | Diagnostic-only Eastmoney spot probe; this endpoint has no production lane. | 显式受限来源请求/只读输入；不发布 | `download/market/market_factor_compare.py`、`download/unit/scripts/test_market_factor_compare.py` |
| `download/market/market_factor_inputs.py` | Read-only, fixed local inputs for real supplier comparisons. | 显式受限来源请求/只读输入；不发布 | `download/market/market_factor_compare.py`、`download/market/market_factor_audit.py`、`download/market/tencent_quotes.py`、`download/unit/scripts/test_market_factor_compare.py` |
| `download/market/market_factor_routes.py` | Route plans for the live market-factor comparison. | 显式受限来源请求/只读输入；不发布 | `download/market/market_factor_compare.py` |
| `download/market/market_factor_text.py` | Human-readable terminal projection for the market factor comparison. | 显式受限来源请求/只读输入；不发布 | `download/market/reporting.py`、`download/unit/scripts/test_market_factor_compare.py` |
| `download/market/options.py` | Immutable inputs owned by the download/market diagnostic adapter. | 业务参数/类型/命令；所选动作持有副作用 | `download/market/cli.py`、`download/market/commands.py`、`helpers/shared/tool_cli.py` |
| `download/market/tencent_quotes.py` | Sample Tencent quotes repeatedly and report source-version stability and request latency. | 显式受限来源请求/只读输入；不发布 | `download/market/commands.py`、`core/catalog.py` |
| `download/market/tushare_daily.py` | Probe the configured Tushare daily capability without exposing credentials or prices. | 显式受限来源请求/只读输入；不发布 | `download/market/commands.py`、`core/catalog.py`、`download/unit/scripts/test_tushare_daily.py` |
| `engineering/__init__.py` | Internal checks selected by scripts/tools.py. | 包初始化；导入不执行动作 | 所属包导入 |
| `engineering/migration/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `engineering/migration/cli.py` | Explicit inspection, candidate construction and rollback for isolated migration. | 显式外部迁移/回滚；codec无独立入口 | `core/catalog.py`、`tooling/unit/scripts/test_tools_maintenance.py`、`core/cli.py按catalog动态选择` |
| `engineering/migration/codec.py` | Explicit legacy-format decoding for isolated data migration; never an active read path. | 显式外部迁移/回滚；codec无独立入口 | `engineering/migration/operations.py`、`tooling/unit/scripts/test_migrate_runtime_data.py` |
| `engineering/migration/operations.py` | Build and verify a data candidate from an external, stopped legacy workspace copy. | 显式外部迁移/回滚；codec无独立入口 | `engineering/migration/cli.py`、`tooling/unit/scripts/test_migrate_runtime_data.py`、`tooling/unit/scripts/test_tools_maintenance.py` |
| `engineering/packaging/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `engineering/packaging/cli.py` | Install the built wheel outside the repository and verify public entrypoints and resources. | 显式隔离venv安装；不切换活动环境 | `core/catalog.py`、`core/cli.py按catalog动态选择` |
| `engineering/quality/__init__.py` | Opt-in source transformations; importing this package performs no work. | 包初始化；导入不执行动作 | 所属包导入 |
| `engineering/quality/cli.py` | Explicit opt-in commands; the existing no-argument gate is unchanged. | 审计只读；format/rename另需授权 | `core/catalog.py`、`tooling/unit/scripts/test_code_quality_tools.py`、`core/cli.py按catalog动态选择` |
| `engineering/quality/debt.py` | Keep refactor naming and complexity debt from changing without review. | 审计只读；format/rename另需授权 | `core/catalog.py`、`system/contract/test_refactor_quality_gate.py` |
| `engineering/quality/files.py` | Bounded source selection and optimistic, per-file replacement. | 审计只读；format/rename另需授权 | `engineering/quality/formatting.py`、`engineering/quality/cli.py`、`engineering/quality/naming.py`、`tooling/unit/scripts/test_code_quality_tools.py` |
| `engineering/quality/formatting.py` | Deterministic comma policy followed by the pinned Ruff formatter. | 审计只读；format/rename另需授权 | `engineering/quality/cli.py`、`tooling/unit/scripts/test_code_quality_tools.py` |
| `engineering/quality/manifest.py` | Strict JSON boundary for reviewable, content-bound rename batches. | 审计只读；format/rename另需授权 | `engineering/quality/rename_worker.py`、`engineering/quality/cli.py`、`tooling/unit/scripts/test_code_quality_tools.py` |
| `engineering/quality/naming.py` | Single machine rule set for opt-in naming audits and rename admission. | 审计只读；format/rename另需授权 | `engineering/quality/rename_worker.py`、`engineering/quality/cli.py`、`system/contract/test_professional_naming_contract.py`、`tooling/unit/scripts/test_code_quality_tools.py` |
| `engineering/quality/rename_worker.py` | Rope runs only in a disposable source copy under a parent-enforced timeout. | 审计只读；format/rename另需授权 | `engineering/quality/cli.py`、`core/catalog.py`、`tooling/unit/scripts/test_code_quality_tools.py` |
| `engineering/skills/__init__.py` | Opt-in, isolated behavioral evaluation of the repository skills. | 包初始化；导入不执行动作 | 所属包导入 |
| `engineering/skills/cases.py` | Small synthetic tasks; acceptance rules remain outside the agent workspace. | compare只读；run为显式付费推理与外部合成目录 | `engineering/skills/cli.py`、`tooling/unit/scripts/test_skill_evaluation.py` |
| `engineering/skills/cli.py` | Opt-in CLI; no model requests occur when imported or when running tests/help. | compare只读；run为显式付费推理与外部合成目录 | `core/catalog.py`、`tooling/unit/scripts/test_check_entrypoint.py`、`core/cli.py按catalog动态选择` |
| `engineering/skills/contracts.py` | Immutable inputs and evidence; JSON is decoded only by trace/report adapters. | compare只读；run为显式付费推理与外部合成目录 | `engineering/skills/cases.py`、`engineering/skills/reports.py`、`engineering/skills/process.py`、`engineering/skills/workspace.py`、`engineering/skills/cli.py`、`engineering/skills/trace.py`、`engineering/skills/scoring.py`、`tooling/unit/scripts/test_skill_evaluation.py` |
| `engineering/skills/process.py` | Bounded subprocess lifetime, including descendants and combined output. | compare只读；run为显式付费推理与外部合成目录 | `engineering/skills/cli.py`、`tooling/unit/scripts/test_skill_evaluation.py` |
| `engineering/skills/reports.py` | Explicit JSON/Markdown projection and bounded report comparison. | compare只读；run为显式付费推理与外部合成目录 | `engineering/skills/cli.py`、`tooling/unit/scripts/test_skill_evaluation.py` |
| `engineering/skills/scoring.py` | Independent static and Git acceptance checks; never execute generated Python. | compare只读；run为显式付费推理与外部合成目录 | `engineering/skills/cli.py`、`tooling/unit/scripts/test_skill_evaluation.py` |
| `engineering/skills/trace.py` | Decode Codex exec JSONL conservatively; unknown events never prove a pass. | compare只读；run为显式付费推理与外部合成目录 | `engineering/skills/cli.py`、`tooling/unit/scripts/test_skill_evaluation.py` |
| `engineering/skills/workspace.py` | Disposable repositories, with no production files or original remote copied. | compare只读；run为显式付费推理与外部合成目录 | `engineering/skills/cli.py`、`engineering/skills/scoring.py`、`tooling/unit/scripts/test_skill_evaluation.py` |
| `infra/__init__.py` | Internal checks selected by scripts/tools.py. | 包初始化；导入不执行动作 | 所属包导入 |
| `infra/database/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `infra/database/cli.py` | Parse only the selected infra/database action and validate its inputs. | 业务参数/类型/命令；所选动作持有副作用 | `core/cli.py按catalog动态选择` |
| `infra/database/commands.py` | Build only the selected infra/database invocation. | 业务参数/类型/命令；所选动作持有副作用 | `core/cli.py及tests/helpers/shared/tool_cli.py按catalog动态选择` |
| `infra/database/database_baseline.py` | Offline joint/single sync and startup-input baselines on disposable databases. | 显式外部fixture；有界SQLite与子进程 | `infra/database/commands.py`、`core/catalog.py`、`tooling/unit/scripts/test_database_baseline.py` |
| `infra/database/database_layout_comparison.py` | Isolated, alternating measurements of the retired and current persistence layouts. | 显式外部fixture；有界SQLite与子进程 | `download/history/history_write_benchmark.py`、`core/catalog.py`、`tooling/unit/scripts/test_tools_business_options.py` |
| `infra/database/database_measurement.py` | Scoped SQLite instrumentation for isolated diagnostic workers only. | 显式外部fixture；有界SQLite与子进程 | `download/history/history_write_benchmark.py`、`download/history/history_publication_measurement.py`、`download/history/history_sqlite_performance.py`、`download/company/company_pipeline_audit.py`、`infra/database/database_baseline.py`、`tooling/unit/scripts/test_database_baseline.py` |
| `infra/database/database_workspace.py` | Explicit external database workspaces and truthful filesystem evidence. | 显式外部fixture；有界SQLite与子进程 | `training/samples/training_samples.py`、`download/history/commands.py`、`infra/database/commands.py`、`tooling/unit/scripts/test_tools_business_options.py`、`tooling/unit/scripts/test_database_baseline.py` |
| `infra/database/options.py` | Immutable inputs owned by the infra/database diagnostic adapter. | 业务参数/类型/命令；所选动作持有副作用 | `infra/database/cli.py`、`infra/database/commands.py`、`helpers/shared/tool_cli.py` |
| `infra/disk/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `infra/disk/cli.py` | Parse only the selected infra/disk action and validate its inputs. | 业务参数/类型/命令；所选动作持有副作用 | `core/cli.py按catalog动态选择` |
| `infra/disk/commands.py` | Build only the selected infra/disk invocation. | 业务参数/类型/命令；所选动作持有副作用 | `core/cli.py及tests/helpers/shared/tool_cli.py按catalog动态选择` |
| `infra/disk/disk_policy_audit.py` | Read-only source inventory of the shared SQLite policy injection boundary. | 只读介质/策略盘点；不作性能通过证据 | `infra/disk/disk_profile.py`、`tooling/unit/scripts/test_disk_diagnostic.py` |
| `infra/disk/disk_profile.py` | Read-only disk inventory for the unified diagnostic command. | 只读介质/策略盘点；不作性能通过证据 | `infra/disk/commands.py`、`core/catalog.py`、`tooling/unit/scripts/test_disk_diagnostic.py` |
| `infra/disk/options.py` | Immutable inputs owned by the infra/disk diagnostic adapter. | 业务参数/类型/命令；所选动作持有副作用 | `infra/disk/cli.py`、`infra/disk/commands.py`、`helpers/shared/tool_cli.py` |
| `recommendation/__init__.py` | Internal checks selected by scripts/tools.py. | 包初始化；导入不执行动作 | 所属包导入 |
| `recommendation/assets/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `recommendation/assets/cli.py` | Explicit, separately selected watchlist consistency and generation actions. | consistency只读；generate显式重建JS | `core/catalog.py`、`tooling/unit/scripts/test_tools_maintenance.py`、`tooling/unit/scripts/test_check_entrypoint.py`、`core/cli.py按catalog动态选择` |
| `recommendation/assets/generation.py` | Generate the packaged long-watchlist JavaScript asset from its JSON source. | consistency只读；generate显式重建JS | `recommendation/assets/cli.py`、`tooling/unit/scripts/test_tools_maintenance.py` |
| `recommendation/long/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `recommendation/long/cli.py` | Parse only the selected recommendation/long action and validate its inputs. | 业务参数/类型/命令；所选动作持有副作用 | `recommendation/unit/scripts/test_tools_recommendation_options.py`、`core/cli.py按catalog动态选择` |
| `recommendation/long/commands.py` | Build only the selected recommendation/long invocation. | 业务参数/类型/命令；所选动作持有副作用 | `recommendation/unit/scripts/test_tools_recommendation_options.py`、`core/cli.py及tests/helpers/shared/tool_cli.py按catalog动态选择` |
| `recommendation/long/long_watchlist.py` | Read-only admission audit; supplier coverage never proves absence of unknown risk. | 只读准入；不发布、不评分 | `recommendation/long/commands.py`、`core/catalog.py`、`recommendation/unit/scripts/test_long_watchlist.py` |
| `recommendation/long/options.py` | Immutable inputs owned by the recommendation/long diagnostic adapter. | 业务参数/类型/命令；所选动作持有副作用 | `recommendation/long/cli.py`、`recommendation/long/commands.py`、`helpers/shared/tool_cli.py` |
| `recommendation/pipeline/cli.py` | Parse only the selected recommendation/pipeline action and validate its inputs. | 业务参数/类型/命令；所选动作持有副作用 | `recommendation/web/cli.py`、`core/cli.py按catalog动态选择` |
| `recommendation/pipeline/commands.py` | Build only the selected recommendation/pipeline invocation. | 业务参数/类型/命令；所选动作持有副作用 | `core/cli.py及tests/helpers/shared/tool_cli.py按catalog动态选择` |
| `recommendation/pipeline/options.py` | Immutable inputs owned by the recommendation/pipeline diagnostic adapter. | 业务参数/类型/命令；所选动作持有副作用 | `recommendation/pipeline/cli.py`、`recommendation/pipeline/commands.py`、`recommendation/web/options.py`、`helpers/shared/tool_cli.py` |
| `recommendation/web/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `recommendation/web/browser_refresh.py` | Measure production scheduler-to-SSE-to-Firefox DOM refresh intervals with deterministic quotes. | 显式只读Web/浏览器；临时资源受限 | `recommendation/web/commands.py`、`core/catalog.py`、`tooling/unit/scripts/test_diagnose_runtime.py`、`recommendation/unit/scripts/test_desktop_dashboard.py` |
| `recommendation/web/cli.py` | Parse only the selected recommendation/web action and validate its inputs. | 业务参数/类型/命令；所选动作持有副作用 | `recommendation/unit/scripts/test_tools_recommendation_options.py`、`core/cli.py按catalog动态选择` |
| `recommendation/web/commands.py` | Build only the selected recommendation/web invocation. | 业务参数/类型/命令；所选动作持有副作用 | `recommendation/unit/scripts/test_tools_recommendation_options.py`、`core/cli.py及tests/helpers/shared/tool_cli.py按catalog动态选择` |
| `recommendation/web/company_ledger_browser.py` | Render the production score column and drawer in isolated desktop Chrome. | 显式只读Web/浏览器；临时资源受限 | `recommendation/web/commands.py`、`core/catalog.py`、`recommendation/unit/scripts/test_company_ledger_tool.py` |
| `recommendation/web/options.py` | Immutable inputs owned by the recommendation/web diagnostic adapter. | 业务参数/类型/命令；所选动作持有副作用 | `recommendation/web/cli.py`、`recommendation/web/commands.py`、`helpers/shared/tool_cli.py` |
| `recommendation/web/web_health.py` | Sample live 统一 Web APIs and detect recommendation-funnel state anomalies. | 显式只读Web/浏览器；临时资源受限 | `recommendation/web/commands.py`、`core/catalog.py`、`recommendation/unit/scripts/test_tools_recommendation_options.py`、`recommendation/unit/scripts/test_web_health.py` |
| `recommendation/web/web_health_contract.py` | Typed parsing boundary for sanitized 统一 Web-health samples. | 显式只读Web/浏览器；临时资源受限 | `recommendation/web/web_health.py` |
| `tools.py` | Repository tools: select exactly one business/module/action. | 仅选择；执行边界由所选动作负责 | `training/samples/training_samples.py`、`training/jobs/training_performance.py`、`engineering/quality/cli.py`、`download/history/download_performance.py`、`infra/database/database_baseline.py`、`infra/database/database_layout_comparison.py`、`core/cli.py`、`core/catalog.py`、`system/contract/test_current_product_contract.py`、`tooling/unit/scripts/test_diagnose_runtime.py`、`tooling/unit/scripts/test_check_entrypoint.py`、`download/unit/scripts/test_training_samples.py`、`Makefile`、`README.md`、`run.sh`、`run.ps1` |
| `training/__init__.py` | Internal checks selected by scripts/tools.py. | 包初始化；导入不执行动作 | 所属包导入 |
| `training/cli.py` | Parse only the selected training action and validate its inputs. | 业务参数/类型/命令；所选动作持有副作用 | `tooling/unit/scripts/test_tools_control_options.py`、`training/unit/scripts/test_training_output_isolation.py`、`core/cli.py按catalog动态选择` |
| `training/commands.py` | Build only the selected training invocation. | 业务参数/类型/命令；所选动作持有副作用 | `tooling/unit/scripts/test_tools_control_options.py`、`core/cli.py及tests/helpers/shared/tool_cli.py按catalog动态选择` |
| `training/jobs/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `training/jobs/training_alignment.py` | Train all alignment contracts against an explicitly external frozen history copy. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/commands.py`、`training/jobs/training_performance.py`、`core/catalog.py`、`training/unit/execution/test_scheduling_candidates.py`、`training/unit/scripts/test_training_alignment.py`、`training/unit/scripts/test_training_performance.py`、`training/unit/scripts/test_training_output_isolation.py`、`tooling/unit/scripts/test_check_entrypoint.py` |
| `training/jobs/training_performance.py` | Plan fresh, directly supervised training processes for comparable benchmark rounds. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/commands.py`、`training/reporting.py`、`training/jobs/training_alignment.py`、`training/unit/scripts/test_training_performance.py`、`training/unit/scripts/test_training_output_isolation.py` |
| `training/jobs/training_source_guard.py` | Keep diagnostic fitting on published history read-only, including due projections. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/samples/v5_quality_samples.py`、`training/jobs/training_alignment.py`、`training/unit/scripts/test_training_alignment.py` |
| `training/models/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `training/models/v5_kernels.py` | Bounded synthetic float64 comparison against a frozen Conv1d reference. | 显式有界纯计算；不读取生产模型 | `training/models/v5_training.py`、`training/unit/profiles/v5/test_network.py` |
| `training/models/v5_lite.py` | Bounded paired V5 lightweight compute and real-sample acceptance diagnostic. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/commands.py`、`core/catalog.py`、`training/unit/execution/test_resource_settings.py`、`training/unit/profiles/v5/test_v5_lifecycle.py`、`training/unit/scripts/test_v5_lite.py` |
| `training/models/v5_training.py` | Real bounded-industry V5 fit with read-only source and external disposable outputs. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/commands.py`、`core/catalog.py`、`system/contract/test_version_naming_boundary.py` |
| `training/options.py` | Immutable inputs owned by the training diagnostic adapter. | 业务参数/类型/命令；所选动作持有副作用 | `training/cli.py`、`training/commands.py`、`training/jobs/training_performance.py`、`helpers/shared/tool_cli.py`、`training/unit/scripts/test_training_output_isolation.py` |
| `training/resources/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `training/resources/memory.py` | Run the fenced V3 training job and emit Tomorrow-compatible repack evidence. | 显式资源/计量；memory可真实训练并写外部结果 | `core/catalog.py`、`tooling/unit/scripts/test_check_entrypoint.py`、`training/unit/scripts/test_check_tomorrow_training_memory.py` |
| `training/resources/scheduling_candidates.py` | Measured-priority CPU admission experiments for isolated training diagnostics. | 显式资源/计量；memory可真实训练并写外部结果 | `training/jobs/training_alignment.py`、`training/unit/execution/test_scheduling_candidates.py` |
| `training/resources/training_process_measurement.py` | Observe each diagnostic training call without changing product resource limits. | 显式资源/计量；memory可真实训练并写外部结果 | `training/jobs/training_alignment.py`、`training/unit/scripts/test_training_alignment.py` |
| `training/resources/training_resources.py` | Bounded CPU admission smoke test; no history, models, configuration writes or network. | 显式资源/计量；memory可真实训练并写外部结果 | `training/commands.py`、`core/catalog.py`、`training/unit/scripts/test_training_resources.py` |
| `training/samples/__init__.py` | Repository tool package. | 包初始化；导入不执行动作 | 所属包导入 |
| `training/samples/computation_candidates.py` | Explicit process-local sample experiments; production never imports this module. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/samples/sample_comparison_worker.py`、`training/jobs/training_alignment.py`、`training/unit/execution/test_computation_candidates.py` |
| `training/samples/sample_comparison_worker.py` | One isolated checkout's sample write/read measurements; never trains or publishes models. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/samples/training_samples.py`、`infra/database/database_baseline.py`、`infra/database/database_layout_comparison.py`、`core/catalog.py`、`training/unit/execution/test_computation_candidates.py`、`training/unit/scripts/test_training_output_isolation.py` |
| `training/samples/training_coverage.py` | Read-only coverage audit of the current named sample format. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/commands.py`、`core/catalog.py`、`training/unit/scripts/test_training_coverage.py` |
| `training/samples/training_read_comparison.py` | Published-history fixtures and end-to-end SQLite/Parquet preparation probes. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/samples/training_samples.py`、`core/catalog.py`、`training/unit/execution/test_scheduling_candidates.py`、`training/unit/scripts/test_training_alignment.py`、`tooling/unit/scripts/test_tools_business_options.py`、`download/unit/scripts/test_training_samples.py` |
| `training/samples/training_samples.py` | Five alternating sample-layout and columnar-read experiments on external fixed fixtures. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/commands.py`、`core/catalog.py`、`tooling/unit/scripts/test_tools_business_options.py`、`training/unit/scripts/test_training_output_isolation.py` |
| `training/samples/v5_quality_samples.py` | Retain explicit V5 quality inputs under samples without fitting or publishing models. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/models/v5_lite.py`、`training/unit/profiles/v5/test_v5_quality_samples.py` |
| `training/samples/v5_samples.py` | Isolated exact-value comparison of V5 wide columns and compact sequence blocks. | 显式外部样本/历史/模型；隔离计算，不写活动数据 | `training/commands.py`、`core/catalog.py`、`system/contract/test_version_naming_boundary.py`、`training/unit/scripts/test_v5_samples.py` |

| `download/company/reporting.py` | Company source and publication diagnostic projections. | 所选业务状态/字段投影；无独立I/O | `core/catalog.py` |
| `download/history/reporting.py` | History diagnostic evidence projections. | 所选业务状态/字段投影；无独立I/O | `core/catalog.py` |
| `download/market/reporting.py` | Market diagnostics, source evidence and the explicitly selected text view. | 所选业务状态/字段投影；无独立I/O | `core/catalog.py` |
| `engineering/packaging/reporting.py` | Installed-wheel public evidence projection. | 所选业务状态/字段投影；无独立I/O | `core/catalog.py` |
| `infra/database/reporting.py` | Technical diagnostic result projection for this owner. | 所选业务状态/字段投影；无独立I/O | `core/catalog.py` |
| `infra/disk/reporting.py` | Technical diagnostic result projection for this owner. | 所选业务状态/字段投影；无独立I/O | `core/catalog.py` |
| `recommendation/long/reporting.py` | Long watchlist admission result projection. | 所选业务状态/字段投影；无独立I/O | `core/catalog.py` |
| `recommendation/pipeline/reporting.py` | Pipeline snapshot and readiness status, evidence and local degradation. | 所选业务状态/字段投影；无独立I/O | `core/catalog.py` |
| `recommendation/web/reporting.py` | Read-only Web and browser diagnostic projections. | 所选业务状态/字段投影；无独立I/O | `core/catalog.py`、`tooling/unit/scripts/test_check_entrypoint.py` |
| `training/reporting.py` | Training diagnostic evidence and benchmark completion projections. | 所选业务状态/字段投影；无独立I/O | `core/catalog.py` |

| `download/company/live_sync.py` | Explicit foreground measurement of the existing company sync against current data. | 显式前台真实同步及外部计量工件；本轮只mock验证 | `core/catalog.py`、`download/unit/scripts/test_company_live_sync_tool.py` |

| `training/output_paths.py` | Keep diagnostic outputs separate from every selected product checkout. | 路径隔离预检；不创建或清理目录 | `training/cli.py`、`training/samples/sample_comparison_worker.py`、`training/samples/training_samples.py`、`training/jobs/training_performance.py`、`training/jobs/training_alignment.py`、`training/unit/scripts/test_training_output_isolation.py` |

| `download/company/output_paths.py` | Reject company replay outputs that overlap protected runtime locations. | 路径隔离预检；不创建或清理目录 | `download/company/company_resume.py`、`download/company/commands.py`、`download/unit/scripts/test_company_output_isolation.py` |

代码之外，Makefile的lint、long-watchlist-check、test-release、performance-check、browser-performance-check及diagnose-list/local均调用唯一入口；根README、两平台启动帮助、启用Python skill和测试同步使用新命令。产品CLI仍拥有产品配置、状态和性能用例，工具复用它们，不从产品包反向导入scripts。pytest只切换消费者与相应命名清单，不添加全局路径注入或新的收集钩子。进度与实际验证只记docs/new/g_施工进度.md原阶段338。

## 荐股准备、预载与V5行业的离线候选

连续回放新增`--performance-scoring-candidate`，取值`baseline/prepared_reuse/v5_industry_parallel/combined`，只允许`recommendation/pipeline/latency`且明确设置replay ticks；`combined`组合本段两项，不自动启用原始因子候选。原始因子仍由独立的`--performance-factor-candidate raw_factor_reuse`显式选择，两项可以在同一冻结版本组合对照。默认生产、默认诊断均baseline。

- `prepared_reuse`：完整输入和配置身份相同才复用资格及板内参考。仍由原owner排名和计算本地分，风险/执行门变化必失效；`preparation_identity_and_cache`记录哈希、等待、缓存查询和写入的成本。输出`scoring_reuse`的requests/calculations/hits/bypasses；未获预算时不缓存但保留本次结果，共用既有feature_batches字节预算。
- `v5_industry_parallel`：诊断明确创建并关闭独立两worker预测池，不能复用已经占满的档位执行池。只并行不同的行业，保留每行业256行批次和模型独立性；按原输入顺序收集，错误/KeyboardInterrupt先取消排队任务并等自己的在途任务结束。`industry_prediction`报告完成、失败、取消、在途及峰值，在途不为0不能算完整通过。不改原生库线程数，不把fixture预测器当作原生TCN验收。

```bash
.venv/bin/python3 scripts/tools.py --business recommendation --module pipeline --action latency \
  --runtime-config /external/frozen/config/runtime.json --scoring-profile v1,v2,v3,v5 \
  --performance-replay-ticks 5 --performance-scoring-candidate combined \
  --command-timeout-seconds 1800 --report /external/scoring-candidate.json
```

预载候选复用已有`download/history/sqlite`的显式preload路径，新增`--history-preload-context-candidate baseline/metrics_reuse`。metrics_reuse只缓存原统计owner的相同末60根日线，保留61/251日独立日期/计数/锚点，最多两份临时统计，收尾清空；摘要中的`context_reuse`报告请求、计算、命中、清理前条数及清理后条数。它不缓存历史行、不新增持久化派生库，也不自动执行数据库写入基线或额外feature审计。

```bash
.venv/bin/python3 scripts/tools.py --business download --module history --action sqlite \
  --history-root /external/frozen-data/market/history --runtime-config /external/frozen/config/runtime.json \
  --history-preload-audit --history-preload-profiles v1,v2,v3,v5 \
  --history-preload-context-candidate metrics_reuse \
  --command-timeout-seconds 1800 --report /external/preload-candidate.json
```

C后台分别去掉候选参数取得baseline；固定源快照、配置、全部模型、源码及同一实际PC，先比对完整结果、原因、六席和输入身份，再测总时间、CPU/RSS/I/O、驻留与取消回收。三种计算候选及预载候选都尚未取得生产启用证据。行业输入须已具备当前完整结构，旧库不做schema升级；不修改活动行业/价格库，不以失败的预载或旧进程补签。

公司事实预载统一读取`--runtime-config`关联scheduler的`pipeline.company_preload`，总时限默认30秒、发布版本变化最多尝试3次；Long准入诊断沿用同一设置，删除原先没有消费者的`--timeout-seconds`参数。该时限只约束预载工作，统一入口的`--command-timeout-seconds`仍负责整个诊断进程的回收。
