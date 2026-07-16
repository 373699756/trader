# 低 Token 仓库探测索引

- 仓库根目录: `/home/cp/Public/trader`
- 生成时间: 2026-07-11T23:17:15Z
- 文件扫描上限: 20000
- 每类候选输出上限: 30
- 每类 Ctags 符号输出上限: 20

该文件只用于缩小源码分析范围；最终结论必须写回两份标准分析文档，并用源码片段或符号路径验证。
默认探测只生成轻量摘要，不运行 make/bear，不创建 GTAGS/cscope 全量索引。

## 仓库规模与构建线索

- 扫描文件数: 168
- 源码/头文件候选数: 0
- 轻量索引用源码/头文件数: 0

### 文件类型 Top

| 后缀 | 数量 |
| --- | --- |
| `.py` | 132 |
| `.js` | 10 |
| `.md` | 5 |
| `.json` | 5 |
| `<none>` | 4 |
| `.sqlite3` | 4 |
| `.txt` | 1 |
| `.ini` | 1 |
| `.bat` | 1 |
| `.sh` | 1 |
| `.ps1` | 1 |
| `.css` | 1 |
| `.tag` | 1 |
| `.html` | 1 |

### 顶层目录 Top

| 目录 | 文件数 |
| --- | --- |
| `stock_analyzer` | 95 |
| `tests` | 36 |
| `static` | 11 |
| `.` | 8 |
| `.runtime` | 8 |
| `.pytest_cache` | 5 |
| `docs` | 3 |
| `templates` | 1 |
| `.claude` | 1 |

### 构建入口候选

- 未发现

### 编译数据库检测

- 未发现 `compile_commands.json`；本探测不默认运行 bear/make。

## 工具可用性

| 工具 | 路径/状态 | 版本摘要 |
| --- | --- | --- |
| `rg` | `/home/cp/.nvm/versions/node/v24.14.0/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/codex-path/rg` | ripgrep 15.1.0 (rev af60c2de9d) |
| `fdfind` | `/usr/bin/fdfind` | fd 8.3.1 |
| `fd` | `未安装` |  |
| `ctags` | `/usr/bin/ctags` | Universal Ctags 5.9.0, Copyright (C) 2015 Universal Ctags Team |
| `readtags` | `/usr/bin/readtags` | Find tag file entries matching specified names. |
| `cscope` | `/usr/bin/cscope` | /usr/bin/cscope: version 15.9 |
| `global` | `/usr/bin/global` | global (Global) 6.6.7 |
| `gtags` | `/usr/bin/gtags` | gtags (Global) 6.6.7 |
| `lizard` | `/home/cp/.local/bin/lizard` | 1.22.1 |
| `bear` | `/usr/bin/bear` | bear 3.0.18 |
| `clangd` | `/usr/bin/clangd` | Ubuntu clangd version 14.0.0-1ubuntu1.1 |
| `clang` | `/usr/bin/clang` | Ubuntu clang version 14.0.0-1ubuntu1.1 |

## 入口/初始化候选

