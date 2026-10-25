"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const dashboardPath = process.argv[2] || path.join(__dirname, "../../src/trader/web/static/dashboard.js");
const selectionPath = path.join(path.dirname(dashboardPath), "selection.js");
const renderPath = path.join(path.dirname(dashboardPath), "render.js");
const longGroupsPath = path.join(path.dirname(dashboardPath), "long_groups.js");
const formattersPath = path.join(path.dirname(dashboardPath), "dashboard_formatters.js");
const patchesPath = path.join(path.dirname(dashboardPath), "dashboard_patches.js");
const patchesSource = fs.readFileSync(patchesPath, "utf8");
const statusHealthPath = path.join(path.dirname(dashboardPath), "status_health.js");
const errorDrawerPath = path.join(path.dirname(dashboardPath), "error_drawer.js");
const statusViewPath = path.join(path.dirname(dashboardPath), "status_view.js");
const statusViewSource = fs.readFileSync(statusViewPath, "utf8");
assert(statusViewSource.includes('[["static_filter"], "静态资格过滤", true]'));
assert(statusViewSource.includes('[["dynamic_filter"], "动态过滤", true]'));
assert(!statusViewSource.includes('["一级稳定过滤", ["dynamic_filter"]]'));
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
      const content = {
        'meta[name="trader-web-snapshot-retention-ms"]': "35000",
        'meta[name="trader-default-scoring-profile"]': "v2",
        'meta[name="trader-long-scoring-profile"]': "v2",
      }[selector];
      assert.notStrictEqual(content, undefined);
      return { content };
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
vm.runInNewContext(patchesSource, sandbox, { filename: patchesPath });
vm.runInNewContext(fs.readFileSync(statusHealthPath, "utf8"), sandbox, { filename: statusHealthPath });
vm.runInNewContext(fs.readFileSync(errorDrawerPath, "utf8"), sandbox, { filename: errorDrawerPath });
vm.runInNewContext(fs.readFileSync(statusViewPath, "utf8"), sandbox, { filename: statusViewPath });
vm.runInNewContext(fs.readFileSync(releaseContractPath, "utf8"), sandbox, { filename: releaseContractPath });
vm.runInNewContext(streamSource, sandbox, { filename: streamPath });
vm.runInNewContext(source.replace(suffix, "\n  window.TraderDashboardTest = Object.freeze({ normalizeDecisionPayload, configuredScoringProfile });\n})();"), sandbox, { filename: dashboardPath });
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
  longTable: sandbox.window.TraderRender.longTable,
  sourceLabel: sandbox.window.TraderRender.sourceLabel,
  formatDurationHms: sandbox.window.TraderStatusView.formatDurationHms,
  inputPipelineDetails: sandbox.window.TraderStatusView.inputPipelineDetails,
  pipelineFunnelSummary: sandbox.window.TraderStatusView.pipelineFunnelSummary,
  decisionPipelineDetails: sandbox.window.TraderStatusView.decisionPipelineDetails,
  healthView: sandbox.window.TraderStatusView.healthView,
  quoteAvailabilitySummary: sandbox.window.TraderStatusView.quoteAvailabilitySummary,
  renderInputQuality: sandbox.window.TraderStatusView.renderInputQuality,
  renderHealth: sandbox.window.TraderStatusView.renderHealth,
  renderPublicationStatus: sandbox.window.TraderStatusView.renderPublicationStatus,
  renderObservationStages: sandbox.window.TraderStatusView.renderObservationStages,
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
assert.strictEqual(state.descriptions.long, "长期研究 · 名单资格待核实\n· 仅展示当前数据");

const historyLoadingStatus = { market_data: {
  published_history_state: "unavailable",
  history_maintenance_state: "loading",
  history_maintenance_stage: "reading_active_snapshot",
} };
const historyLoadingPayload = { strategy: "tomorrow", status: "not_ready", trade_date: "2026-10-08" };
for (const strategy of ["tomorrow", "d25"]) {
  const failedPayload = { ...historyLoadingPayload, strategy };
  const readiness = sandbox.window.TraderStatusView.recommendationReadinessStatus(failedPayload, {
    market_data: {
      ...historyLoadingStatus.market_data,
      history_maintenance_state: "failed",
      history_maintenance_reason: "history_preload_deadline",
    },
  });
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(state.notReadyMessage(failedPayload, readiness))),
    {
      message: "本地历史加载失败，暂不能生成荐股",
      notice: "原因：history_preload_deadline；处理后重启服务重新加载",
      level: "warn",
    },
  );
}
const historyReadiness = sandbox.window.TraderStatusView.recommendationReadinessStatus(
  historyLoadingPayload, historyLoadingStatus,
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage(historyLoadingPayload, historyReadiness))),
  { message: "正在加载本地历史数据", notice: "历史加载完成后自动重试评分，请稍候", level: "idle" },
);
for (const payload of [
  { ...historyLoadingPayload, historical: true },
  { ...historyLoadingPayload, strategy: "long" },
  { ...historyLoadingPayload, status: "ready" },
]) {
  assert.notStrictEqual(
    sandbox.window.TraderStatusView.recommendationReadinessStatus(payload, historyLoadingStatus)?.primary_blocker,
    "history_projection_loading",
  );
}
assert.notStrictEqual(
  sandbox.window.TraderStatusView.recommendationReadinessStatus(historyLoadingPayload, {
    market_data: { ...historyLoadingStatus.market_data, published_history_state: "active" },
  })?.primary_blocker,
  "history_projection_loading",
);
assert(state, "dashboard state helpers were not exported into the test sandbox");
const unselectedLeader = {
  code: "688199", name: "久日新材", selected: false,
  action: "observe", action_reason: "industry_limit", scores: { final_score: 79.45 },
};
assert.strictEqual(
  state.detailItem({ strategy: "tomorrow", items: [], top_scores: [unselectedLeader] }, "688199").action_reason,
  "industry_limit",
);
assert.match(state.drawer(unselectedLeader, { strategy: "tomorrow" }), /未入池/);
assert.match(state.drawer(unselectedLeader, { strategy: "tomorrow" }), /超过行业集中度上限/);
const rankSnapshot = {
  strategy: "tomorrow",
  status: "ready",
  input_versions: {
    score_scale: "learned_rank_percentile_0_100",
    score_model_id: "learned_rank",
    execution_mode: "shadow",
    production_authority: "false",
  },
  selection_diagnostics: { maximum_final_score: 99.5, executable_threshold: 78, observation_floor: 73 },
};
const rankItem = {
  code: "600001", rank: 1, action: "observe", tier: "watch", review_outcome: "applied",
  scores: { local_score: 65, final_score: 99.5 },
};
assert.match(sandbox.window.TraderRender.tableDefinition(rankSnapshot).head, /模型排序/);
assert.match(sandbox.window.TraderRender.tableRows([rankItem], rankSnapshot), /aria-label="模型排序分"/);
const rankDrawer = state.drawer(rankItem, rankSnapshot);
assert.match(rankDrawer, /模型排序分/);
assert.match(rankDrawer, /本地评分/);
assert.match(rankDrawer, /65\.00/);
assert.match(rankDrawer, /99\.50/);
assert.match(rankDrawer, /排名百分位，不是上涨概率/);
assert.match(rankDrawer, /Shadow 仅供观察/);
assert(!rankDrawer.includes("最终评分仍为本地分"));
assert.match(state.drawer(rankItem, { strategy: "tomorrow", scoring_profile: "v1" }), /最终评分/);
assert(!state.drawer(rankItem, { strategy: "tomorrow", scoring_profile: "v1" }).includes("模型排序分"),
  "profile name alone must not reinterpret a historical score scale");
