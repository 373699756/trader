"use strict";

const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const dashboardPath = process.argv[2];
const path = require("path");
const selectionPath = path.join(path.dirname(dashboardPath), "selection.js");
const renderPath = path.join(path.dirname(dashboardPath), "render.js");
const longGroupsPath = path.join(path.dirname(dashboardPath), "long_groups.js");
const formattersPath = path.join(path.dirname(dashboardPath), "dashboard_formatters.js");
const patchesPath = path.join(path.dirname(dashboardPath), "dashboard_patches.js");
const statusViewPath = path.join(path.dirname(dashboardPath), "status_view.js");
const releaseContractPath = path.join(path.dirname(dashboardPath), "release_contract.js");
const streamPath = path.join(path.dirname(dashboardPath), "dashboard_stream.js");
const streamSource = fs.readFileSync(streamPath, "utf8");
let source = fs.readFileSync(dashboardPath, "utf8");
const suffix = "\n})();";
source = source.trimEnd();
assert(source.endsWith(suffix), "dashboard.js must retain its IIFE boundary");

const sandbox = {
  URLSearchParams,
  console,
  document: {
    addEventListener() {},
    createElement() { return {}; },
    querySelector(selector) {
      assert.strictEqual(selector, 'meta[name="trader-web-snapshot-retention-ms"]');
      return { content: "35000" };
    },
  },
  window: {
    addEventListener() {},
    TraderLongWatchlistData: {
      watchlist_version: "test-watchlist",
      items: [{ code: "600001", name: "配置名称", industry: "配置行业" }],
      groups: [{ name: "半导体设备", category: "chokepoint", codes: ["600001"] }],
    },
  },
};
vm.runInNewContext(fs.readFileSync(renderPath, "utf8"), sandbox, { filename: renderPath });
vm.runInNewContext(fs.readFileSync(selectionPath, "utf8"), sandbox, { filename: selectionPath });
vm.runInNewContext(fs.readFileSync(longGroupsPath, "utf8"), sandbox, { filename: longGroupsPath });
vm.runInNewContext(fs.readFileSync(formattersPath, "utf8"), sandbox, { filename: formattersPath });
vm.runInNewContext(fs.readFileSync(patchesPath, "utf8"), sandbox, { filename: patchesPath });
vm.runInNewContext(fs.readFileSync(statusViewPath, "utf8"), sandbox, { filename: statusViewPath });
vm.runInNewContext(fs.readFileSync(releaseContractPath, "utf8"), sandbox, { filename: releaseContractPath });
vm.runInNewContext(streamSource, sandbox, { filename: streamPath });
vm.runInNewContext(source, sandbox, { filename: dashboardPath });
const missingPatchSandbox = {
  URLSearchParams,
  console,
  document: { addEventListener() {}, createElement() { return {}; } },
  window: { addEventListener() {} },
};
vm.runInNewContext(fs.readFileSync(formattersPath, "utf8"), missingPatchSandbox, { filename: formattersPath });
vm.runInNewContext(source, missingPatchSandbox, { filename: dashboardPath });
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(missingPatchSandbox.window.TraderDashboardDiagnostics.snapshot().browserErrors)),
  [
    "dependency_missing:TraderDashboardPatches",
    "dependency_missing:TraderReleaseContract",
    "dependency_missing:TraderDashboardStream",
  ],
);
const state = {
  ...sandbox.window.TraderSelection,
  ...sandbox.window.TraderDashboardPatches,
  latencySummary: sandbox.window.TraderDashboardFormatters.latencySummary,
  drawer: sandbox.window.TraderRender.drawer,
  frozenTodayTable: sandbox.window.TraderRender.frozenTodayTable,
  isFrozenTodayView: sandbox.window.TraderRender.isFrozenTodayView,
  longTable: sandbox.window.TraderRender.longTable,
  sourceLabel: sandbox.window.TraderRender.sourceLabel,
  formatDurationHms: sandbox.window.TraderStatusView.formatDurationHms,
  healthView: sandbox.window.TraderStatusView.healthView,
  quoteCoverageSummary: sandbox.window.TraderStatusView.quoteCoverageSummary,
  renderQuoteCoverage: sandbox.window.TraderStatusView.renderQuoteCoverage,
  renderSummary: sandbox.window.TraderStatusView.renderSummary,
  runtimeErrorRows: sandbox.window.TraderStatusView.runtimeErrorRows,
  updateQuoteAge: sandbox.window.TraderStatusView.updateQuoteAge,
  decisionPayloadCompatibility: sandbox.window.TraderReleaseContract.decisionPayloadCompatibility,
  statusPayloadCompatibility: sandbox.window.TraderReleaseContract.statusPayloadCompatibility,
  tableColumnCount: sandbox.window.TraderRender.tableColumnCount,
  tableRows: sandbox.window.TraderRender.tableRows,
  longGroupAveragePct: sandbox.window.TraderLongGroups.groupAveragePct,
  longGroupDisplayPayload: sandbox.window.TraderLongGroups.displayPayload,
  longGroupStaticFallbackPayload: sandbox.window.TraderLongGroups.staticFallbackPayload,
  longGroupNormalized: sandbox.window.TraderLongGroups.normalized,
  longGroupRenderBar: sandbox.window.TraderLongGroups.renderBar,
  longGroupVisibleRecommendations: sandbox.window.TraderLongGroups.visibleRecommendations,
};
assert(state, "dashboard state helpers were not exported into the test sandbox");
assert.strictEqual(
  sandbox.window.TraderDashboardDiagnostics.snapshot().webSnapshotRetentionMs,
  35000,
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.statusPayloadCompatibility({ schema_version: "v2_status_v1" }))),
  { compatible: false, reason: "status_schema_mismatch" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.statusPayloadCompatibility({
    schema_version: "v2_status_v3",
    release: {
      decision_view_schema: "v2_decision_view_v2",
      web_asset_revision: "release-contract-2026-08-26-v3",
    },
  }))),
  { compatible: true, reason: "" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.decisionPayloadCompatibility({ schema_version: "v2_decision_view_v1" }))),
  { compatible: false, reason: "decision_schema_mismatch" },
);
assert(source.includes("draft: null,"), "formal recommendation patches must clear observation drafts");
assert(
  source.includes("loadRecommendations, loadStatus, applyRecommendationPatch, applyOverlayPatch"),
  "dashboard must inject the direct recommendation patch handler into the SSE controller",
);
assert(
  streamSource.includes("applyRecommendationPatch(payload)"),
  "decision SSE must render its replacement patch without an unconditional snapshot GET",
);
assert.strictEqual(state.formatDurationHms(0), "0s");
assert.strictEqual(state.formatDurationHms(-1), "0s");
assert.strictEqual(state.formatDurationHms(Number.NaN), "0s");
assert.strictEqual(state.formatDurationHms(59), "59s");
assert.strictEqual(state.formatDurationHms(60), "1m 0s");
assert.strictEqual(state.formatDurationHms(3599), "59m 59s");
assert.strictEqual(state.formatDurationHms(3600), "1h 0m 0s");
assert.strictEqual(state.formatDurationHms(95580), "26h 33m 0s");
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.quoteCoverageSummary([
    {
      code: "600001",
      name: "完整股票",
      industry: "银行",
      price: 10.5,
      pct_change: 0,
      source: "tencent",
      source_time: "2026-08-14T10:00:00+08:00",
      quote_status: "live",
    },
    {
      code: "600002",
      name: "",
      industry: "银行",
      price: 11.5,
      pct_change: 1.2,
      source: "tencent",
      source_time: "2026-08-14T10:00:01+08:00",
      quote_status: "retained",
    },
    {
      code: "600003",
      name: "缺行情股票",
      industry: "银行",
      price: null,
      pct_change: null,
      source: "long_watchlist",
      source_time: null,
      quote_status: "missing",
    },
  ]))),
  { total: 3, available: 2, quoteMissing: 1, identityMissing: 1 },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.quoteCoverageSummary([]))),
  { total: 0, available: 0, quoteMissing: 0, identityMissing: 0 },
);
const summaryElements = {
  quoteCoverageStatus: { textContent: "" },
  quoteCoverageMeta: { textContent: "" },
  funnelStatus: { textContent: "" },
  funnelMeta: { textContent: "" },
  quoteSource: { textContent: "" },
  budgetStatus: { textContent: "" },
  budgetMeta: { textContent: "" },
  headerFreeze: { textContent: "" },
  freezeMeta: { textContent: "" },
  snapshotStrategy: { textContent: "" },
  snapshotDate: { textContent: "" },
};
state.renderSummary(
  summaryElements,
  {
    status: "ready",
    strategy: "today",
    trade_date: "2026-08-14",
    frozen: false,
    score_status: "scored",
    coverage: { candidate_count: 120, evaluated_count: 80, rejected_count: 40 },
  },
  [
    {
      code: "600001", name: "正式股票", industry: "银行", action: "executable",
      price: 10, pct_change: 1, source: "tencent", source_time: "2026-08-14T10:00:00+08:00",
      scores: { final_score: 82 },
    },
    {
      code: "600002", name: "观察股票", industry: "证券", action: "observe",
      price: null, pct_change: null, source: "decision", source_time: "2026-08-14T09:59:00+08:00",
      scores: { final_score: 75 },
    },
  ],
  "open",
  { source: "tencent" },
  sandbox.window.TraderSelection,
  sandbox.window.TraderRender,
  { deepseek_budget: { limit: 168, used: 2, remaining: 166 } },
);
assert.strictEqual(summaryElements.quoteCoverageStatus.textContent, "1 / 2");
assert.strictEqual(summaryElements.quoteCoverageMeta.textContent, "行情缺失 1 · 身份缺失 0");
assert.strictEqual(summaryElements.funnelStatus.textContent, "120 → 80 → 1");
assert.strictEqual(summaryElements.funnelMeta.textContent, "过滤 40 · 观察 1 · 最高 82.00");
assert.strictEqual(summaryElements.snapshotDate.textContent, "2026-08-14");
state.renderSummary(
  summaryElements,
  {
    status: "not_ready",
    strategy: "today",
    trade_date: "2026-08-14",
    frozen: false,
    score_status: "scored",
    coverage: { candidate_count: 0, evaluated_count: 0, rejected_count: 0 },
    items: [],
  },
  [],
  "open",
  { source: "fixture" },
  sandbox.window.TraderSelection,
  sandbox.window.TraderRender,
  {
    deepseek_budget: { used: 0, remaining: 168, planned_limit: 71 },
    market_data: {
      security_master: {
        provider: "free_market+production_calendar",
        tushare_required: false,
      },
    },
    scheduler: {
      input_quality: {
        today: {
          status: "not_ready",
          candidate_optional_reason_counts: {
            missing_listing_date: 221,
            missing_listing_age_sessions: 65,
            board_identity_degraded: 221,
          },
          supply_funnel: {
            requested_candidates: 360,
            full_scored: 65,
            filter_reject: 216,
            selected_executable: 2,
            selected_observe: 2,
          },
          summary: {
            trade_date: "2026-08-14",
            quote_total_count: 360,
            quote_covered_count: 352,
            quote_missing_count: 8,
            security_identity_missing_count: 286,
            latest_quote_source: "tencent",
            latest_quote_source_time: "2026-08-14T10:00:00+08:00",
            highest_final_score: 74.25,
          },
        },
      },
    },
  },
);
assert.strictEqual(summaryElements.quoteCoverageStatus.textContent, "352 / 360");
assert.strictEqual(
  summaryElements.quoteCoverageMeta.textContent,
  "行情缺失 8 · 身份缺失 286（上市日期 221 · 交易日龄 65；免费行情+交易日历补齐中）",
);
assert.strictEqual(summaryElements.funnelStatus.textContent, "360 → 65 → 2");
assert.strictEqual(summaryElements.funnelMeta.textContent, "过滤 216 · 观察草稿 2 · 最高 74.25");
assert.strictEqual(summaryElements.quoteSource.textContent, "腾讯行情");
assert.strictEqual(summaryElements.budgetStatus.textContent, "0 / 168");
assert.strictEqual(summaryElements.budgetMeta.textContent, "已用 / 剩余 · 上限 168 · 复核 0/0");
state.renderSummary(
  summaryElements,
  {
    status: "not_ready",
    strategy: "tomorrow",
    trade_date: "2026-08-14",
    frozen: false,
    score_status: "scored",
    coverage: {},
    items: [],
    draft: null,
  },
  [],
  "warming",
  null,
  sandbox.window.TraderSelection,
  sandbox.window.TraderRender,
  {
    deepseek_budget: { used: 0, remaining: 168, limit: 168 },
    market_data: {
      active_source: "eastmoney",
      candidate_quote_latest_source: "tencent",
      candidate_quote_cache_entries: 360,
      candidate_quote_age: {
        sample_count: 360,
        latest_source_time: "2026-08-14T10:01:00+08:00",
      },
    },
    scheduler: { input_quality: {}, lanes: [{ strategy: "tomorrow", running: true, pending: true }] },
  },
);
assert.strictEqual(summaryElements.quoteCoverageStatus.textContent, "360 / 360");
assert.strictEqual(summaryElements.quoteCoverageMeta.textContent, "行情缺失 0 · 身份缺失 待评分");
assert.strictEqual(summaryElements.funnelStatus.textContent, "360 → 采集中 → 0");
assert.strictEqual(summaryElements.funnelMeta.textContent, "过滤 待计算 · 观察草稿 正在生成 · 最高 —");
assert.strictEqual(summaryElements.quoteSource.textContent, "腾讯行情");
assert.strictEqual(summaryElements.headerFreeze.textContent, "采集中");
assert.strictEqual(summaryElements.freezeMeta.textContent, "首次评分正在运行");
const notReadyAgeElements = {
  quoteAge: { textContent: "" },
  quoteTime: { textContent: "" },
  quoteSource: summaryElements.quoteSource,
  snapshotMeta: { textContent: "" },
};
state.updateQuoteAge(
  notReadyAgeElements,
  {
    status: "not_ready",
    strategy: "today",
    trade_date: "2026-08-14",
    items: [],
    published_at: null,
  },
  sandbox.window.TraderRender,
  {
    market_data: {
      candidate_quote_age: {
        latest_source_time: new Date(Date.now() - 65_000).toISOString(),
      },
    },
  },
);
assert.strictEqual(notReadyAgeElements.quoteAge.textContent, "1m 5s");
assert.notStrictEqual(notReadyAgeElements.quoteTime.textContent, "-");
state.renderSummary(
  summaryElements,
  {
    status: "ready",
    strategy: "long",
    trade_date: "2026-08-14",
    frozen: false,
    score_status: "not_applicable",
    coverage: { candidate_count: 1, evaluated_count: 1, rejected_count: 0 },
  },
  [{
    code: "600001", name: "长期股票", industry: "行业", action: "observe",
    price: 10, pct_change: 0, source: "tencent", source_time: "2026-08-14T10:00:00+08:00",
  }],
  "closed",
  { source: "tencent" },
  sandbox.window.TraderSelection,
  sandbox.window.TraderRender,
  { deepseek_budget: { limit: 168, used: 2, remaining: 166 } },
);
assert.strictEqual(summaryElements.funnelStatus.textContent, "不适用");
assert.strictEqual(summaryElements.funnelMeta.textContent, "长期固定观察池不评分、不产生推荐");
state.renderQuoteCoverage(summaryElements, [{
  code: "600001", name: "长期股票", industry: "行业",
  price: null, pct_change: null, source: "long_watchlist", source_time: null,
  quote_status: "missing",
}]);
assert.strictEqual(summaryElements.quoteCoverageStatus.textContent, "0 / 1");
assert.strictEqual(summaryElements.quoteCoverageMeta.textContent, "行情缺失 1 · 身份缺失 0");
assert.strictEqual(
  state.initialStrategy({
    strategies: {
      today: { status: "not_ready", coverage: { selected_count: 0 } },
      tomorrow: { status: "ready", coverage: { selected_count: 0 } },
      d25: { status: "ready", coverage: { selected_count: 0 } },
      long: { status: "ready", coverage: { selected_count: 224 } },
    },
  }),
  "long",
);
assert.strictEqual(
  state.initialStrategy({
    strategies: {
      today: { status: "ready", coverage: { selected_count: 3 } },
      tomorrow: { status: "ready", coverage: { selected_count: 6 } },
      long: { status: "ready", coverage: { selected_count: 224 } },
    },
  }),
  "today",
);
assert.strictEqual(state.initialStrategy({ strategies: {} }), "today");
const degradedHealth = state.healthView({
  health: { level: "degraded", issue_count: 2 },
  recent_errors: [
    {
      code: "refresh:source_unavailable",
      severity: "degraded",
      strategy: "tomorrow",
      stage: "refresh",
      last_occurred_at: "2026-08-14T10:15:42+08:00",
      count: 2,
      recovery_status: "active",
    },
  ],
}, ["corporate_risk_history_unavailable"]);
assert.strictEqual(degradedHealth.level, "degraded");
assert.strictEqual(degradedHealth.issueCount, 2);
assert.strictEqual(degradedHealth.badge, "降级 · 2项");
assert.strictEqual(degradedHealth.primary.message, "行情刷新暂时不可用");
assert.strictEqual(degradedHealth.primary.meta.includes("明日"), true);
assert.strictEqual(degradedHealth.primary.meta.includes("数据刷新"), true);
assert.strictEqual(degradedHealth.issues.length, 2);
const runtimeRows = state.runtimeErrorRows(degradedHealth.issues);
assert.strictEqual(runtimeRows.includes("refresh:source_unavailable"), true);
assert.strictEqual(runtimeRows.includes("公司风险历史暂不可核验"), true);
assert.strictEqual(runtimeRows.includes("活动中"), true);
const normalHealth = state.healthView({ health: { level: "normal", issue_count: 0 }, recent_errors: [] }, []);
assert.strictEqual(normalHealth.badge, "正常 · 无最近错误");
assert.strictEqual(normalHealth.primary, null);
assert.strictEqual(state.currentViewMatches("long", "current"), true);
assert.strictEqual(state.currentViewMatches("today", "current"), true);
assert.strictEqual(state.currentViewMatches("tomorrow", "current"), true);
assert.strictEqual(state.currentViewMatches("d25", "current"), true);
assert.strictEqual(state.currentViewMatches("tomorrow", "live"), true);
assert.strictEqual(state.currentViewMatches("today", "official"), true);
const longStaticFallback = state.longGroupStaticFallbackPayload("long_api_unavailable");
assert.strictEqual(longStaticFallback.status, "ready");
assert.strictEqual(longStaticFallback.strategy, "long");
assert.strictEqual(longStaticFallback.score_status, "not_applicable");
assert.strictEqual(longStaticFallback.items.length, 1);
assert.strictEqual(longStaticFallback.items[0].code, "600001");
assert.strictEqual(longStaticFallback.items[0].price, null);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(longStaticFallback.degraded_reasons)),
  ["long_api_unavailable"],
);
const liveShort = {
  status: "ready",
  strategy: "tomorrow",
  historical: false,
  frozen: false,
  items: [
    { code: "600001", action: "executable" },
    { code: "600002", action: "observe" },
  ],
};
assert.strictEqual(state.observationDisplayState(liveShort, "warmup"), "closed_market");
assert.strictEqual(state.observationDisplayState(liveShort, "today_main"), "open");
assert.strictEqual(state.observationDisplayState(liveShort, "midday"), "open");
assert.strictEqual(state.observationDisplayState(liveShort, "final_review"), "open");
assert.strictEqual(state.observationDisplayState(liveShort, "after_close"), "closed_market");
assert.strictEqual(state.observationDisplayState({ ...liveShort, strategy: "today" }, "today_late"), "open");
assert.strictEqual(state.observationDisplayState({ ...liveShort, strategy: "today" }, "midday"), "closed_market");
assert.strictEqual(state.observationDisplayState({ ...liveShort, strategy: "today" }, "afternoon"), "closed_market");
assert.strictEqual(state.observationDisplayState({ ...liveShort, frozen: true }, "today_main"), "closed_frozen");
assert.strictEqual(state.observationDisplayState({ ...liveShort, historical: true }, "midday"), "hidden_history");
assert.strictEqual(
  state.observationDisplayState({ ...liveShort, status: "not_ready", historical: true }, "midday"),
  "hidden_history",
);
assert.strictEqual(state.observationDisplayState(liveShort, ""), "unknown");
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.observationRecommendations(liveShort, "midday"))),
  [{ code: "600002", action: "observe" }],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.observationRecommendations(liveShort, "after_close"))),
  [],
);
const notReadyDraft = {
  status: "not_ready",
  strategy: "tomorrow",
  historical: false,
  frozen: false,
  items: [],
  draft: { items: [{ code: "600003", action: "observe" }] },
};
assert.strictEqual(state.observationDisplayState(notReadyDraft, "midday"), "open");
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.observationRecommendations(notReadyDraft, "midday"))),
  [{ code: "600003", action: "observe" }],
);
const emptyObservationDraft = { ...notReadyDraft, draft: { items: [] } };
assert.strictEqual(state.observationDisplayState(emptyObservationDraft, "midday"), "empty");
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.observationRecommendations(emptyObservationDraft, "midday"))),
  [],
);
assert.strictEqual(state.observationEmptyMessage("empty"), "本轮无股票达到观察条件");
assert.strictEqual(state.observationEmptyMessage("warming"), "正在生成观察草稿");
assert.strictEqual(state.observationEmptyMessage("unavailable"), "本轮尚无可用观察草稿，请查看运行状态");
assert.strictEqual(
  state.observationDisplayState(
    { ...notReadyDraft, draft: null },
    "midday",
    { scheduler: { lanes: [{ name: "trader-v2-tomorrow", running: true, pending: false }] } },
  ),
  "warming",
);
assert.strictEqual(
  state.observationDisplayState(
    { ...notReadyDraft, draft: null },
    "midday",
    { scheduler: { lanes: [{ name: "trader-v2-tomorrow", running: false, pending: false, completed_count: 58 }] } },
  ),
  "unavailable",
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage({
    strategy: "tomorrow",
    readiness_reason: "snapshot_not_published",
  }))),
  { message: "明日策略当前快照尚未发布", notice: "当前策略快照尚未形成，等待本地评分发布" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage({
    strategy: "d25",
    readiness_reason: "afternoon_freeze_pending",
  }))),
  { message: "14:50 正式快照尚未形成", notice: "冻结流程尚未完成；不会展示上一交易日结果" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage({
    strategy: "d25",
    readiness_reason: "afternoon_close_recovery_pending",
  }))),
  { message: "14:50 正式快照缺失", notice: "正在等待允许的收盘恢复；不会展示上一交易日结果" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage({
    strategy: "long",
    readiness_reason: "long_snapshot_not_ready",
  }))),
  { message: "长期策略当前尚无可用数据", notice: "长期策略只展示当前研究快照" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage({
    strategy: "today",
    readiness_reason: "today_freeze_missed",
  }))),
  { message: "11:20 前未形成正式快照", notice: "按冻结规则今日不补算，当前无推荐" },
);
assert.strictEqual(
  state.emptyRecommendationMessage({
    selection_diagnostics: {
      empty_reason: "score_below_observation_floor",
      maximum_final_score: 64.5,
      observation_floor: 65,
      executable_threshold: 70,
    },
  }),
  "最高评分 64.50，低于观察门槛 65.00（正式门槛 70.00），本轮无正式推荐和观察项",
);
assert.strictEqual(
  state.emptyRecommendationMessage({
    selection_diagnostics: {
      empty_reason: "risk_or_execution_blocked",
      blocked_reason_counts: { "corporate_risk_history_unavailable": 1 },
    },
  }),
  "达到观察门槛的候选均不可执行：公司风险历史暂不可核验（1只）",
);
assert.strictEqual(
  state.emptyRecommendationMessage({
    selection_diagnostics: { empty_reason: "risk_or_execution_blocked" },
  }, 2),
  "本轮无正式推荐；2只进入观察池，具体原因见下表",
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.latencySummary([10, 20, 30]))),
  { sample_count: 3, p50_ms: 20, p95_ms: 30, maximum_ms: 30 },
);
assert.strictEqual(
  state.longTable().head,
  "<tr><th>排名</th><th>股票</th><th>最新价</th><th>今日涨跌</th><th>成交 / 换手</th><th>总市值</th><th>行情来源 / 时间</th></tr>",
);
assert.strictEqual(state.tableColumnCount({ strategy: "long", historical: false }), 7);
const frozenToday = {
  strategy: "today",
  trade_date: "2026-07-23",
  current_trade_date: "2026-07-23",
  historical: false,
  frozen: true,
  degraded_reasons: [],
  fusion_mode: "local_degraded",
};
assert.strictEqual(state.isFrozenTodayView(frozenToday), true);
assert.strictEqual(state.isFrozenTodayView({ ...frozenToday, historical: true }), false);
assert.strictEqual(state.isFrozenTodayView({ ...frozenToday, strategy: "tomorrow" }), false);
assert.strictEqual(state.isFrozenTodayView({ ...frozenToday, phase: "close_fallback" }), false);
assert.strictEqual(state.isFrozenTodayView({ ...frozenToday, current_trade_date: "2026-07-24" }), false);
assert.strictEqual(state.tableColumnCount(frozenToday), 7);
assert.strictEqual(
  state.frozenTodayTable().head,
  "<tr><th>排名</th><th>股票</th><th>11:20锚点价</th><th>锚点时涨跌</th><th>当前价</th><th>当前涨跌</th><th>锚点至今</th></tr>",
);
const frozenTodayItem = {
  rank: 1,
  code: "600001",
  name: "锚点股票",
  industry: "测试行业",
  anchor_price: 10,
  anchor_source_time: "2026-07-23T11:19:50+08:00",
  anchor_daily_return_pct: 2,
  price: 11,
  pct_change: 4.5,
  anchor_to_now_pct: 10,
  source: "tencent",
  source_time: "2026-07-23T13:30:00+08:00",
  scores: { local_score: 80, final_score: 80 },
  action: "executable",
  action_reason: "score_threshold_met",
  risks: [],
};
const frozenTodayRows = state.tableRows([frozenTodayItem], frozenToday);
assert.match(frozenTodayRows, /11:19:50/);
assert.match(frozenTodayRows, />11\.00</);
assert.match(frozenTodayRows, /\+10\.00%/);
const frozenTodayDrawer = state.drawer(frozenTodayItem, frozenToday);
assert.match(frozenTodayDrawer, /实际锚点时间/);
assert.match(frozenTodayDrawer, /11:19:50/);
assert.match(frozenTodayDrawer, /当前价/);
assert.match(frozenTodayDrawer, /锚点至今/);
const longDrawer = state.drawer(
  {
    ...frozenTodayItem,
    action: "observe",
    action_reason: "fixed_long_watchlist",
  },
  { strategy: "long", historical: false, degraded_reasons: [] },
);
assert.match(longDrawer, /观察结论/);
assert.match(longDrawer, /核心行情/);
assert.doesNotMatch(longDrawer, /最终评分/);
assert.doesNotMatch(longDrawer, /评分与风险/);
assert.strictEqual(state.sourceLabel("unavailable"), "行情暂不可用");
assert.strictEqual(state.sourceLabel("long_watchlist"), "长期观察名单");
assert.strictEqual(state.sourceLabel("tencent"), "腾讯行情");
assert.match(
  state.tableRows([
    {
      rank: 1,
      code: "688012",
      name: "中微公司",
      industry: "半导体设备",
      price: 182.4,
      pct_change: 1.23,
      amount: 900000000,
      turnover_rate: 2.5,
      market_cap: 120000000000,
      source: "eastmoney+sina",
      source_time: "2026-07-24T14:50:00+08:00",
    },
  ], { strategy: "long" }),
  /东方财富 · 新浪行情/,
);
assert.strictEqual(sandbox.window.TraderLongGroups.scopeLabel("chokepoint"), "卡脖子行业");
assert.strictEqual(sandbox.window.TraderLongGroups.scopeLabel("future_growth"), "高成长赛道");
const liveLongPayload = state.longGroupDisplayPayload({
  strategy: "long",
  status: "ready",
  items: [{ code: "600001", name: "实时名称", industry: "", price: 12.3, market_cap: 5000000000 }],
  long_groups: [],
});
assert.strictEqual(liveLongPayload.items[0].name, "实时名称");
assert.strictEqual(liveLongPayload.items[0].industry, "配置行业");
assert.strictEqual(liveLongPayload.items[0].market_cap, 5000000000);

