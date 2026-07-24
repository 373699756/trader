"use strict";

const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const dashboardPath = process.argv[2];
const path = require("path");
const selectionPath = path.join(path.dirname(dashboardPath), "selection.js");
const renderPath = path.join(path.dirname(dashboardPath), "render.js");
const longGroupsPath = path.join(path.dirname(dashboardPath), "long_groups.js");
const utilsPath = path.join(path.dirname(dashboardPath), "dashboard_utils.js");
let source = fs.readFileSync(dashboardPath, "utf8");
const suffix = "\n})();";
source = source.trimEnd();
assert(source.endsWith(suffix), "dashboard.js must retain its IIFE boundary");
source = `${source.slice(0, -suffix.length)}
  window.__dashboardD4 = {
    emptyRecommendationMessage,
    mergePatchItems,
    overlayPatchDecision,
    patchVersionValid,
    recommendationPatchDecision,
    topKValid,
  };
})();`;

const sandbox = {
  URLSearchParams,
  console,
  document: { addEventListener() {}, createElement() { return {}; } },
  window: { addEventListener() {} },
};
vm.runInNewContext(fs.readFileSync(renderPath, "utf8"), sandbox, { filename: renderPath });
vm.runInNewContext(fs.readFileSync(selectionPath, "utf8"), sandbox, { filename: selectionPath });
vm.runInNewContext(fs.readFileSync(longGroupsPath, "utf8"), sandbox, { filename: longGroupsPath });
vm.runInNewContext(fs.readFileSync(utilsPath, "utf8"), sandbox, { filename: utilsPath });
vm.runInNewContext(source, sandbox, { filename: dashboardPath });
const state = {
  ...sandbox.window.TraderSelection,
  ...sandbox.window.__dashboardD4,
  latencySummary: sandbox.window.TraderDashboardUtils.latencySummary,
  longGroupNormalized: sandbox.window.TraderLongGroups.normalized,
  longGroupVisibleRecommendations: sandbox.window.TraderLongGroups.visibleRecommendations,
};
assert(state, "dashboard D4 helpers were not exported into the test sandbox");
assert.strictEqual(
  state.emptyRecommendationMessage({
    selection_diagnostics: {
      empty_reason: "score_below_observation_floor",
      maximum_final_score: 64.5,
      selection_floor: 65,
    },
  }),
  "最高评分 64.50，低于观察门槛 65.00，本轮不荐股",
);
assert.strictEqual(
  state.emptyRecommendationMessage({
    selection_diagnostics: { empty_reason: "risk_or_execution_blocked" },
  }),
  "候选达到评分门槛，但被风险或执行条件拦截",
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.latencySummary([10, 20, 30]))),
  { sample_count: 3, p50_ms: 20, p95_ms: 30, maximum_ms: 30 },
);

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
assert.strictEqual(state.topKValid(merged), true);
assert.strictEqual(
  state.topKValid(Array.from({ length: 26 }, (_value, index) => ({ code: String(600000 + index), rank: index + 1 })), "long"),
  true,
);
assert.strictEqual(
  state.topKValid(Array.from({ length: 19 }, (_value, index) => ({ code: String(600000 + index), rank: index + 1 })), "today"),
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
const longPayload = {
  strategy: "long",
  long_groups: [
    { name: "半导体设备", category: "chokepoint", codes: ["600002", "600001"], count: 2 },
    { name: "低价潜力股", category: "low_price_potential", codes: ["600003", "600001"], count: 2 },
  ],
};
const longItems = [
  { code: "600001", rank: 11 },
  { code: "600002", rank: 12 },
  { code: "600003", rank: 13 },
];
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.longGroupNormalized(longPayload, "chokepoint"))),
  [{ name: "半导体设备", category: "chokepoint", codes: ["600002", "600001"], count: 2 }],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.longGroupVisibleRecommendations(longPayload, longItems, "chokepoint", "半导体设备"))),
  [{ code: "600002", rank: 1 }, { code: "600001", rank: 2 }],
);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(state.longGroupVisibleRecommendations(longPayload, longItems, "low_price_potential", ""))),
  [{ code: "600003", rank: 1 }, { code: "600001", rank: 2 }],
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