assert(!sandbox.window.TraderRender.tableDefinition({ ...rankSnapshot, strategy: "long" }).head.includes("模型排序"));
const rankEmpty = state.emptyRecommendationMessage(rankSnapshot, 0, null);
assert.match(rankEmpty, /排名百分位不与本地动作线比较/);
assert(!rankEmpty.includes("距离正式线"));
assert(!rankEmpty.includes("已达到正式线"));
const rankNotice = state.snapshotNotice(rankSnapshot, null);
assert.match(rankNotice.message, /Shadow/);
assert.match(rankNotice.message, /不计入正式推荐业绩/);
const v5Snapshot = {
  ...rankSnapshot,
  scoring_profile: "v5",
  input_versions: {
    score_scale: "weighted_evidence_quality_0_100",
    score_model: "v5_industry_tcn:model-hash",
    execution_mode: "shadow",
    production_authority: "false",
  },
};
const v5Item = {
  ...rankItem,
  action_reason: "engineering_shadow_only",
  scores: { local_score: 80, final_score: 80 },
};
assert.strictEqual(sandbox.window.TraderRender.scorePresentation(v5Snapshot).shadow, true);
assert.strictEqual(sandbox.window.TraderRender.scorePresentation(v5Snapshot).learnedRank, false);
assert.match(sandbox.window.TraderRender.tableDefinition(v5Snapshot).head, /最终/);
const v5Drawer = state.drawer(v5Item, v5Snapshot);
assert.match(v5Drawer, /最终评分/);
assert.match(v5Drawer, /Shadow 仅供观察/);
assert(!v5Drawer.includes("模型排序分"));
const v5Notice = state.snapshotNotice(v5Snapshot, null);
assert.match(v5Notice.message, /Shadow · 本地评分仅供观察/);
assert(!v5Notice.message.includes("模型排序"));
assert.strictEqual(sandbox.window.TraderRender.scorePresentation({
  ...v5Snapshot, input_versions: { score_scale: "weighted_evidence_quality_0_100" },
}).shadow, false, "profile name must not reinterpret historical V5 authority");
const rankPublication = summaryFixture();
state.renderPublicationStatus(rankPublication, rankSnapshot, null);
assert.strictEqual(rankPublication.publicationStatus.textContent, "Shadow 观察");
const previousQuerySelector = sandbox.document.querySelector;
sandbox.document.querySelector = () => ({ content: "v1" });
assert.strictEqual(sandbox.window.TraderDashboardTest.configuredScoringProfile(), "v1");
sandbox.document.querySelector = previousQuerySelector;
const configuredLongDetail = state.longGroupDisplayPayload(state.longGroupStaticFallbackPayload("pending"));
assert.strictEqual(state.detailItem(configuredLongDetail, "600001").name, "配置名称");
assert.strictEqual(state.detailItem(configuredLongDetail, "600001").action, "observe");
assert.strictEqual(
  state.longGroupDisplayPayload({ strategy: "long", items: [{ code: "600001", price: 15.25 }] }).items[0].price,
  15.25,
);
const scoreOnlySummary = summaryFixture();
state.renderSummary(
  scoreOnlySummary,
  {
    status: "ready",
    strategy: "tomorrow",
    trade_date: "2026-08-14",
    observed_at: "2026-08-14T19:53:30+08:00",
    frozen: true,
    score_status: "scored",
    input_versions: { score_scale: "weighted_evidence_quality_0_100" },
    coverage: { candidate_count: 57, evaluated_count: 57, rejected_count: 57 },
    items: [],
    selection_diagnostics: {
      maximum_final_score: 55,
      executable_threshold: 78,
      observation_floor: 73,
      empty_reason: "score_below_observation_floor",
    },
  },
  [],
  "closed",
  null,
  sandbox.window.TraderSelection,
  sandbox.window.TraderRender,
  {},
);
assert.strictEqual(scoreOnlySummary.inputQualityScoreTime.textContent, "评分于 19:53:30 完成");
for (const historical of [false, true]) {
  for (const status of [
    { market_data: { restart_required: true } },
    { scoring_profile: { maintenance: [{ restart_required: true }] } },
  ]) {
    const summary = summaryFixture();
    const payload = { status: "ready", strategy: "tomorrow", historical, trade_date: "2026-10-20", items: [] };
    state.renderSummary(summary, payload, [], "closed", null,
      sandbox.window.TraderSelection, sandbox.window.TraderRender, status);
    assert.strictEqual(summary.publicationMeta.textContent.includes("重启后采用"), !historical);
    state.renderSummary(summary, payload, [], "closed", null,
      sandbox.window.TraderSelection, sandbox.window.TraderRender, {});
    assert(!summary.publicationMeta.textContent.includes("重启后采用"));
  }
}
assert.strictEqual(
  sandbox.window.TraderDashboardDiagnostics.snapshot().webSnapshotRetentionMs,
  35000,
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.statusPayloadCompatibility({ schema_version: "runtime_status_unsupported" }))),
  { compatible: false, reason: "status_schema_mismatch" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.statusPayloadCompatibility({
    schema_version: "runtime_status",
    release: {
      decision_view_schema: "decision_view",
    },
  }))),
  { compatible: true, reason: "" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.decisionPayloadCompatibility({ schema_version: "decision_view_unsupported" }))),
  { compatible: false, reason: "decision_schema_mismatch" },
);
assert(patchesSource.includes("draft: null,"), "formal recommendation patches must clear observation drafts");
assert(
  source.includes("loadRecommendations, loadStatus, applyRecommendationPatch, applyOverlayPatch"),
  "dashboard must inject the direct recommendation patch handler into the SSE controller",
);
assert(
  streamSource.includes("applyRecommendationPatch(payload)"),
  "decision SSE must render its replacement patch without an unconditional snapshot GET",
);
assert.strictEqual(state.formatDurationHms(0), "0秒");
assert.strictEqual(state.formatDurationHms(-1), "0秒");
assert.strictEqual(state.formatDurationHms(Number.NaN), "0秒");
assert.strictEqual(state.formatDurationHms(59), "59秒");
assert.strictEqual(state.formatDurationHms(60), "1分 0秒");
assert.strictEqual(state.formatDurationHms(3599), "59分 59秒");
assert.strictEqual(state.formatDurationHms(3600), "1时 0分 0秒");
assert.strictEqual(state.formatDurationHms(95580), "26时 33分 0秒");
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.quoteAvailabilitySummary([
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
  { total: 3, available: 2, quoteMissing: 1 },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.quoteAvailabilitySummary([]))),
  { total: 0, available: 0, quoteMissing: 0 },
);
function summaryFixture() {
  return {
    inputQualityStatus: { textContent: "" },
    inputQualityMeta: { textContent: "" },
    inputQualityBlockers: { textContent: "" },
    inputQualityDegradations: { textContent: "" },
    inputQualityStages: { textContent: "" },
    funnelStatus: { textContent: "" },
    funnelStages: { textContent: "" },
    funnelMeta: { textContent: "" },
    sixSeatStatus: { textContent: "" },
    sixSeatMeta: { textContent: "" },
    quoteSource: { textContent: "" },
    quoteAge: { textContent: "" },
    quoteTime: { textContent: "" },
    quoteFreshness: { dataset: {} },
    budgetStatus: { textContent: "" },
    budgetMeta: { textContent: "" },
    inputQualityStrategy: { textContent: "" },
    inputQualityScoreTime: { textContent: "" },
    publicationStatus: { textContent: "" },
    publicationMeta: { textContent: "" },
    observationStageList: { innerHTML: "" },
  };
}
const modelBlockedPayload = { status: "not_ready", strategy: "tomorrow", view: "current", items: [] };
const modelBlockedRuntime = {
  run_mode: "preview", preview_state: "failed", preview_error: "production_model_contract_conflict",
  market_trade_date: "2026-10-23", preview_started_at: "2026-10-24T16:00:00+08:00",
  market_data: { active_source: "unavailable", candidate_quote_cache_entries: 0, candidate_quote_age: { sample_count: 0 } },
};
function renderModelBlockedSummary(runtime, payload = modelBlockedPayload, firstVisible = null) {
  const elements = summaryFixture();
  state.renderSummary(elements, payload, [], "closed", firstVisible,
    sandbox.window.TraderSelection, sandbox.window.TraderRender, runtime);
  return elements;
}
for (const reason of ["missing", "corrupt", "integrity_conflict", "contract_conflict", "publication_busy", "dependency_missing"]) {
  for (const strategy of ["tomorrow", "d25"]) {
    const summary = renderModelBlockedSummary(
      { ...modelBlockedRuntime, preview_error: `production_model_${reason}` },
      { ...modelBlockedPayload, strategy },
    );
    assert.strictEqual(summary.quoteSource.textContent, "行情尚未准备");
    assert.strictEqual(summary.inputQualityScoreTime.textContent, "模型未就绪，预览已阻断");
    assert.strictEqual(summary.publicationStatus.textContent, "测试预览 · 已阻塞");
  }
}
const cachedQuoteRuntime = { ...modelBlockedRuntime, market_data: {
  active_source: "unavailable", candidate_quote_latest_source: "tencent", candidate_quote_cache_entries: 1,
  candidate_quote_age: { sample_count: 1, latest_source_time: "2026-10-23T15:00:00+08:00" },
} };
assert.strictEqual(renderModelBlockedSummary(cachedQuoteRuntime).quoteSource.textContent, "腾讯行情");
assert.strictEqual(renderModelBlockedSummary(cachedQuoteRuntime).inputQualityScoreTime.textContent,
  "模型未就绪，预览已阻断");
assert.strictEqual(renderModelBlockedSummary(modelBlockedRuntime, modelBlockedPayload, {
  source: "tencent", source_time: "2026-10-23T15:00:00+08:00",
}).quoteSource.textContent, "腾讯行情");
for (const runtime of [
  { ...modelBlockedRuntime, preview_error: "quote_timeout" },
  { ...modelBlockedRuntime, preview_error: "production_model_features_missing" },
  { ...modelBlockedRuntime, preview_state: "waiting_inputs" },
  { ...modelBlockedRuntime, run_mode: "production" },
]) {
  const summary = renderModelBlockedSummary(runtime);
  assert.strictEqual(summary.quoteSource.textContent, "行情暂不可用");
  assert(!summary.inputQualityScoreTime.textContent.includes("预览已阻断"));
}
for (const payload of [
  { ...modelBlockedPayload, strategy: "long" },
  { ...modelBlockedPayload, historical: true, view: "history" },
  { ...modelBlockedPayload, status: "ready" },
]) {
  const summary = renderModelBlockedSummary(modelBlockedRuntime, payload);
  assert.notStrictEqual(summary.quoteSource.textContent, "行情尚未准备");
  assert(!summary.inputQualityScoreTime.textContent.includes("预览已阻断"));
}
const switchingElements = {
  ...summaryFixture(),
  recommendationTable: { classList: { remove() {}, add() {} } },
  tableColumns: { innerHTML: "" },
  tableHead: { innerHTML: "" },
  tableBody: { innerHTML: "" },
  longStockHeader: { hidden: false },
  longSidebar: { hidden: false },
  longScopeTabs: { hidden: false },
  resultLayout: { classList: { toggle() {} } },
};
switchingElements.budgetStatus.textContent = "旧策略预算";
const switchState = { strategy: "d25", payload: null };
const switchRenderer = sandbox.window.TraderStatusView.createDashboardStateRenderer(
  switchingElements, switchState, sandbox.window.TraderSelection, sandbox.window.TraderRender,
);
switchRenderer.renderLoadingState();
assert.strictEqual(switchingElements.quoteSource.textContent, "行情待确认");
assert.strictEqual((switchingElements.observationStageList.innerHTML.match(/class="observation-stage"/g) || []).length, 11);
assert.strictEqual(switchingElements.budgetStatus.textContent, "—");
assert.ok(switchingElements.tableHead.innerHTML.includes("<th>排名</th><th>状态</th><th>股票</th>"));
assert.strictEqual(switchingElements.longStockHeader.hidden, true);
switchRenderer.renderMissingHistoricalDate("d25", "2026-10-13");
assert.strictEqual((switchingElements.observationStageList.innerHTML.match(/class="observation-stage"/g) || []).length, 11);
assert.ok(switchingElements.tableHead.innerHTML.includes("<th>排名</th><th>状态</th><th>股票</th>"));
const stageKeys = ["local_data", "static_filter", "dynamic_market", "dynamic_filter", "market_features",
  "model_filter", "local_score", "local_risk", "fixed_selection", "risk_review", "action_publish"];