- `run.sh:78:tcp_open() {`
- `stock_analyzer/calibrate.py:1820:def main() -> int:`
- `stock_analyzer/calibrate.py:1954:    raise SystemExit(main())`
- `stock_analyzer/daily_job.py:235:    raise SystemExit(main())`
- `stock_analyzer/daily_job.py:29:def main() -> int:`
- `stock_analyzer/market_data.py:119:def main() -> int:`
- `stock_analyzer/market_data.py:400:    raise SystemExit(main())`
- `stock_analyzer/selfcheck.py:117:    raise SystemExit(main())`
- `stock_analyzer/selfcheck.py:93:def main() -> int:`
- `stock_analyzer/services/app_services.py:139:            next_auto_update_window_start_fn=lambda now: next_auto_update_window_start(`
- `stock_analyzer/validation_runtime_support.py:79:def next_auto_update_window_start(now: datetime, start_time: str, until_time: str) -> datetime:`
- `tests/scoring/test_backtest_exit.py:199:    unittest.main()`
- `tests/scoring/test_legacy_remaining.py:1306:    def test_strategy_validation_skips_unbuyable_limit_up_at_next_open(self):`
- `tests/scoring/test_recommendation_strategies.py:687:    unittest.main()`
- `tests/scoring/test_snapshot_jobs.py:178:            result = daily_job.main()`
- `tests/scoring/test_snapshot_jobs.py:209:            result = daily_job.main()`
- `tests/scoring/test_snapshot_jobs.py:252:            result = daily_job.main()`
- `tests/scoring/test_snapshot_jobs.py:61:            result = daily_job.main()`
- `tests/scoring/test_tomorrow_strategy.py:1067:    unittest.main()`
- `tests/scoring/test_validation_backfill.py:222:    unittest.main()`
- `tests/scoring/test_validation_gates.py:217:    unittest.main()`
- `tests/scoring/test_validation_oos.py:194:    unittest.main()`
- `tests/scoring/test_validation_repository_runtime.py:274:    unittest.main()`
- `tests/scoring/test_validation_store.py:207:    unittest.main()`
- `tests/test_calibrate_deepseek.py:109:    unittest.main()`
- `tests/test_deepseek_client.py:851:    unittest.main()`
- `tests/test_deepseek_scheduler.py:98:    unittest.main()`
- `tests/test_frontend_contracts.py:173:    unittest.main()`
- `tests/test_history_cache_resilience.py:86:    unittest.main()`
- `tests/test_performance_contracts.py:121:    unittest.main()`

## 关键结构候选

- `stock_analyzer/app_container.py:109:class ApplicationContainer:`
- `stock_analyzer/app_container.py:16:class AsyncSnapshotWriter:`
- `stock_analyzer/app_container.py:52:class PayloadCache:`
- `stock_analyzer/candidate_pipeline.py:20:class CandidatePipeline:`
- `stock_analyzer/daily_data.py:42:class DailyMarketDataStore:`
- `stock_analyzer/deepseek/batch_rerank_service.py:6:class BatchRerankService:`
- `stock_analyzer/deepseek/budget_policy.py:8:class BudgetPolicy:`
- `stock_analyzer/deepseek/cache.py:10:class DeepSeekCache:`
- `stock_analyzer/deepseek/configuration.py:6:class DeepSeekRuntimeConfig:`
- `stock_analyzer/deepseek/http_client.py:11:class DeepSeekHttpResult:`
- `stock_analyzer/deepseek/http_client.py:24:class DeepSeekHttpClient:`
- `stock_analyzer/deepseek/market_gate_service.py:10:class MarketGateReviewService:`
- `stock_analyzer/deepseek/news_context.py:10:class NewsContextProvider:`
- `stock_analyzer/deepseek/payload_builder.py:9:class PayloadBuilder:`
- `stock_analyzer/deepseek/rerank_service.py:6:class RerankService:`
- `stock_analyzer/deepseek/result_merger.py:7:class ResultMerger:`
- `stock_analyzer/deepseek/usage_accounting.py:10:class UsageAccounting:`
- `stock_analyzer/deepseek/validation_review_service.py:13:class ValidationReviewService:`
- `stock_analyzer/factor_snapshot.py:31:class FactorSnapshotStore:`
- `stock_analyzer/fundamentals.py:119:class TushareFundamentalAdapter(FundamentalProviderAdapter):`
- `stock_analyzer/fundamentals.py:151:class AKShareFundamentalAdapter(FundamentalProviderAdapter):`
- `stock_analyzer/fundamentals.py:183:class LegacyProviderFundamentalAdapter(FundamentalProviderAdapter):`
- `stock_analyzer/fundamentals.py:203:class HistoryProviderAdapter:`
- `stock_analyzer/fundamentals.py:216:class HistoryCacheAdapter(HistoryProviderAdapter):`
- `stock_analyzer/fundamentals.py:231:class LocalHistoryAdapter(HistoryProviderAdapter):`
- `stock_analyzer/fundamentals.py:246:class ProviderHistoryAdapter(HistoryProviderAdapter):`
- `stock_analyzer/fundamentals.py:27:class FundamentalFetchResult:`
- `stock_analyzer/fundamentals.py:45:class HistoryLookupResult:`
- `stock_analyzer/fundamentals.py:73:class FundamentalProviderAdapter:`
- `stock_analyzer/fundamentals.py:91:class EastmoneyFundamentalAdapter(FundamentalProviderAdapter):`

## 算法/机制候选

