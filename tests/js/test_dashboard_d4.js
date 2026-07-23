"use strict";

const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const dashboardPath = process.argv[2];
let source = fs.readFileSync(dashboardPath, "utf8");
const suffix = "\n})();";
source = source.trimEnd();
assert(source.endsWith(suffix), "dashboard.js must retain its IIFE boundary");
source = `${source.slice(0, -suffix.length)}
  window.__dashboardD4 = {
    currentViewLabel,
    latencySummary,
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
  document: { addEventListener() {} },
  window: { addEventListener() {} },
};
vm.runInNewContext(source, sandbox, { filename: dashboardPath });
const state = sandbox.window.__dashboardD4;
assert(state, "dashboard D4 helpers were not exported into the test sandbox");
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
assert.strictEqual(state.currentViewLabel(null), "正在判断");
assert.strictEqual(state.currentViewLabel({ status: "not_ready" }), "未就绪");
assert.strictEqual(state.currentViewLabel({ status: "ready", historical: true }), "历史冻结");
assert.strictEqual(
  state.currentViewLabel({ status: "ready", strategy: "long", historical: false, frozen: false }),
  "当前快照",
);
assert.strictEqual(
  state.currentViewLabel({ status: "ready", phase: "close_fallback", historical: false, frozen: true }),
  "收盘补算",
);
assert.strictEqual(
  state.currentViewLabel({ status: "ready", historical: false, frozen: false }),
  "实时草稿",
);
assert.strictEqual(
  state.currentViewLabel({ status: "ready", historical: false, frozen: true }),
  "已冻结",
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
assert.strictEqual(state.topKValid([{ code: "600001", rank: 1 }, { code: "600002", rank: 1 }]), false);
assert.strictEqual(state.topKValid([{ code: "600001", rank: 0 }]), false);

console.log("dashboard D4 state contract passed");