const outputs = [5289, 4000, 4000, 390, 380, 370, 370, 370, 6, 6, 6];
const stageSnapshots = stageKeys.map((stage, index) => ({
  stage, state: "ready", input_count: index ? outputs[index - 1] : 5289, output_count: outputs[index],
  input_batch_id: "round-a:" + index, output_batch_id: "round-a:" + (index + 1),
  rejected_count: index > 0 && index !== 8 ? outputs[index - 1] - outputs[index] : 0,
  pending_count: 0, failed_count: 0, not_selected_count: index === 8 ? 364 : 0,
  latency_ms: [135, 0, 1250, null][index % 4], reasons: [],
  as_of: "2026-08-14T09:15:00+08:00",
  operation_durations: index === 0 ? [{ operation: "cache_reuse", duration_ms: 135 }]
    : index === 1 ? [{ operation: "startup_reuse", duration_ms: null }] : [],
}));
const pipelineFixture = {
  current_stage: "action_publish", elapsed_ms: 2000, stage_snapshots: stageSnapshots,
  stages: stageSnapshots.map((stage) => ({ ...stage, key: stage.stage, state: "completed",
    facets: stage.stage === "action_publish" ? [
      { key: "action_executable", count: 3 }, { key: "action_observe", count: 2 },
      { key: "action_unavailable", count: 1 },
    ] : [], reason_counts: [] })),
};
const publishedPayload = { status: "ready", strategy: "tomorrow", trade_date: "2026-08-14",
  decision_version: "decision:tomorrow:immutable", content_hash: "hash-a",
  coverage: { evaluated_count: 370 }, items: [], pipeline: pipelineFixture };
const matchingReceipt = { operation: "formal_write", strategy: "tomorrow", trade_date: "2026-08-14",
  state: "failed", input_count: 1, output_count: 0, rejected_count: 0, pending_count: 0, failed_count: 1,
  latency_ms: 125, reasons: [{ code: "io_oserror", count: 1 }],
  decision_version: "decision:tomorrow:immutable", content_hash: "hash-a",
  source_health: { state: "unavailable", age_seconds: 20 } };
const canonicalObservationElements = summaryFixture();
const renderPublished = (receipts, extra = {}) => state.renderSummary(
  canonicalObservationElements, publishedPayload, [], "open", null,
  sandbox.window.TraderSelection, sandbox.window.TraderRender,
  { scheduler: { publication_io: receipts, ...extra } },
);
renderPublished([matchingReceipt, { ...matchingReceipt, operation: "checkpoint_consume", content_hash: "old-hash" }]);
assert.strictEqual((canonicalObservationElements.observationStageList.innerHTML.match(/class="observation-stage"/g) || []).length, 11);
for (const value of ["135 ms", "1.25 s", "0 ms", "未测量", "未入选 364", "固定评分前六",
  "本轮缓存复用 135 ms", "沿用启动结果", "本轮未重新执行", "文件或数据库读写失败（1次）"]) {
  assert(canonicalObservationElements.observationStageList.innerHTML.includes(value), value);
}
assert(canonicalObservationElements.funnelStages.textContent.includes("本轮至决策 2.00 s"));
assert(canonicalObservationElements.observationStageList.innerHTML.includes('data-publication-operation="formal_write"'));
assert(!canonicalObservationElements.observationStageList.innerHTML.includes('data-publication-operation="checkpoint_consume"'));
assert.match(canonicalObservationElements.observationStageList.innerHTML,
  /<button class="observation-stage-toggle"[^>]*aria-expanded="false"/);
renderPublished([{ ...matchingReceipt, decision_version: "old-round" }]);
assert(!canonicalObservationElements.observationStageList.innerHTML.includes('data-publication-operation="formal_write"'));
renderPublished([{ ...matchingReceipt, trade_date: "2026-08-13" }]);
assert(!canonicalObservationElements.observationStageList.innerHTML.includes('data-publication-operation="formal_write"'));
renderPublished([{ ...matchingReceipt, state: "ready", failed_count: 0, output_count: 1, reasons: [] }], {
  input_quality: { tomorrow: { summary: { trade_date: publishedPayload.trade_date },
    stage_snapshots: [{ output_batch_id: "round-b" }] } },
});
assert(canonicalObservationElements.funnelStages.textContent.includes("新输入轮正在处理"));
assert(canonicalObservationElements.observationStageList.innerHTML.includes("未入选 364"));
assert(!canonicalObservationElements.observationStageList.innerHTML.includes("文件或数据库读写失败"));

// Closing time must not remove the chain when no decision has been published.
const pendingClosePayload = {
  status: "not_ready", strategy: "tomorrow", trade_date: "2026-08-14", frozen: false,
  coverage: {}, items: [],
};
const closeChainElements = summaryFixture();
const renderCloseChain = (payload, quality = null) => state.renderSummary(
  closeChainElements, payload, [], "closed_market", null,
  sandbox.window.TraderSelection, sandbox.window.TraderRender,
  { phase: "after_close", scheduler: { input_quality: quality ? { tomorrow: quality } : {} } },
);
const chainCount = () => (closeChainElements.observationStageList.innerHTML.match(/class="observation-stage"/g) || []).length;
renderCloseChain(pendingClosePayload);
assert.strictEqual(chainCount(), 11);
assert(closeChainElements.observationStageList.innerHTML.includes("淘汰 — · 待补 — · 失败 —"));
assert(closeChainElements.observationStageList.innerHTML.includes("缺失字段不代表零"));
const pendingQuality = {
  summary: { trade_date: "2026-08-14" },
  stage_snapshots: stageSnapshots.slice(0, 3), pipeline: pipelineFixture,
};
renderCloseChain(pendingClosePayload, pendingQuality);
assert.strictEqual(chainCount(), 11);
assert(closeChainElements.observationStageList.innerHTML.includes("4000 → 4000"));
renderCloseChain(pendingClosePayload, {
  ...pendingQuality,
  stage_snapshots: pendingQuality.stage_snapshots.map((stage) => stage.stage === "dynamic_market"
    ? { ...stage, output_count: 3333 } : stage),
});
assert(closeChainElements.observationStageList.innerHTML.includes("4000 → 3333"));
renderCloseChain(pendingClosePayload, { ...pendingQuality, summary: { trade_date: "2026-08-13" } });
assert.strictEqual(chainCount(), 11);
assert(!closeChainElements.observationStageList.innerHTML.includes("4000"));
const frozenClosePayload = { ...publishedPayload, frozen: true };
renderCloseChain(frozenClosePayload, pendingQuality);
assert.strictEqual(chainCount(), 11);
assert(closeChainElements.observationStageList.innerHTML.includes("未入选 364"));
assert(!closeChainElements.observationStageList.innerHTML.includes("3333"));
renderCloseChain({ ...frozenClosePayload, historical: true }, pendingQuality);
assert.strictEqual(chainCount(), 11);
renderCloseChain({ ...pendingClosePayload, historical: true }, pendingQuality);
assert.strictEqual(chainCount(), 11);
assert(!closeChainElements.observationStageList.innerHTML.includes("4000"));
const nextRoundQuality = {
  ...pendingQuality, stage_snapshots: [{ ...stageSnapshots[0], output_batch_id: "round-b" }],
};
renderCloseChain(publishedPayload, nextRoundQuality);
assert(closeChainElements.funnelStages.textContent.includes("新输入轮正在处理"));
for (const payload of [frozenClosePayload, { ...publishedPayload, historical: true },
  { ...publishedPayload, view: "history" }]) {
  renderCloseChain(payload, nextRoundQuality);
  assert(!closeChainElements.funnelStages.textContent.includes("新输入轮正在处理"),
    "frozen and historical chains must not display a current scoring round");
  assert(closeChainElements.observationStageList.innerHTML.includes("未入选 364"));
}
renderCloseChain(publishedPayload, {
  ...nextRoundQuality, summary: { trade_date: "2026-08-13" },
});
assert(!closeChainElements.funnelStages.textContent.includes("新输入轮正在处理"),
  "a different trade date must not appear as the next round");
const selectableObservationElements = summaryFixture();
const missingModelElements = summaryFixture();
state.renderObservationStages(missingModelElements, stageSnapshots.map((stage) => stage.stage === "model_filter"
  ? { ...stage, state: "not_ready", input_count: 1401, output_count: 0, rejected_count: 0,
      pending_count: 1401, failed_count: 0, not_selected_count: 0, latency_ms: 1570,
      reasons: [{ code: "history_model_date_mismatch", count: 1401 }] }
  : stage), pipelineFixture, false, []);
assert(missingModelElements.observationStageList.innerHTML.includes("模型输入未就绪"));
assert(missingModelElements.observationStageList.innerHTML.includes("淘汰 0 · 待补 1401 · 失败 0"));
assert(missingModelElements.observationStageList.innerHTML.includes("模型历史截止日期不匹配"));
state.renderObservationStages(selectableObservationElements, stageSnapshots, pipelineFixture, false, []);
selectableObservationElements.observationStageList.querySelectorAll = () => [{ dataset: { stageKeys: "dynamic_filter" } }];
state.renderObservationStages(selectableObservationElements, stageSnapshots, pipelineFixture, false, []);
assert.match(selectableObservationElements.observationStageList.innerHTML,
  /class="observation-stage is-expanded"[^>]*data-stage-keys="dynamic_filter"[\s\S]*?aria-expanded="true"/);