- `pytest.ini:5:    slow: long-running scoring or runtime tests`
- `README.md:15:- 历史因子默认启用；缓存缺失时后台分批预热，不阻塞推荐接口。盘后使用 `./run.sh after-close` 或 `.\run.ps1 after-close` 更新完整日线、快照和 IC`
- `run.ps1:109:    if (-not [string]::IsNullOrWhiteSpace($configured)) {`
- `run.ps1:118:        [string]$ProxyHostName,`
- `run.ps1:119:        [string]$ProxyPortValue`
- `run.ps1:128:    if ([string]::IsNullOrWhiteSpace((Get-EnvValue "NO_PROXY" ""))) {`
- `run.ps1:131:    if ([string]::IsNullOrWhiteSpace((Get-EnvValue "no_proxy" ""))) {`
- `run.ps1:138:        if (-not [string]::IsNullOrWhiteSpace((Get-EnvValue $name ""))) {`
- `run.ps1:148:        if (-not [string]::IsNullOrWhiteSpace($value)) {`
- `run.ps1:174:        if (-not [string]::IsNullOrWhiteSpace($value)) {`
- `run.ps1:206:                if ([string]::IsNullOrWhiteSpace((Get-EnvValue "PIP_PROXY" ""))) {`
- `run.ps1:228:        [string]$File,`
- `run.ps1:229:        [string[]]$PrefixArgs = @()`
- `run.ps1:241:        [string[]]$Arguments`
- `run.ps1:256:    return ([string]$line).Trim()`
- `run.ps1:31:        [string]$Name,`
- `run.ps1:32:        [string]$DefaultValue = ""`
- `run.ps1:36:    if ([string]::IsNullOrWhiteSpace($value)) {`
- `run.ps1:43:    param([string]$PathValue)`
- `run.ps1:66:        [string]$TargetHost,`
- `run.ps1:91:    if (-not [string]::IsNullOrWhiteSpace($configured)) {`
- `run.ps1:99:        if (-not [string]::IsNullOrWhiteSpace($candidate) -and -not $seen.ContainsKey($candidate)) {`
- `run.sh:265:        "请先安装兼容的 Python 后重试；如果已有旧虚拟环境，请删除后重建。"`
- `run.sh:288:with urllib.request.urlopen(request, timeout=8) as response:`
- `run.sh:339:    --default-timeout "$PIP_TIMEOUT"`
- `run.sh:359:  printf '首次安装失败，重试一次：关闭构建隔离后再次安装（适配 Python 3.14）。\n'`
- `run.sh:82:  timeout 2 bash -c "</dev/tcp/$host/$port" >/dev/null 2>&1`
- `static/app.js:105:  const next = JSON.stringify(value ?? null);`
- `static/app.js:106:  if (state.renderFingerprints[key] === next) {`
- `static/app.js:109:  state.renderFingerprints[key] = next;`

## 数据流转候选

- `pytest.ini:5:    slow: long-running scoring or runtime tests`
- `README.md:11:- 个股预测：输入股票代码，返回本地预测和 DeepSeek 优化建议`
- `run.ps1:109:    if (-not [string]::IsNullOrWhiteSpace($configured)) {`
- `run.ps1:118:        [string]$ProxyHostName,`
- `run.ps1:119:        [string]$ProxyPortValue`
- `run.ps1:128:    if ([string]::IsNullOrWhiteSpace((Get-EnvValue "NO_PROXY" ""))) {`
- `run.ps1:131:    if ([string]::IsNullOrWhiteSpace((Get-EnvValue "no_proxy" ""))) {`
- `run.ps1:138:        if (-not [string]::IsNullOrWhiteSpace((Get-EnvValue $name ""))) {`
- `run.ps1:148:        if (-not [string]::IsNullOrWhiteSpace($value)) {`
- `run.ps1:174:        if (-not [string]::IsNullOrWhiteSpace($value)) {`
- `run.ps1:206:                if ([string]::IsNullOrWhiteSpace((Get-EnvValue "PIP_PROXY" ""))) {`
- `run.ps1:228:        [string]$File,`
- `run.ps1:229:        [string[]]$PrefixArgs = @()`
- `run.ps1:241:        [string[]]$Arguments`
- `run.ps1:256:    return ([string]$line).Trim()`
- `run.ps1:31:        [string]$Name,`
- `run.ps1:32:        [string]$DefaultValue = ""`
- `run.ps1:36:    if ([string]::IsNullOrWhiteSpace($value)) {`
- `run.ps1:43:    param([string]$PathValue)`
- `run.ps1:66:        [string]$TargetHost,`
- `run.ps1:91:    if (-not [string]::IsNullOrWhiteSpace($configured)) {`
- `run.ps1:99:        if (-not [string]::IsNullOrWhiteSpace($candidate) -and -not $seen.ContainsKey($candidate)) {`
- `run.sh:143:  while IFS= read -r host; do`
- `run.sh:144:    while IFS= read -r port; do`
- `static/app.js:105:  const next = JSON.stringify(value ?? null);`
- `static/app.js:160:  return String(value ?? "")`
- `static/app.js:22:  selectedValidation: {`
- `static/app.js:235:      context.recommendations.selectRecommendationPool(button.dataset.poolFilter || "today");`
- `static/app.js:244:      context.validation.selectValidationStrategy(button.dataset.validationStrategy || "short_term");`
- `static/recommendation-app.js:135:          top_n: String(window.APP_CONFIG.defaultTopN || 18),`

