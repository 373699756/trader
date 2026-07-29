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
let source = fs.readFileSync(dashboardPath, "utf8");
const suffix = "\n})();";
source = source.trimEnd();
assert(source.endsWith(suffix), "dashboard.js must retain its IIFE boundary");

const sandbox = {
  URLSearchParams,
  console,
  document: { addEventListener() {}, createElement() { return {}; } },
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
  ["dependency_missing:TraderDashboardPatches"],
);
const state = {
  ...sandbox.window.TraderSelection,
  ...sandbox.window.TraderDashboardPatches,
  latencySummary: sandbox.window.TraderDashboardFormatters.latencySummary,
  drawer: sandbox.window.TraderRender.drawer,
  frozenTodayObservationTable: sandbox.window.TraderRender.frozenTodayObservationTable,
  frozenTodayTable: sandbox.window.TraderRender.frozenTodayTable,
  isFrozenTodayView: sandbox.window.TraderRender.isFrozenTodayView,
  longTable: sandbox.window.TraderRender.longTable,
  sourceLabel: sandbox.window.TraderRender.sourceLabel,
  tableColumnCount: sandbox.window.TraderRender.tableColumnCount,
  tableRows: sandbox.window.TraderRender.tableRows,
  longGroupAveragePct: sandbox.window.TraderLongGroups.groupAveragePct,
  longGroupDisplayPayload: sandbox.window.TraderLongGroups.displayPayload,
  longGroupNormalized: sandbox.window.TraderLongGroups.normalized,
  longGroupRenderBar: sandbox.window.TraderLongGroups.renderBar,
  longGroupVisibleRecommendations: sandbox.window.TraderLongGroups.visibleRecommendations,
};
assert(state, "dashboard D4 helpers were not exported into the test sandbox");
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
assert.strictEqual(
  state.frozenTodayObservationTable().head,
  "<tr><th>排名</th><th>股票</th><th>11:20锚点价</th><th>锚点时涨跌</th><th>当前价</th><th>当前涨跌</th><th>锚点至今</th><th>最终分</th><th>观察原因</th></tr>",
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
  schema_version: 2,
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
  state.recommendationPatchDecision({ ...patch, schema_version: 1 }, payload, "today-base", "today", "live"),
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
  schema_version: 2,
  projection_version: "today-next",
  snapshot_id: "today-next",
  strategy: "today",
  trade_date: "2026-07-23",
  quotes: [],
};
const current = { ...payload, snapshot_id: "today-next", projection_version: "today-next" };
assert.strictEqual(state.overlayPatchDecision(overlay, current, "today-next", "today"), "apply");
assert.strictEqual(
  state.overlayPatchDecision({ ...overlay, projection_version: "wrong", snapshot_id: "wrong" }, current, "today-next", "today"),
  "overlay_projection_mismatch",
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
  mixedItems,
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.observationRecommendations({ strategy: "today", historical: false, items: mixedItems }))),
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
    message: "已冻结 · 收盘补算 · 仅本地评分 · 2026/7/22 15:01:00",
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
    message: "长期实时数据 · 不评分、不冻结 · 2026/7/22 15:01:00",
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
    message: "历史快照 2026-07-22 · 名单与评分为当日冻结结果 · 行情按最新可用报价展示",
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

console.log("dashboard D4 state contract passed");