const selectedMarkup = selectableObservationElements.observationStageList.innerHTML;
selectableObservationElements.observationStageList.contains = () => true;
sandbox.window.getSelection = () => ({ isCollapsed: false, anchorNode: {}, focusNode: {} });
state.renderObservationStages(selectableObservationElements, [], null, false, []);
assert.strictEqual(selectableObservationElements.observationStageList.innerHTML, selectedMarkup);
delete sandbox.window.getSelection;
assert.strictEqual(state.pipelineFunnelSummary(stageSnapshots, 3, 2),
  "荐股链路 5289→4000→4000→390→380→370→370→370→6→6→6 · 正式 3 · 观察 2");
assert.strictEqual(state.pipelineFunnelSummary([{ state: "not_ready", output_count: 0 }], 0, "已关闭"), null);
assert(state.inputPipelineDetails(pipelineFixture).includes("模型过滤 380→370"));
assert(state.decisionPipelineDetails(pipelineFixture).includes("固定评分前六 370→6"));
assert.strictEqual(sandbox.window.TraderRender.pipelineReasonLabel("<script>alert(1)</script>"), "原因说明暂未提供");
const legacyElements = summaryFixture();
state.renderObservationStages(legacyElements, [{ stage: "data_source", output_count: 800 }], null, false, []);
assert(legacyElements.observationStageList.innerHTML.includes("历史记录按原协议只读"));
state.renderObservationStages(legacyElements, stageSnapshots, pipelineFixture, true, []);
assert(legacyElements.observationStageList.innerHTML.includes("长期固定观察池不经过"));
const summaryElements = summaryFixture();
state.renderSummary(
  summaryElements,
  {
    status: "ready",
    strategy: "tomorrow",
    trade_date: "2026-08-14",
    frozen: false,
    score_status: "scored",
    coverage: { candidate_count: 120, evaluated_count: 80, rejected_count: 40 },
  },
  [
    {
      code: "600002", name: "观察股票", industry: "证券", action: "observe",
      price: null, pct_change: null, source: "decision", source_time: "2026-08-14T09:59:00+08:00",
      scores: { final_score: 75 },
    },
    {
      code: "600001", name: "正式股票", industry: "银行", action: "executable",
      price: 10, pct_change: 1, source: "tencent", source_time: "2026-08-14T10:00:00+08:00",
      scores: { final_score: 82 },
    },
  ],
  "open",
  { source: "tencent" },
  sandbox.window.TraderSelection,
  sandbox.window.TraderRender,
  { deepseek_budget: { limit: 168, used: 2, remaining: 166 } },
);
assert.strictEqual(summaryElements.inputQualityStatus.textContent, "已评分 80 / 总体 120");
assert.strictEqual(summaryElements.inputQualityMeta.textContent, "最高 82.00 · 阶段范围与门槛未保存");
assert.strictEqual(summaryElements.funnelStatus.textContent, "阶段观测不可用");
assert.strictEqual(summaryElements.funnelStages.textContent, "旧快照未保存逐阶段运行观测；不以聚合计数拼接漏斗");
assert.strictEqual(summaryElements.inputQualityStrategy.textContent, "明");
assert.strictEqual(summaryElements.inputQualityScoreTime.textContent, "评分时间不可用");
assert.strictEqual(summaryElements.publicationStatus.textContent, "实时滚动");
assert.strictEqual(summaryElements.publicationMeta.textContent, "明 15:00 固化");
const persistedPipelineElements = summaryFixture();
state.renderSummary(
  persistedPipelineElements,
  {
    status: "ready",
    strategy: "tomorrow",
    trade_date: "2026-08-14",
    frozen: true,
    observed_at: "2026-08-14T14:48:00+08:00",
    score_status: "scored",
    coverage: { candidate_count: 5289, evaluated_count: 110, rejected_count: 5093 },
    selection_diagnostics: { maximum_final_score: 65.42 },
    pipeline: pipelineFixture,
  },
  [],
  "closed",
  null,
  sandbox.window.TraderSelection,
  sandbox.window.TraderRender,
  { scheduler: { input_quality: {} }, deepseek_budget: { limit: 168, used: 0, remaining: 168 } },
);
assert.strictEqual(persistedPipelineElements.inputQualityStatus.textContent, "已发布轮 · 评分 370");
assert.ok(persistedPipelineElements.inputQualityStages.textContent.includes("本地资料加载 5289"));
assert.ok(persistedPipelineElements.inputQualityStages.textContent.includes("动态过滤 4000→390"));
assert.ok(persistedPipelineElements.inputQualityStages.textContent.includes("模型过滤 380→370"));
assert.strictEqual(persistedPipelineElements.funnelStatus.textContent, "已发布本地结果");
assert.ok(!persistedPipelineElements.funnelStages.textContent.includes("模型成本门"));
assert.ok(!persistedPipelineElements.funnelStages.textContent.includes("最终入池"));
state.renderSummary(
  summaryElements,
  {
    status: "ready",
    strategy: "tomorrow",
    trade_date: "2026-08-14",
    frozen: false,
    score_status: "scored",
    coverage: { candidate_count: 120, evaluated_count: 80, rejected_count: 40 },
  },
  [{
    code: "600001", name: "正式股票", industry: "银行", action: "executable",
    price: 10, pct_change: 1, source: "tencent", source_time: "2026-08-14T10:00:00+08:00",
    scores: { final_score: 82 },
  }],
  "open",
  { source: "tencent" },
  sandbox.window.TraderSelection,
  sandbox.window.TraderRender,
  {
    scheduler: {
      input_quality: {
        tomorrow: {
          candidate_count: 120,
          candidate_scored_count: 80,
          pipeline: { current_stage: "evidence_score", stages: [
            { key: "input_coverage", state: "completed", input_count: 120, output_count: null, facets: [
              { key: "candidate_features", count: 120, total: 120 },
              { key: "security_master", count: 118, total: 120 },
              { key: "history", count: 96, total: 120 },
            ] },
            { key: "evidence_score", state: "completed", input_count: 120, output_count: 80 },
          ] },
          summary: { trade_date: "2026-08-14", quote_total_count: 120, quote_covered_count: 120 },
          candidate_optional_reason_counts: { cross_source_deviation: 3 },
        },
      },
    },
  },
);
assert.strictEqual(summaryElements.inputQualityStatus.textContent, "已评分 80 / 总体 120");
assert.strictEqual(summaryElements.funnelStatus.textContent, "阶段观测不可用");
assert.ok(!summaryElements.inputQualityMeta.textContent.includes("历史 96"));
assert.ok(!summaryElements.inputQualityBlockers.textContent.includes("历史不足 24"));
assert.ok(!summaryElements.inputQualityDegradations.textContent.includes("跨源价格偏差 3"));
state.renderSummary(
  summaryElements,
  {
    status: "not_ready",
    strategy: "tomorrow",
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
        tomorrow: {
          status: "not_ready",
          candidate_optional_reason_counts: {
            missing_listing_date: 240,
            board_identity_degraded: 240,
          },
          pipeline: { current_stage: "concentration", stages: [
            { key: "dynamic_filter", state: "completed", input_count: 576, output_count: 360,
              facets: [{ key: "filter_reject", count: 216, total: 576 }] },
            { key: "candidate_refresh", state: "completed", input_count: 360, output_count: 360 },
            { key: "input_coverage", state: "degraded", input_count: 360, output_count: null, facets: [
              { key: "candidate_features", count: 360, total: 360 },
              { key: "security_master", count: 120, total: 360 },
              { key: "history", count: 78, total: 360 },
            ] },
            { key: "evidence_score", state: "completed", input_count: 360, output_count: 56 },
            { key: "fusion", state: "completed", input_count: 56, output_count: 56,
              metric_ranges: [{ metric: "final_score", minimum: 41.25, maximum: 74.25 }] },
            { key: "action_gate", state: "completed", input_count: 56, output_count: 4, facets: [
              { key: "action_executable", count: 2, total: 56 },
              { key: "action_observe", count: 2, total: 56 },
            ] },
            { key: "concentration", state: "completed", input_count: 4, output_count: 4, facets: [
              { key: "selected_executable", count: 2, total: 4 },
              { key: "selected_observe", count: 2, total: 4 },
            ] },
          ] },
          summary: {
            trade_date: "2026-08-14",
            quote_total_count: 360,
            quote_covered_count: 360,
            quote_missing_count: 0,
            security_identity_missing_count: 240,
            latest_quote_source: "tencent",
            latest_quote_source_time: "2026-08-14T10:00:00+08:00",
            highest_final_score: 74.25,
          },
        },
      },
    },
  },
);
assert.strictEqual(summaryElements.inputQualityStatus.textContent, "尚无已发布轮");
assert.strictEqual(summaryElements.funnelStatus.textContent, "尚无已发布轮");
assert(!summaryElements.funnelMeta.textContent.includes("360"));
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
assert.strictEqual(summaryElements.inputQualityStatus.textContent, "尚无已发布轮");
assert.strictEqual(summaryElements.funnelMeta.textContent, "等待本轮发布");
assert.strictEqual(summaryElements.inputQualityStages.textContent, "运行中输入不与已发布名单拼接");
assert.strictEqual(summaryElements.quoteSource.textContent, "腾讯行情");
assert.strictEqual(summaryElements.publicationStatus.textContent, "采集中");
assert.strictEqual(summaryElements.publicationMeta.textContent, "等待本轮正式结果");
const notReadyAgeElements = {
  quoteAge: { textContent: "" },
  quoteTime: { textContent: "" },
  quoteSource: summaryElements.quoteSource,
  quoteFreshness: { dataset: {} },
};
state.updateQuoteAge(
  notReadyAgeElements,
  {
    status: "not_ready",
    strategy: "tomorrow",
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
assert.strictEqual(notReadyAgeElements.quoteAge.textContent, "1分 5秒");
assert.notStrictEqual(notReadyAgeElements.quoteTime.textContent, "-");
const emptyTomorrowElements = summaryFixture();
const liveCandidateTime = new Date(Date.now() - 65_000).toISOString();
state.renderSummary(
  emptyTomorrowElements,
  {
    status: "ready",
    strategy: "tomorrow",
    trade_date: "2026-08-14",
    observed_at: "2026-08-14T11:19:00+08:00",
    frozen: false,
    score_status: "scored",
    coverage: { candidate_count: 295, evaluated_count: 85, rejected_count: 210 },
    items: [],
  },
  [],
  "empty",
  null,
  sandbox.window.TraderSelection,
  sandbox.window.TraderRender,
  {
    market_data: {
      candidate_quote_latest_source: "tencent",
      candidate_quote_age: { latest_source_time: liveCandidateTime },
    },
    scheduler: {
      input_quality: {
        tomorrow: {
          summary: {
            trade_date: "2026-08-14",
            latest_quote_source: "tencent",
            latest_quote_source_time: "2026-08-14T11:19:00+08:00",
          },
        },
      },
    },
  },
);
state.updateQuoteAge(
  emptyTomorrowElements,
  { status: "ready", strategy: "tomorrow", trade_date: "2026-08-14", items: [] },
  sandbox.window.TraderRender,
  { market_data: { candidate_quote_latest_source: "tencent", candidate_quote_age: { latest_source_time: liveCandidateTime } } },
);
assert.strictEqual(emptyTomorrowElements.quoteSource.textContent, "腾讯行情");
assert.strictEqual(emptyTomorrowElements.quoteAge.textContent, "1分 5秒");
assert.strictEqual(emptyTomorrowElements.inputQualityScoreTime.textContent, "评分于 11:19:00 完成");
assert.strictEqual(emptyTomorrowElements.publicationStatus.textContent, "实时滚动");
assert.strictEqual(emptyTomorrowElements.publicationMeta.textContent, "明 15:00 固化");
state.renderPublicationStatus(
  emptyTomorrowElements,
  { status: "ready", strategy: "d25", frozen: true },
  {},
);
assert.strictEqual(emptyTomorrowElements.publicationStatus.textContent, "已冻结");
assert.strictEqual(emptyTomorrowElements.publicationMeta.textContent, "D25 15:00 已固化");
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
assert.strictEqual(summaryElements.inputQualityScoreTime.textContent, "长期策略不评分");
assert.strictEqual(summaryElements.publicationStatus.textContent, "不适用");
assert.strictEqual(summaryElements.publicationMeta.textContent, "长期固定观察池，不评分、不冻结");
state.renderInputQuality(summaryElements, {
  strategy: "long",
  status: "ready",
}, [{
  code: "600001", name: "长期股票", industry: "行业",
  price: null, pct_change: null, source: "long_watchlist", source_time: null,
  quote_status: "missing",
}], null, null);
assert.strictEqual(summaryElements.inputQualityStatus.textContent, "不适用");
assert.strictEqual(summaryElements.inputQualityMeta.textContent, "长期固定观察池不评分");
assert.strictEqual(
  state.initialStrategy({
    strategies: {
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
      tomorrow: { status: "ready", coverage: { selected_count: 6 } },
      long: { status: "ready", coverage: { selected_count: 224 } },
    },
  }),
  "tomorrow",
);
assert.strictEqual(state.initialStrategy({ strategies: {} }), "tomorrow");
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
}, ["corporate_risk_window_unavailable"]);
assert.strictEqual(degradedHealth.level, "degraded");
assert.strictEqual(degradedHealth.issueCount, 2);
assert.strictEqual(degradedHealth.badge, "降级 · 2项");
assert.strictEqual(degradedHealth.primary.message, "行情刷新暂时不可用");
assert.strictEqual(degradedHealth.primary.meta.includes("明"), true);
assert.strictEqual(degradedHealth.primary.meta.includes("数据刷新"), true);
assert.strictEqual(degradedHealth.issues.length, 2);
const runtimeRows = state.runtimeErrorRows(degradedHealth.issues);
assert.strictEqual(runtimeRows.includes("refresh:source_unavailable"), true);
assert.strictEqual(runtimeRows.includes("近一年公司风险覆盖不足"), true);
assert.strictEqual(runtimeRows.includes("活动中"), true);
const normalHealth = state.healthView({ health: { level: "normal", issue_count: 0 }, recent_errors: [] }, []);
assert.strictEqual(normalHealth.badge, "正常 · 无最近错误");
assert.strictEqual(normalHealth.primary, null);
const healthElements = {
  errorDetailsButton: { hidden: false, disabled: false, dataset: {}, title: "" },
  healthBadge: { textContent: "" },
};
const recoveredHealth = state.renderHealth(healthElements, {
  health: { level: "normal", issue_count: 0 },
  recent_errors: [{
    code: "review:review_unavailable",
    severity: "degraded",
    strategy: "tomorrow",
    stage: "review",
    recovery_status: "recovered",
  }],
}, [], "tomorrow");
assert.strictEqual(healthElements.errorDetailsButton.hidden, true);
assert.strictEqual(recoveredHealth.visibleIssues.length, 0);
const activeHealth = state.renderHealth(healthElements, {
  health: { level: "degraded", issue_count: 1 },
  recent_errors: [{
    code: "refresh:source_unavailable",
    severity: "degraded",
    strategy: "tomorrow",
    stage: "refresh",
    recovery_status: "active",
  }],
}, [], "tomorrow");
assert.strictEqual(healthElements.errorDetailsButton.hidden, false);
assert.strictEqual(healthElements.errorDetailsButton.disabled, false);
assert.strictEqual(healthElements.healthBadge.textContent, "异常 1");
assert.strictEqual(activeHealth.visibleIssues.length, 1);
const longHealth = state.renderHealth(healthElements, {
  health: { level: "degraded", issue_count: 1 },
  recent_errors: [{
    code: "refresh:source_unavailable",
    severity: "degraded",
    strategy: null,
    stage: "refresh",
    recovery_status: "active",
  }],
}, [], "long");
assert.strictEqual(healthElements.errorDetailsButton.disabled, true);
assert.strictEqual(healthElements.errorDetailsButton.title, "长期策略不展示评分链路观察面板");
assert.strictEqual(healthElements.healthBadge.textContent, "观察不适用");
assert.strictEqual(longHealth.visibleIssues.length, 1);
assert.strictEqual(
  sandbox.window.TraderStatusView.issueSummaryTitle(degradedHealth.issues),
  "最近错误 · 活动2项",
);
assert.strictEqual(
  sandbox.window.TraderStatusView.issueSummaryTitle([
    ...degradedHealth.issues,
    {
      code: "review:review_unavailable",
      severity: "degraded",
      strategy: "tomorrow",
      stage: "review",
      recoveryStatus: "recovered",
    },
  ]),
  "最近错误 · 活动2项 / 已恢复1项",
);
assert.strictEqual(state.currentViewMatches("long", "current"), true);
assert.strictEqual(state.currentViewMatches("tomorrow", "current"), true);
assert.strictEqual(state.currentViewMatches("tomorrow", "current"), true);
assert.strictEqual(state.currentViewMatches("d25", "current"), true);
assert.strictEqual(state.currentViewMatches("tomorrow", "live"), true);
assert.strictEqual(state.currentViewMatches("tomorrow", "official"), true);
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
assert.strictEqual(state.observationDisplayState(liveShort, "before_close"), "open");
assert.strictEqual(state.observationDisplayState(liveShort, "after_close"), "closed_market");
assert.strictEqual(state.observationDisplayState({ ...liveShort, frozen: true }, "before_close"), "closed_frozen");
assert.strictEqual(state.observationDisplayState({ ...liveShort, historical: true }, "before_close"), "hidden_history");
assert.strictEqual(
  state.observationDisplayState({ ...liveShort, status: "not_ready", historical: true }, "before_close"),
  "hidden_history",
);
assert.strictEqual(state.observationDisplayState(liveShort, ""), "unknown");
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.observationRecommendations(liveShort, "before_close"))),
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
assert.strictEqual(state.observationDisplayState(notReadyDraft, "before_close"), "open");
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.observationRecommendations(notReadyDraft, "before_close"))),
  [{ code: "600003", action: "observe" }],
);
const emptyObservationDraft = { ...notReadyDraft, draft: { items: [] } };
assert.strictEqual(state.observationDisplayState(emptyObservationDraft, "before_close"), "empty");
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.observationRecommendations(emptyObservationDraft, "before_close"))),
  [],
);
assert.strictEqual(state.observationEmptyMessage("empty"), "本轮无股票达到观察条件");
assert.strictEqual(state.observationEmptyMessage("warming"), "正在生成观察草稿");
assert.strictEqual(state.observationEmptyMessage("unavailable"), "本轮尚无可用观察草稿，请查看运行状态");
assert.strictEqual(
  state.observationDisplayState(
    { ...notReadyDraft, draft: null },
    "before_close",
    { scheduler: { lanes: [{ name: "trader-tomorrow", running: true, pending: false }] } },
  ),
  "warming",
);
assert.strictEqual(
  state.observationDisplayState(
    { ...notReadyDraft, draft: null },
    "before_close",
    { scheduler: { lanes: [{ name: "trader-tomorrow", running: false, pending: false, completed_count: 58 }] } },
  ),
  "unavailable",
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage({
    strategy: "tomorrow",
    readiness_reason: "snapshot_not_published",
  }))),
  {
    message: "明策略当前快照尚未发布",
    notice: "当前策略快照尚未形成，等待本地评分发布",
    level: "idle",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage({
    strategy: "d25",
    readiness_reason: "afternoon_freeze_pending",
  }))),
  {
    message: "15:00 正式快照尚未形成",
    notice: "冻结流程尚未完成；不会展示上一交易日结果",
    level: "warn",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage({
    strategy: "d25",
    readiness_reason: "afternoon_close_recovery_pending",
  }))),
  {
    message: "15:00 正式快照缺失",
    notice: "正在等待允许的收盘恢复；不会展示上一交易日结果",
    level: "warn",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage({
    strategy: "long",
    readiness_reason: "long_snapshot_not_ready",
  }))),
  {
    message: "长期策略当前尚无可用数据",
    notice: "长期策略只展示当前研究快照",
    level: "idle",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage(
    { strategy: "tomorrow", readiness_reason: "snapshot_not_published" },
    {
      primary_blocker: "candidate_quotes_pending",
      pipeline: { stages: [{ key: "candidate_refresh", state: "running", input_count: 360, output_count: 128 }] },
    },
  ))),
  {
    message: "采集中｜候选行情 128 / 360，评分尚未完成",
    notice: "采集中｜候选行情 128 / 360，评分尚未完成",
    level: "idle",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage(
    { strategy: "tomorrow", readiness_reason: "snapshot_not_published" },
    {
      primary_blocker: "market_population_stale",
      population_count: 5291,
      population_filter_reason_counts: { stale_quote: 5089 },
    },
  ))),
  {
    message: "暂不可发布｜全市场行情已过期 5089 / 5291，等待本轮行情刷新",
    notice: "暂不可发布｜全市场行情已过期 5089 / 5291，等待本轮行情刷新",
    level: "warn",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage(
    { strategy: "tomorrow", readiness_reason: "snapshot_not_published" },
    {
      primary_blocker: "market_liquidity_history_unavailable",
      population_count: 5279,
      population_filter_reason_counts: { missing_liquidity_history: 5165 },
    },
  ))),
  {
    message: "暂不可发布｜全市场流动性历史未就绪 5165 / 5279，等待历史档案同步",
    notice: "暂不可发布｜全市场流动性历史未就绪 5165 / 5279，等待历史档案同步",
    level: "warn",
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage(
    { strategy: "tomorrow", readiness_reason: "snapshot_not_published" },
    {
      primary_blocker: "model_input_unavailable",
      pipeline: { stages: [
        { key: "strategy_history", state: "completed", input_count: 205, output_count: 201 },
        { key: "model_input", state: "completed", input_count: 201, output_count: 0 },
      ] },
    },
  ))),
  {
    message: "暂不可发布｜策略历史合格 201 只，但模型输入合格 0 只；请核对模型所需行业与特征字段",
    notice: "暂不可发布｜策略历史合格 201 只，但模型输入合格 0 只；请核对模型所需行业与特征字段",
    level: "warn",
  },
);
const securityMasterBlocked = state.notReadyMessage(
  { strategy: "tomorrow", readiness_reason: "snapshot_not_published" },
  {
    primary_blocker: "security_master_coverage_incomplete",
    pipeline: { stages: [
      { key: "candidate_refresh", state: "completed", input_count: 360, output_count: 360 },
      { key: "input_coverage", state: "degraded", input_count: 360, output_count: null, facets: [
        { key: "candidate_features", count: 360, total: 360 },
        { key: "security_master", count: 120, total: 360 },
      ] },
    ] },
    candidate_optional_reason_counts: { missing_listing_date: 240 },
  },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(securityMasterBlocked)),
  {
    message: "暂不可发布｜基础资料 120 / 360，要求 360 / 360",
    notice: "暂不可发布｜基础资料 120 / 360，要求 360 / 360",
    level: "warn",
  },
);
assert.ok(!securityMasterBlocked.message.includes("上市日期"));
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.notReadyMessage(
    { strategy: "d25", readiness_reason: "snapshot_not_published" },
    {
      primary_blocker: "strategy_history_unavailable",
      history_required_sessions: 61,
      pipeline: { stages: [
        { key: "candidate_refresh", state: "completed", input_count: 360, output_count: 360 },
        { key: "input_coverage", state: "degraded", input_count: 360, output_count: null,
          facets: [{ key: "history", count: 350, total: 360 }] },
      ] },
    },
  ))),
  {
    message: "暂不可发布｜符合至少 61 个交易日 350 / 360",
    notice: "暂不可发布｜符合至少 61 个交易日 350 / 360",
    level: "warn",
  },
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
  state.emptyRecommendationMessage(
    {
      strategy: "tomorrow",
      input_versions: { score_scale: "weighted_evidence_quality_0_100" },
      coverage: { evaluated_count: 239 },
      selection_diagnostics: {
        empty_reason: "no_positive_net_utility",
        maximum_final_score: 100,
        observation_floor: 73,
        executable_threshold: 78,
      },
    },
    0,
    {
      pipeline: { stages: [{ key: "action_gate", state: "completed", input_count: 239, output_count: 0,
        facets: [
          { key: "observation_threshold_met", count: 0, total: 239 },
          { key: "executable_threshold_met", count: 0, total: 239 },
        ] }] },
      supply_reason_counts: {
        corporate_risk_window_unavailable: 360,
        stale_quote: 360,
        structured_risk_unavailable: 360,
        model_net_utility_non_positive: 239,
      },
    },
  ),
  "评分已完成｜最高统一评分 100.00；239 只已评分股票的预测成本后净超额均未转正，因此观察池和正式推荐均为 0只，保持空仓；主要原因：近一年公司风险覆盖不足（360只）、行情已过期，仅供观察（360只）、结构化风险数据尚未就绪（360只）",
);
assert.strictEqual(
  state.emptyRecommendationMessage({
    strategy: "tomorrow",
    coverage: { evaluated_count: 239 },
    selection_diagnostics: {
      empty_reason: "no_positive_net_utility",
      maximum_final_score: 97.44,
    },
  }),
  "评分已完成｜最高相对信号分 97.44（仅表示 239 只已评分股票内的排序，不代表已通过成本门）；239 只已评分股票的预测成本后净超额均未转正，因此观察池和正式推荐均为 0只，保持空仓",
);
assert.strictEqual(
  state.emptyRecommendationMessage({
    strategy: "tomorrow",
    frozen: true,
    coverage: { evaluated_count: 0 },
    selection_diagnostics: {
      empty_reason: "no_positive_net_utility",
      maximum_final_score: 0,
      observation_floor: 73,
      executable_threshold: 78,
    },
  }),
  "冻结记录未形成可评分候选，不能证明成本后净超额均未转正；本轮保持空仓，等待下一交易日重新评分",
);
assert.strictEqual(
  state.emptyRecommendationMessage({
    strategy: "tomorrow",
    frozen: false,
    coverage: { evaluated_count: 0 },
    selection_diagnostics: { empty_reason: "no_positive_net_utility" },
  }),
  "当前快照未形成可评分候选，不能证明成本后净超额均未转正；本轮保持空仓并等待重新评分",
);
assert.strictEqual(
  state.emptyRecommendationMessage(
    {
      strategy: "d25",
      selection_diagnostics: {
        empty_reason: "risk_or_execution_blocked",
        maximum_final_score: 76.4,
        observation_floor: 73,
        executable_threshold: 78,
      },
    },
    2,
    {
      pipeline: { stages: [{ key: "action_gate", state: "completed", input_count: 15, output_count: 0,
        facets: [
          { key: "observation_threshold_met", count: 15, total: 15 },
          { key: "executable_threshold_met", count: 0, total: 15 },
        ] }] },
      supply_reason_counts: {
        below_score_threshold: 12,
        risk_veto: 5,
        corporate_risk_window_unavailable: 3,
        stale_quote: 1,
      },
    },
  ),
  "评分已完成｜最高分 76.40，距离正式线 1.60；达到观察线 15只、正式线 0只；主要原因：评分未达到执行门槛（12只）、风险事实触发限制（5只）、近一年公司风险覆盖不足（3只）",
);
assert.strictEqual(
  state.emptyRecommendationMessage({
    selection_diagnostics: {
      empty_reason: "risk_or_execution_blocked",
      blocked_reason_counts: { "corporate_risk_window_unavailable": 1 },
    },
  }),
  "达到观察门槛的候选均不可执行：近一年公司风险覆盖不足（1只）",
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
  "<tr><th>类别</th><th>股票</th><th>最新价</th><th>今日涨跌</th><th>成交 / 换手</th><th>总市值</th><th>行情来源 / 时间</th></tr>",
);
assert.strictEqual(state.tableColumnCount({ strategy: "long", historical: false }), 7);
const getItem = {
  rank: 2, code: "600001", name: "证据股票", board: "main", industry: "测试行业",
  action: "executable", action_reason: "score_threshold_met", selected: true,
  setup: { type: "volume_breakout" }, review_outcome: "applied",
  research_coverage: { evidence_count: 3, structured_risk_fact_count: 1, review_eligible: true,
    components: { financial: "known_clear", announcements: "stale", pledge: "unknown", unlock: "known_risk" } },
  risk_codes: ["unlock_risk"],
  risk_details: [{ risk_code: "unlock_risk", origin: "deepseek", veto_applied: true }],
  quote: { price: 11, pct_change: 1, amount: 10000000, turnover_rate: 2, market_cap: 1000000000 },
  anchor_quote: { price: 10 },
  scores: {
    local: 80, final: 80, deepseek: null, deepseek_risk_penalty: 0,
    model_signal_score: 82.5, predicted_excess_return_pct: 1.45,
    estimated_cost_pct: 0.2, predicted_net_excess_pct: 1.25, model_disagreement_pct: 0.1,
  },
};
const getPayload = sandbox.window.TraderDashboardTest.normalizeDecisionPayload(
  { strategy: "tomorrow", status: "ready", trade_date: "2026-07-23", items: [getItem], top_scores: [getItem] },
  "tomorrow", "", "current",
);
const patchItem = {
  rank: 2, code: "600001", name: "证据股票", board: "main", industry: "测试行业",
  action: "executable", action_reason: "score_threshold_met", setup: { type: "volume_breakout" },
  review_outcome: "applied",
  research_coverage: { evidence_count: 3, structured_risk_fact_count: 1, review_eligible: true,
    components: { financial: "known_clear", announcements: "stale", pledge: "unknown", unlock: "known_risk" } },
  price: 11, pct_change: 1, amount: 10000000, turnover_rate: 2, market_cap: 1000000000,
  anchor_price: 10, quote_status: "decision_anchor",
  risks: [{ risk_code: "unlock_risk", origin: "deepseek", veto_applied: true }],
  scores: {
    local_score: 80, final_score: 80, deepseek_score: null, deepseek_risk_penalty: 0,
    model_signal_score: 82.5, predicted_excess_return_pct: 1.45,
    estimated_cost_pct: 0.2, predicted_net_excess_pct: 1.25, model_disagreement_pct: 0.1,
  },
};
const patchPayload = state.replacementPayload(getPayload, {
  publication_id: "test-publication", snapshot_id: "test-publication", projection_version: "test-projection",
  strategy: "tomorrow", trade_date: "2026-07-23", view: "live", phase: "current",
  coverage: {}, upserts: [patchItem], top_scores: [patchItem],
}, [patchItem]);
for (const payload of [getPayload, patchPayload]) {
  const item = payload.items[0];
  const row = state.tableRows([item], payload);
  const detail = state.drawer(item, payload);
  assert.match(row, /<td>2<\/td>/);
  assert.doesNotMatch(row, /风险研究：/);
  assert.match(row, /已复核/);
  assert.doesNotMatch(row, /<td>长期<\/td>|未复核/);
  assert.match(detail, /模型相对信号分<\/span><strong>82\.50/);
  assert.match(detail, /入场形态<\/span><strong>放量突破/);
  assert.match(detail, /预测成本后净超额<\/span><strong>\+1\.25%/);
  assert.match(detail, /研究证据<\/span><strong>3 条/);
  assert.match(detail, /结构化风险事实<\/span><strong>1 条/);
  assert.match(detail, /财务数据<\/span><strong>已核：未见风险/);
  assert.match(detail, /公司公告 \/ 减持<\/span><strong>已过期/);
  assert.match(detail, /股权质押<\/span><strong>未获取/);
  assert.match(detail, /限售解禁<\/span><strong>已核：有风险/);
  assert.match(detail, /限售股解禁风险/);
  assert.match(detail, /DeepSeek · 已生效否决/);
  assert.doesNotMatch(detail, /未知风险|扣分 -|财务数据<\/span><strong>未获取/);
  assert.strictEqual(state.recommendationSummary(payload, [item]).modelReview, "1 / 1");
  const budgetElements = { budgetStatus: {}, budgetMeta: {} };
  sandbox.window.TraderStatusView.renderBudgetSummary(
    budgetElements, { used: 1, remaining: 167, limit: 168 }, payload,
  );
  assert.match(budgetElements.budgetMeta.textContent, /复核 1\/1/);
  assert.strictEqual(payload.top_scores[0].scores.model_signal_score, 82.5);
}
const observedReview = { ...patchItem, code: "600002", action: "observe", review_outcome: "applied" };
const budgetWithObservation = { budgetStatus: {}, budgetMeta: {} };
sandbox.window.TraderStatusView.renderBudgetSummary(
  budgetWithObservation, { used: 2, remaining: 166, limit: 168 },
  { ...patchPayload, items: [patchItem, observedReview] },
);
assert.match(budgetWithObservation.budgetMeta.textContent, /复核 1\/1/);
const budgetVisibility = {
  budgetPanel: { hidden: false },
  summaryBand: { classList: { toggle(name, enabled) {
    assert.strictEqual(name, "is-three-column");
    budgetVisibility.threeColumns = enabled;
  } } },
  budgetStatus: {}, budgetMeta: {},
};
sandbox.window.TraderStatusView.renderBudgetSummary(
  budgetVisibility, { used: 0, remaining: 168, limit: 168 }, patchPayload, { enabled: false },
);
assert.strictEqual(budgetVisibility.budgetPanel.hidden, true);
assert.strictEqual(budgetVisibility.threeColumns, true);
sandbox.window.TraderStatusView.renderBudgetSummary(
  budgetVisibility, { used: 0, remaining: 168, limit: 168 }, patchPayload, { enabled: true },
);
assert.strictEqual(budgetVisibility.budgetPanel.hidden, false);
assert.strictEqual(budgetVisibility.threeColumns, false);
sandbox.window.TraderStatusView.renderBudgetSummary(
  budgetVisibility, { used: 0, remaining: 168, limit: 168 }, patchPayload,
);
assert.strictEqual(budgetVisibility.budgetPanel.hidden, true);
assert.strictEqual(budgetVisibility.threeColumns, true);
assert.match(sandbox.window.TraderRender.tableDefinition(getPayload).head, /本地 \/ 风险复核 \/ 最终/);
assert.match(sandbox.window.TraderRender.tableDefinition(getPayload).head,
  /今日涨跌<\/th><th>换手率<\/th><th>成交额<\/th>/);