## 资源/并发候选

- `README.md:113:.\run.ps1 after-close`
- `README.md:114:.\run.ps1 after-close --strategy all`
- `README.md:115:.\run.ps1 after-close --market-data-limit 500`
- `README.md:15:- 历史因子默认启用；缓存缺失时后台分批预热，不阻塞推荐接口。盘后使用 `./run.sh after-close` 或 `.\run.ps1 after-close` 更新完整日线、快照和 IC`
- `run.ps1:11:  .\run.ps1 after-close [daily_job 参数]`
- `run.ps1:22:  .\run.ps1 after-close --strategy all`
- `run.ps1:23:  .\run.ps1 after-close --market-data-limit 500`
- `run.ps1:362:with urllib.request.urlopen(request, timeout=8) as response:`
- `run.ps1:431:        "after-close" {}`
- `run.ps1:465:    if (($RunMode -eq "after-close" -or $RunMode -eq "daily-job") -and $venvAlreadyReady -and (Get-EnvValue "AFTER_CLOSE_INSTALL_DEPS" "0") -ne "1") {`
- `run.ps1:477:    if ($RunMode -eq "after-close" -or $RunMode -eq "daily-job") {`
- `run.ps1:485:        $dailyJobArgs = @("-m", "stock_analyzer.daily_job", "--after-close") + @($RunArgs)`
- `run.sh:145:      if tcp_open "$host" "$port"; then`
- `run.sh:187:      if ! tcp_open "$host" "$port"; then`
- `run.sh:270:check_internet_connectivity() {`
- `run.sh:288:with urllib.request.urlopen(request, timeout=8) as response:`
- `run.sh:32:  ./run.sh after-close [daily_job 参数]`
- `run.sh:378:if { [ "$RUN_MODE" = "after-close" ] || [ "$RUN_MODE" = "daily-job" ]; } \`
- `run.sh:383:  check_internet_connectivity "$VENV_DIR/bin/python"`
- `run.sh:392:if [ "$RUN_MODE" = "after-close" ] || [ "$RUN_MODE" = "daily-job" ]; then`
- `run.sh:396:  exec "$VENV_DIR/bin/python" -m stock_analyzer.daily_job --after-close "$@"`
- `run.sh:42:  ./run.sh after-close --strategy all                 # 收盘后下载日线、快照、回填、刷新因子`
- `run.sh:43:  ./run.sh after-close --market-data-limit 500         # 分批下载，适合普通办公 PC`
- `run.sh:61:  after-close|daily-job)`
- `run.sh:78:tcp_open() {`
- `static/app.js:222:function bindEvents() {`
- `static/app.js:254:  bindEvents();`
- `static/app.js:2:  timer: null,`
- `static/recommendation-app.js:100:      function connectRecommendationStream() {`
- `static/recommendation-app.js:105:        state.timer = setInterval(() => {`

## 媒体链路候选