const payload = {
  status: "ready",
  snapshot_id: "today-base",
  projection_version: "today-base",
  strategy: "today",
  trade_date: "2026-07-23",
  current_trade_date: "2026-07-23",
  view: "live",
  frozen: false,
  items: [{ code: "600001", rank: 1 }, { code: "600002", rank: 2 }],
};
const patch = {
  patch_schema_version: 2,
  schema_version: "v2_event_v1",
  base_projection_version: "today-base",
  projection_version: "today-next",
  snapshot_id: "today-next",
  strategy: "today",
  trade_date: "2026-07-23",
  view: "live",
  frozen: false,
  upserts: [{ code: "600001", rank: 2 }, { code: "600003", rank: 1 }],
  removed_codes: ["600002"],
};

assert.strictEqual(state.patchVersionValid(patch), true);
assert.strictEqual(
  state.recommendationPatchDecision(patch, payload, "today-base", "today", "live"),
  "apply",
);
assert.strictEqual(
  state.recommendationPatchDecision(patch, payload, "today-base", "today", "current"),
  "apply",
);
assert.strictEqual(
  state.recommendationPatchDecision(
    { ...patch, view: "official", frozen: true },
    payload,
    "today-base",
    "today",
    "current",
  ),
  "apply",
);
assert.strictEqual(
  state.recommendationPatchDecision({ ...patch, schema_version: "v2_event_v0" }, payload, "today-base", "today", "live"),
  "schema_mismatch",
);
assert.strictEqual(
  state.recommendationPatchDecision({ ...patch, trade_date: "2026-07-22" }, payload, "today-base", "today", "live"),
  "identity_mismatch",
);
assert.strictEqual(
  state.recommendationPatchDecision(
    { ...patch, base_projection_version: "unknown" },
    payload,
    "today-base",
    "today",
    "live",
  ),
  "base_mismatch",
);
assert.strictEqual(
  state.recommendationPatchDecision(
    patch,
    { ...payload, snapshot_id: "frozen", projection_version: "frozen", frozen: true, view: "official" },
    "frozen",
    "today",
    "official",
  ),
  "ignore_late_draft",
);
assert.strictEqual(
  state.recommendationPatchDecision(
    patch,
    { ...payload, snapshot_id: "frozen", projection_version: "frozen", frozen: true, view: "official" },
    "frozen",
    "today",
    "current",
  ),
  "ignore_late_draft",
);
const overlay = {
  patch_schema_version: 2,
  schema_version: "v2_event_v1",
  projection_version: "today-next",
  snapshot_id: "today-decision",
  strategy: "today",
  trade_date: "2026-07-23",
  quotes: [],
};
const current = { ...payload, snapshot_id: "today-decision", projection_version: "today-current" };
assert.strictEqual(state.overlayPatchDecision(overlay, current, "today-current", "today"), "apply");
assert.strictEqual(
  state.overlayPatchDecision({ ...overlay, projection_version: "wrong", snapshot_id: "wrong" }, current, "today-next", "today"),
  "overlay_projection_mismatch",
);
assert.strictEqual(state.eventMatchesCurrent({ strategy: "today", trade_date: "2026-07-23" }, "today", "2026-07-23"), true);
assert.strictEqual(state.eventMatchesCurrent({ strategy: "d25", trade_date: "2026-07-23" }, "today", "2026-07-23"), false);
assert.strictEqual(state.eventMatchesCurrent({ strategy: "today", trade_date: "2026-07-22" }, "today", "2026-07-23"), false);
assert(
  streamSource.includes("if (!state.date && eventMatchesCurrent(payload) && applyRecommendationPatch(payload))"),
  "unrelated strategy events must not apply decision patches",
);
assert(
  source.includes("current.projection_version === state.payload.projection_version"),
  "status reconciliation must compare the API projection identity after a lost event",
);