assert.match(sandbox.window.TraderRender.tableDefinition(getPayload).head,
  /股票<\/th><th>板块行业<\/th><th>最新价/);
assert.doesNotMatch(sandbox.window.TraderRender.tableDefinition(getPayload).head, /动作<\/th>/);
assert.strictEqual(state.tableColumnCount(getPayload), 11);
const noResearchItem = { ...getPayload.items[0], research_coverage: null, review_outcome: null, risks: [] };
assert.doesNotMatch(state.drawer(noResearchItem, getPayload), /公司风险研究|风险研究：|风险扣分/);
assert.match(state.tableRows([noResearchItem], getPayload), /无记录/);
const unknownResearchItem = { ...noResearchItem, code: "600312", industry: "", action: "unavailable",
  pct_change: 1.2, research_coverage: { evidence_count: 2, structured_risk_fact_count: 0,
    review_eligible: false,
    components: { financial: "unknown", announcements: "unknown", pledge: "unknown", unlock: "unknown" } } };
const unknownRow = state.tableRows([unknownResearchItem], getPayload);
assert.match(unknownRow, /600312<\/span><\/td>\s*<td class="board-industry-cell"><span class="stock-name">主板/);
assert.doesNotMatch(unknownRow, /未分类|风险研究：/);
assert.match(unknownRow, /<td class="positive">\+1\.20%<\/td>/);
assert.match(unknownRow, /未入复核/);
assert.doesNotMatch(unknownRow, /<\/b>本地|<\/b>风险复核|<\/b>最终/);
assert.doesNotMatch(unknownRow, /<td class="action-cell">/);
const classifiedRow = state.tableRows([{ ...unknownResearchItem,
  classified_industry: "D44电力、热力生产和供应业" }], getPayload);