- `static/recommendation-app.js:241:        const displayRows = RecommendationUtils.filterAndSortRows(rows, {`
- `static/recommendation-app.js:245:        if (!displayRows.length) {`
- `static/recommendation-app.js:249:        els.shortTermBody.innerHTML = RecommendationTables.renderShortTermTableRows(displayRows, {`
- `static/recommendation-app.js:265:        const displayRows = RecommendationUtils.filterAndSortRows(rows, {`
- `static/recommendation-app.js:269:        if (!displayRows.length) {`
- `static/recommendation-app.js:273:        els.tomorrowBody.innerHTML = RecommendationTables.renderTomorrowTableRows(displayRows, {`
- `static/recommendation-app.js:289:        const displayRows = RecommendationUtils.filterAndSortRows(rows, {`
- `static/recommendation-app.js:293:        if (!displayRows.length) {`
- `static/recommendation-app.js:297:        els.swingBody.innerHTML = RecommendationTables.renderSwingTableRows(displayRows, {`
- `static/recommendation-status.js:140:        Object.values(state.charts).forEach((chart) => chart && !chart.isDisposed?.() && chart.resize());`
- `static/recommendation-status.js:145:          if (chart && !chart.isDisposed?.() && chart.getDom?.()?.offsetParent !== null) {`
- `static/recommendation-status.js:28:        if (!chart || chart.isDisposed?.()) {`
- `static/styles.css:112:  display: flex;`
- `static/styles.css:120:  display: inline-flex;`
- `static/styles.css:165:  display: grid;`
- `static/styles.css:173:  display: flex;`
- `static/styles.css:215:  display: block;`
- `static/styles.css:222:  display: block;`
- `static/styles.css:238:  display: flex;`
- `static/styles.css:252:  display: grid;`
- `static/styles.css:291:  display: grid;`
- `static/styles.css:306:  display: none;`
- `static/styles.css:310:  display: block;`
- `static/styles.css:314:  display: grid;`
- `static/styles.css:327:  display: grid;`
- `static/styles.css:337:  display: grid;`
- `static/styles.css:355:  display: block;`
- `static/styles.css:369:  display: grid;`
- `static/styles.css:66:  display: block;`
- `static/styles.css:71:  display: none;`

## 海思/板级 SDK 候选

- `stock_analyzer/scoring_core/base.py:119:    {"segment": "工业控制/PLC", "keywords": ("DCS", "PLC", "伺服驱动", "运动控制", "工业控制", "工控")},`
- `stock_analyzer/scoring_core/base.py:94:    "PLC", "伺服驱动", "运动控制", "轴承", "导轨", "滚珠丝杠", "液压",`
- `docs/plan.md:1131:| C | **催化剂/事件 alpha 独立化** | 事件驱动信号独立成源, 与量价并行 | 事件因果链比量价更清晰、被套利更少 | 中 | 🥈 高 |`
- `docs/plan.md:1219:**核心思想**: 把"可验证催化剂"(业绩预告、政策受益、大额订单、重组、回购) 独立成一个事件驱动策略, 与量价策略并行产出候选, 再由 6.2.4 集成层合并。`
- `docs/plan.md:7:本文档的目标是把现有"0-100 综合排序分"逐步升级为"扣成本后的期望收益 / 下行风险 / 置信度"共同驱动的工程闭环。所有改进只追求提升风险调整后收益与验证可信度, 不承诺也不暗示保证盈利。`

## Ctags 符号摘要（轻量）

该摘要来自 Universal Ctags JSON 输出，用于快速定位真实符号；仍需按源码窗口验证后再写入正式文档。

### 函数/方法


### 类/结构/枚举


### 宏/常量


### 全局变量/成员


### 类型/命名空间


### 其它符号

- `no source files for ctags scan`

## 复杂度候选（lizard）

- `no source files for lizard scan`

## 源码准确性下一轮精读入口

- 未发现

## 使用约束

- 先围绕“源码准确性下一轮精读入口”读取小片段，再决定核心路径。
- Ctags/lizard 结果是候选证据，不是结论；优先验证 owner、生命周期、调用上下文、失败路径和锁/等待边界。
- 默认不运行 bear/make；只有用户明确要求构建探测时才生成或更新 `compile_commands.json`。
- 不要把本索引当成结论；只把源码验证后的框架、结构、算法和数据流写入正式文档。
- 如果候选过多，优先选择入口函数、跨模块数据流、owner 明确的结构体、复杂度高且处于主链路的函数。