const merged = state.mergePatchItems(payload.items, patch.upserts, new Set(patch.removed_codes));
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(merged)),
  [{ code: "600003", rank: 1 }, { code: "600001", rank: 2 }],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.mergePatchItems(
    [{ code: "600010", rank: 1, action: "observe" }],
    [{ code: "600011", rank: 2, action: "executable" }],
    new Set(),
  ))),
  [
    { code: "600011", rank: 2, action: "executable" },
    { code: "600010", rank: 1, action: "observe" },
  ],
);
assert.strictEqual(state.topKValid(merged), true);
assert.strictEqual(
  state.topKValid(Array.from({ length: 26 }, (_value, index) => ({ code: String(600000 + index), rank: index + 1 })), "long"),
  true,
);
assert.strictEqual(
  state.topKValid(Array.from({ length: 13 }, (_value, index) => ({ code: String(600000 + index), rank: index + 1 })), "today"),
  false,
);
assert.strictEqual(
  state.topKValid(Array.from({ length: 12 }, (_value, index) => ({ code: String(600000 + index), rank: index + 1 })), "today"),
  true,
);
const splitPoolItems = [
  ...Array.from({ length: 6 }, (_value, index) => ({
    code: String(600100 + index),
    rank: index + 1,
    action: "executable",
  })),
  ...Array.from({ length: 6 }, (_value, index) => ({
    code: String(600200 + index),
    rank: index + 1,
    action: "observe",
  })),
];
assert.strictEqual(state.topKValid(splitPoolItems, "today"), true);
assert.strictEqual(
  state.topKValid([
    ...splitPoolItems,
    { code: "600999", rank: 7, action: "executable" },
  ], "today"),
  false,
);
assert.strictEqual(state.topKValid([{ code: "600001", rank: 1 }, { code: "600002", rank: 1 }]), false);
assert.strictEqual(state.topKValid([{ code: "600001", rank: 0 }]), false);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.resolveStrategyDate("today", "tomorrow", "2026-07-22", ["2026-07-22"]))),
  { date: "2026-07-22", availability: "available" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.resolveStrategyDate("today", "d25", "2026-07-22", ["2026-07-21"]))),
  { date: "2026-07-22", availability: "missing" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.resolveStrategyDate("tomorrow", "long", "2026-07-22", []))),
  { date: "", availability: "available" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.resolveStrategyDate("long", "today", "2026-07-22", ["2026-07-22"]))),
  { date: "", availability: "available" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.resolveStrategyDate("today", "tomorrow", "2026-07-22", null))),
  { date: "2026-07-22", availability: "unknown" },
);
const dateSelect = {
  disabled: false,
  options: [],
  value: "",
  append(option) { this.options.push(option); },
  set innerHTML(value) {
    assert.strictEqual(value, "");
    this.options.length = 0;
  },
};
state.renderDateOptions(dateSelect, "tomorrow", ["2026-07-21"], "2026-07-22", "missing");
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(dateSelect.options)),
  [
    { value: "", textContent: "当前" },
    { value: "2026-07-22", textContent: "2026-07-22（无数据）" },
    { value: "2026-07-21", textContent: "2026-07-21" },
  ],
);
assert.strictEqual(dateSelect.value, "2026-07-22");
assert.strictEqual(dateSelect.disabled, false);
const mixedItems = [
  { code: "600001", action: "executable" },
  { code: "600002", action: "observe" },
];
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.visibleRecommendations({ strategy: "today", historical: false, items: mixedItems }))),
  [{ code: "600001", action: "executable" }],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.visibleRecommendations({ strategy: "long", historical: false, items: mixedItems }))),
  mixedItems,
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.visibleRecommendations({ strategy: "today", historical: true, items: mixedItems }))),
  [{ code: "600001", action: "executable" }],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(
    state.observationRecommendations({ status: "ready", strategy: "today", historical: false, items: mixedItems }, "today_main"),
  )),
  [{ code: "600002", action: "observe" }],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.observationRecommendations({ strategy: "today", historical: true, items: mixedItems }))),
  [],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.snapshotNotice({
    status: "ready",
    strategy: "today",
    frozen: true,
    phase: "today_main",
    fusion_mode: "local_degraded",
    degraded_reasons: [
      "main:board_data_reliability_below_threshold",
      "deepseek_pending",
    ],
  }))),
  {
    level: "warning",
    message: "11:20 已冻结 · 名单与评分不变 · 行情按最新可用报价展示 · 冻结时降级：主板板块数据可靠度不足、模型复核未在冻结前完成（已按本地评分固化）",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.snapshotNotice({
    status: "ready",
    strategy: "tomorrow",
    historical: false,
    frozen: true,
    phase: "close_fallback",
    published_at: "2026-07-22T15:01:00+08:00",
    degraded_reasons: [],
  }))),
  {
    level: "ok",
    message: "已冻结 · 收盘补算 · 仅本地评分 · 15:01:00",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.snapshotNotice({
    status: "ready",
    strategy: "long",
    historical: false,
    frozen: false,
    phase: "long_current",
    published_at: "2026-07-22T15:01:00+08:00",
    degraded_reasons: [],
  }))),
  {
    level: "ok",
    message: "长期实时数据 · 不评分、不冻结 · 15:01:00",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.snapshotNotice({
    status: "ready",
    strategy: "tomorrow",
    historical: true,
    trade_date: "2026-07-22",
    frozen: true,
    phase: "afternoon",
    degraded_reasons: [],
  }))),
  {
    level: "ok",
    message: "历史快照 · 名单与评分为当日冻结结果 · 行情按最新可用报价展示",
  },
);
const longPayload = {
  strategy: "long",
  long_groups: [
    {
      name: "半导体设备",
      category: "chokepoint",
      source_section: "document_scan",
      codes: ["600002", "600001", "600003"],
      count: 3,
      sections: [
        { source_section: "document_scan", codes: ["600002", "600001"] },
        { source_section: "current_leaders", codes: ["600003"] },
      ],
    },
    { name: "具身智能", category: "future_growth", codes: ["600004", "600002"], count: 2 },
    { name: "芯片与电子", category: "low_price_potential", codes: ["600003", "600001"], count: 2 },
    { name: "算力与卫星", category: "low_price_potential", codes: ["600004", "600002"], count: 2 },
  ],
};
const longItems = [
  { code: "600001", rank: 11 },
  { code: "600002", rank: 12 },
  { code: "600003", rank: 13 },
  { code: "600004", rank: 14 },
];
const longAverage = state.longGroupAveragePct(longPayload.long_groups[0], [
  { code: "600001", pct_change: 1.2 },
  { code: "600002", pct_change: -0.4 },
  { code: "600003", pct_change: null },
  { code: "600004", pct_change: 99 },
]);
assert.ok(Math.abs(longAverage.average - 0.4) < 1e-12);
assert.strictEqual(longAverage.validCount, 2);
assert.strictEqual(longAverage.totalCount, 3);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.longGroupAveragePct(longPayload.long_groups[1], [
    { code: "600004", pct_change: null },
    { code: "600002", pct_change: "" },
  ]))),
  { average: null, validCount: 0, totalCount: 2 },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.longGroupAveragePct(longPayload.long_groups[2], [
    { code: "600003", pct_change: 0 },
    { code: "600001", pct_change: Number.NaN },
  ]))),
  { average: 0, validCount: 1, totalCount: 2 },
);
const longTabElements = {
  longSidebar: { hidden: true },
  longStockHeader: { hidden: true },
  longTitle: { textContent: "" },
  longScopeTabs: { querySelectorAll() { return []; } },
  longIndustryTabs: { innerHTML: "" },
  longMeta: { textContent: "" },
  longStockContext: { textContent: "" },
};
const longTabState = { longScope: "chokepoint", longGroup: "" };
state.longGroupRenderBar(longTabElements, longTabState, {
  ...longPayload,
  items: [
    { code: "600001", pct_change: 1.2 },
    { code: "600002", pct_change: -0.4 },
    { code: "600003", pct_change: null },
  ],
});
assert.match(longTabElements.longIndustryTabs.innerHTML, /半导体设备/);
assert.match(longTabElements.longIndustryTabs.innerHTML, /long-industry-average positive/);
assert.match(longTabElements.longIndustryTabs.innerHTML, /\+0\.40%/);
assert.match(longTabElements.longIndustryTabs.innerHTML, /有效行情 2\/3 只/);
assert.match(longTabElements.longIndustryTabs.innerHTML, />3 只<\/b>/);
state.longGroupRenderBar(longTabElements, longTabState, {
  ...longPayload,
  items: [
    { code: "600001", pct_change: -1 },
    { code: "600002", pct_change: -2 },
    { code: "600003", pct_change: null },
  ],
});
assert.match(longTabElements.longIndustryTabs.innerHTML, /long-industry-average negative/);
assert.match(longTabElements.longIndustryTabs.innerHTML, /-1\.50%/);
longTabState.longScope = "future_growth";
longTabState.longGroup = "";
state.longGroupRenderBar(longTabElements, longTabState, {
  ...longPayload,
  items: [{ code: "600004", pct_change: null }],
});
assert.match(longTabElements.longIndustryTabs.innerHTML, /long-industry-average is-unavailable/);
assert.match(longTabElements.longIndustryTabs.innerHTML, />--<\/em>/);
assert.match(longTabElements.longIndustryTabs.innerHTML, /有效行情 0\/2 只/);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.longGroupNormalized(longPayload, "chokepoint"))),
  [
    {
      key: "document_scan:半导体设备",
      name: "半导体设备",
      category: "chokepoint",
      source_section: "document_scan",
      sections: [
        { source_section: "document_scan", codes: ["600002", "600001"] },
        { source_section: "current_leaders", codes: ["600003"] },
      ],
      codes: ["600002", "600001", "600003"],
      count: 3,
    },
  ],
);
assert.deepStrictEqual(
  JSON.parse(
    JSON.stringify(
      state.longGroupVisibleRecommendations(longPayload, longItems, "chokepoint", "document_scan:半导体设备"),
    ),
  ),
  [
    { code: "600002", rank: 1, long_section: "document_scan", long_section_divider: false },
    { code: "600001", rank: 2, long_section: "document_scan", long_section_divider: false },
    { code: "600003", rank: 3, long_section: "current_leaders", long_section_divider: true },
  ],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.longGroupVisibleRecommendations(longPayload, longItems, "future_growth", "具身智能"))),
  [{ code: "600004", rank: 1 }, { code: "600002", rank: 2 }],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.longGroupVisibleRecommendations(longPayload, longItems, "low_price_potential", "算力与卫星"))),
  [{ code: "600004", rank: 1 }, { code: "600002", rank: 2 }],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.recommendationSummary(
    {
      status: "ready",
      stale: false,
      degraded_reasons: [],
    },
    [
      { scores: { final_score: 83.4 }, review: { outcome: "accepted" } },
      { scores: { final_score: 78.25 }, review: null },
    ],
  ))),
  {
    topScore: "83.40",
    modelReview: "1 / 2",
    dataQuality: "正常",
    dataQualityTitle: "",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.recommendationSummary(
    {
      status: "ready",
      score_status: "not_applicable",
      stale: false,
      degraded_reasons: [],
    },
    [{ scores: { final_score: 0 }, review: null }],
  ))),
  {
    topScore: "-",
    modelReview: "-",
    dataQuality: "正常",
    dataQualityTitle: "",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.recommendationSummary(
    {
      status: "not_ready",
      stale: true,
      degraded_reasons: ["snapshot_not_ready"],
    },
    [],
  ))),
  {
    topScore: "-",
    modelReview: "-",
    dataQuality: "无数据",
    dataQualityTitle: "荐股快照尚未就绪",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.recommendationSummary(
    {
      status: "ready",
      stale: false,
      degraded_reasons: ["model_unavailable", "quote_fallback"],
    },
    [{ scores: { final_score: null }, review: null }],
  ))),
  {
    topScore: "-",
    modelReview: "0 / 1",
    dataQuality: "降级 · 2项",
    dataQualityTitle: "模型服务暂不可用、行情已使用备用数据",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(sandbox.window.TraderRender.reasonLabels([
    "main:board_data_reliability_below_threshold",
    "corporate_risk_history_unavailable",
    "unknown_runtime_code",
  ]))),
  ["主板：板块数据可靠度不足", "公司风险历史暂不可核验", "部分数据暂不可用"],
);
assert.strictEqual(
  sandbox.window.TraderRender.statusErrorLabel(
    "TopK live overlay degraded: market-data result completed after its batch deadline",
  ),
  "TopK 行情刷新暂时降级",
);
assert.strictEqual(sandbox.window.TraderRender.fusionModeLabel("local_degraded"), "本地评分模式");
const runtimeDiagnostics = [];
sandbox.window.TraderRender.rememberDiagnostic(runtimeDiagnostics, "raw_runtime_code");
sandbox.window.TraderRender.rememberDiagnostic(runtimeDiagnostics, "raw_runtime_code");
assert.deepStrictEqual(JSON.parse(JSON.stringify(runtimeDiagnostics)), ["raw_runtime_code"]);
assert.strictEqual(state.isSnapshotNotFound({ code: "snapshot_not_found" }), true);
assert.strictEqual(state.isSnapshotNotFound({ code: "other" }), false);
assert(streamSource.includes("const STREAM_RETRY_INITIAL_MS = 1000;"), "SSE reconnect must retry after one second");
assert(streamSource.includes("const FALLBACK_POLL_MS = 3000;"), "disconnect polling must reconcile within three seconds");
const overlayPatchBody = source.match(/function applyOverlayPatch\(patch\) \{([\s\S]*?)\n  \}\n\n  function requestRecommendationResync/);
assert(overlayPatchBody, "overlay patch function must remain inspectable");
assert(!overlayPatchBody[1].includes("renderPayload(state.payload)"), "overlay patches must not rebuild the full table");

console.log("dashboard state contract passed");