assert.match(classifiedRow, /<td class="board-industry-cell"><span class="stock-name">主板<\/span><span class="stock-code">证监会行业 D44电力、热力生产和供应业/);
assert.doesNotMatch(state.drawer(unknownResearchItem, getPayload), /公司风险研究|风险研究：/);
assert.match(state.drawer(unknownResearchItem, getPayload), /未进入本次 DeepSeek 复核候选/);
assert.match(state.tableRows([{ ...unknownResearchItem, pct_change: -1.2 }], getPayload),
  /<td class="negative">-1\.20%<\/td>/);
assert.match(state.tableRows([{ ...unknownResearchItem, research_coverage: {
  ...unknownResearchItem.research_coverage, review_eligible: true } }], getPayload), /无结果/);
const legacyRiskItem = { ...getPayload.items[0], research_coverage: { evidence_count: 2 },
  risks: [{ risk_code: "unlock_risk" }] };
assert.doesNotMatch(state.drawer(legacyRiskItem, getPayload), /公司风险研究/);
assert.match(state.drawer(legacyRiskItem, getPayload), /来源未保存/);
const scoredItem = {
  rank: 1,
  code: "600001",
  name: "锚点股票",
  board: "main",
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
const modelVersionDrawer = state.drawer(
  {
    ...scoredItem,
    scores: {
      ...scoredItem.scores,
      model_signal_score: 82.5,
      predicted_excess_return_pct: 1.45,
      estimated_cost_pct: 0.2,
      predicted_net_excess_pct: 1.25,
      model_disagreement_pct: 0.1,
    },
  },
  {
    strategy: "tomorrow",
    historical: false,
    degraded_reasons: [],
    input_versions: {
      score_model: "daily_reconstructible_ensemble:model-hash",
    },
  },
);
assert.match(modelVersionDrawer, /daily_reconstructible_ensemble:model-hash/);
assert.match(modelVersionDrawer, /模型相对信号分/);
assert.doesNotMatch(modelVersionDrawer, /P2信号分/);
const longDrawer = state.drawer(
  {
    ...scoredItem,
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
      source: "tencent+sina",
      source_time: "2026-07-24T14:50:00+08:00",
    },
  ], { strategy: "long" }),
  /腾讯行情 · 新浪行情/,
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
sandbox.window.TraderLongWatchlistData.items = [];
sandbox.window.TraderLongWatchlistData.groups = [];
const emptyLongPayload = state.longGroupDisplayPayload({
  strategy: "long",
  status: "ready",
  items: [{ code: "600001", name: "旧运行快照" }],
  long_groups: [{ name: "旧分组", category: "chokepoint", codes: ["600001"] }],
});
assert.deepStrictEqual(emptyLongPayload.items, []);
assert.deepStrictEqual(emptyLongPayload.long_groups, []);
assert.strictEqual(emptyLongPayload.status, "not_ready");

const payload = {
  status: "ready",
  snapshot_id: "tomorrow-base",
  projection_version: "tomorrow-base",
  strategy: "tomorrow",
  trade_date: "2026-07-23",
  current_trade_date: "2026-07-23",
  view: "live",
  frozen: false,
  coverage: {
    candidate_count: 360,
    evaluated_count: 0,
    rejected_count: 89,
    selected_count: 0,
    executable_count: 0,
    observation_count: 0,
  },
  items: [{ code: "600001", rank: 1 }, { code: "600002", rank: 2 }],
};
const patch = {
  patch_schema_version: 4,
  schema_version: "decision_event",
  publication_id: "tomorrow-next",
  base_projection_version: "tomorrow-base",
  projection_version: "tomorrow-next",
  snapshot_id: "tomorrow-next",
  strategy: "tomorrow",
  trade_date: "2026-07-23",
  view: "live",
  frozen: false,
  coverage: {
    candidate_count: 360,
    evaluated_count: 229,
    rejected_count: 89,
    selected_count: 2,
    executable_count: 2,
    observation_count: 0,
  },
  upserts: [{ code: "600001", rank: 2 }, { code: "600003", rank: 1 }],
  removed_codes: ["600002"],
};

assert.strictEqual(state.patchVersionValid(patch), true);
assert.strictEqual(
  state.recommendationPatchDecision(patch, payload, "tomorrow-base", "tomorrow", "live"),
  "apply",
);
const coveragePatch = {
  ...patch,
  replace: true,
  upserts: [],
  removed_codes: [],
  coverage: {
    ...patch.coverage,
    selected_count: 0,
    executable_count: 0,
  },
};
assert.strictEqual(
  state.recommendationPatchDecision(coveragePatch, payload, "tomorrow-base", "tomorrow", "live"),
  "apply",
);
const replacementPayload = state.replacementPayload(payload, coveragePatch, []);
assert.strictEqual(replacementPayload.publication_id, coveragePatch.publication_id);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(replacementPayload.coverage)),
  coveragePatch.coverage,
  "a complete SSE replacement must atomically replace stale snapshot coverage",
);
const replacementSummary = summaryFixture();
state.renderSummary(
  replacementSummary,
  replacementPayload,
  [],
  "empty",
  null,
  sandbox.window.TraderSelection,
  sandbox.window.TraderRender,
  null,
);
assert.strictEqual(replacementSummary.funnelStatus.textContent, "阶段观测不可用");
assert.strictEqual(
  state.recommendationPatchDecision(patch, payload, "tomorrow-base", "tomorrow", "current"),
  "apply",
);
assert.strictEqual(
  state.recommendationPatchDecision({ ...patch, coverage: null }, payload, "tomorrow-base", "tomorrow", "current"),
  "schema_mismatch",
);
assert.strictEqual(
  state.recommendationPatchDecision(
    { ...patch, view: "official", frozen: true },
    payload,
    "tomorrow-base",
    "tomorrow",
    "current",
  ),
  "apply",
);
assert.strictEqual(
  state.recommendationPatchDecision({ ...patch, schema_version: "decision_event_unsupported" }, payload, "tomorrow-base", "tomorrow", "live"),
  "schema_mismatch",
);
assert.strictEqual(
  state.recommendationPatchDecision({ ...patch, publication_id: "other-publication" }, payload, "tomorrow-base", "tomorrow", "live"),
  "schema_mismatch",
);
assert.strictEqual(
  state.recommendationPatchDecision({ ...patch, trade_date: "2026-07-22" }, payload, "tomorrow-base", "tomorrow", "live"),
  "identity_mismatch",
);
assert.strictEqual(
  state.recommendationPatchDecision(
    { ...patch, base_projection_version: "unknown" },
    payload,
    "tomorrow-base",
    "tomorrow",
    "live",
  ),
  "base_mismatch",
);
assert.strictEqual(
  state.recommendationPatchDecision(
    patch,
    { ...payload, snapshot_id: "frozen", projection_version: "frozen", frozen: true, view: "official" },
    "frozen",
    "tomorrow",
    "official",
  ),
  "ignore_late_draft",
);
assert.strictEqual(
  state.recommendationPatchDecision(
    patch,
    { ...payload, snapshot_id: "frozen", projection_version: "frozen", frozen: true, view: "official" },
    "frozen",
    "tomorrow",
    "current",
  ),
  "ignore_late_draft",
);
const overlay = {
  patch_schema_version: 4,
  schema_version: "decision_event",
  publication_id: "tomorrow-decision",
  projection_version: "tomorrow-next",
  snapshot_id: "tomorrow-decision",
  strategy: "tomorrow",
  trade_date: "2026-07-23",
  quotes: [],
};
const current = { ...payload, snapshot_id: "tomorrow-decision", projection_version: "tomorrow-current" };
assert.strictEqual(state.overlayPatchDecision(overlay, current, "tomorrow-current", "tomorrow"), "apply");
assert.strictEqual(
  state.overlayPatchDecision(
    { ...overlay, publication_id: "wrong", projection_version: "wrong", snapshot_id: "wrong" },
    current,
    "tomorrow-next",
    "tomorrow",
  ),
  "overlay_projection_mismatch",
);
assert.strictEqual(state.eventMatchesCurrent({ strategy: "tomorrow", trade_date: "2026-07-23" }, "tomorrow", "2026-07-23"), true);
assert.strictEqual(state.eventMatchesCurrent({ strategy: "d25", trade_date: "2026-07-23" }, "tomorrow", "2026-07-23"), false);
assert.strictEqual(state.eventMatchesCurrent({ strategy: "tomorrow", trade_date: "2026-07-22" }, "tomorrow", "2026-07-23"), false);
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
    { code: "600010", rank: 1, action: "observe" },
    { code: "600011", rank: 2, action: "executable" },
  ],
);
assert.strictEqual(state.topKValid(merged), true);
assert.strictEqual(
  state.topKValid(Array.from({ length: 26 }, (_value, index) => ({ code: String(600000 + index), rank: index + 1 })), "long"),
  true,
);
assert.strictEqual(
  state.topKValid(Array.from({ length: 13 }, (_value, index) => ({ code: String(600000 + index), rank: index + 1 })), "tomorrow"),
  false,
);
assert.strictEqual(
  state.topKValid(Array.from({ length: 6 }, (_value, index) => ({ code: String(600000 + index), rank: index + 1 })), "tomorrow"),
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
assert.strictEqual(state.topKValid(splitPoolItems, "tomorrow"), false);
assert.strictEqual(state.topKValid([
  { code: "600100", rank: 1, action: "executable" },
  { code: "600101", rank: 2, action: "observe" },
  { code: "600102", rank: 3, action: "unavailable" },
], "tomorrow"), true);
assert.strictEqual(
  state.topKValid([
    ...splitPoolItems,
    { code: "600999", rank: 7, action: "executable" },
  ], "tomorrow"),
  false,
);
assert.strictEqual(state.topKValid([{ code: "600001", rank: 1 }, { code: "600002", rank: 1 }]), false);
assert.strictEqual(state.topKValid([{ code: "600001", rank: 0 }]), false);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.resolveStrategyDate("tomorrow", "tomorrow", "2026-07-22", ["2026-07-22"]))),
  { date: "2026-07-22", availability: "available" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.resolveStrategyDate("tomorrow", "d25", "2026-07-22", ["2026-07-21"]))),
  { date: "2026-07-22", availability: "missing" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.resolveStrategyDate("tomorrow", "long", "2026-07-22", []))),
  { date: "", availability: "available" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.resolveStrategyDate("long", "tomorrow", "2026-07-22", ["2026-07-22"]))),
  { date: "", availability: "available" },
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.resolveStrategyDate("tomorrow", "tomorrow", "2026-07-22", null))),
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
  JSON.parse(JSON.stringify(state.visibleRecommendations({ strategy: "tomorrow", historical: false, items: mixedItems }))),
  mixedItems,
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.visibleRecommendations({ strategy: "long", historical: false, items: mixedItems }))),
  mixedItems,
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.visibleRecommendations({ strategy: "tomorrow", historical: true, items: mixedItems }))),
  mixedItems,
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(
    state.observationRecommendations({ status: "ready", strategy: "tomorrow", historical: false, items: mixedItems }, "before_close"),
  )),
  [{ code: "600002", action: "observe" }],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.observationRecommendations({ strategy: "tomorrow", historical: true, items: mixedItems }))),
  [],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.snapshotNotice({
    status: "ready",
    strategy: "tomorrow",
    frozen: true,
    phase: "before_close",
    fusion_mode: "local_degraded",
    degraded_reasons: [
      "main:board_data_reliability_below_threshold",
      "deepseek_pending",
    ],
  }))),
  {
    level: "warning",
    message: "15:00 已冻结 · 名单与评分不变 · 行情按最新可用报价展示 · 冻结时降级：主板板块数据可靠度不足、模型复核未在冻结前完成（已按本地评分固化）",
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
    phase: "before_close",
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
      { scores: { final_score: 83.4 }, review_outcome: "applied" },
      { scores: { final_score: 78.25 }, review_outcome: null },
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
    "corporate_risk_window_unavailable",
    "strategy_history_coverage_partial",
    "structured_risk_unavailable",
    "cross_source_deviation",
    "history_data_degraded",
  ]))),
  [
    "主板：板块数据可靠度不足",
    "近一年公司风险覆盖不足",
    "候选历史数据覆盖不完整",
    "结构化风险数据尚未就绪",
    "跨源价格校验存在偏差",
    "历史行情数据可靠度不足",
  ],
);
assert.strictEqual(
  sandbox.window.TraderRender.statusErrorLabel(
    "TopK live overlay degraded: market-data result completed after its batch deadline",
  ),
  "TopK 行情刷新暂时降级",
);
assert.strictEqual(sandbox.window.TraderRender.fusionModeLabel("local_degraded"), "本地评分模式");
assert.strictEqual(sandbox.window.TraderRender.actionReason("industry_limit"), "超过行业集中度上限");
assert.strictEqual(sandbox.window.TraderRender.actionReason("top_k_limit"), "位于正式候选池名额之外");
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
assert(overlayPatchBody[1].includes("top_scores: (state.payload.top_scores || []).map(updateQuotedItem)"),
  "overlay patches must update visible six-seat quote details without changing score identity");

console.log("dashboard state contract passed");
