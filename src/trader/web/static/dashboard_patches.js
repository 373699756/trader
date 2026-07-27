(function () {
  "use strict";

  function patchVersionValid(patch) {
    return Boolean(patch && patch.patch_schema_version === 2 && patch.schema_version === 2);
  }

  function recommendationPatchDecision(patch, payload, currentVersion, strategy, view) {
    if (!patchVersionValid(patch) || !Array.isArray(patch.upserts)
      || !Array.isArray(patch.removed_codes) || !Array.isArray(patch.removals || [])) return "schema_mismatch";
    if (!patch.projection_version || patch.snapshot_id !== patch.projection_version
      || patch.strategy !== strategy || !["live", "official"].includes(patch.view)
      || patch.view !== (patch.frozen ? "official" : "live")) return "identity_mismatch";
    const expectedDate = payload && (payload.current_trade_date || payload.trade_date);
    if (expectedDate && patch.trade_date !== expectedDate) return "identity_mismatch";
    if (["current", "official"].includes(view) && payload && payload.frozen === true && patch.frozen !== true) {
      return "ignore_late_draft";
    }
    const baseVersion = patch.base_projection_version || patch.base_snapshot_id || "";
    if (baseVersion && baseVersion !== currentVersion) return "base_mismatch";
    if (!baseVersion && patch.replace !== true && payload && payload.status !== "not_ready") return "base_mismatch";
    if (!patchItemsValid(patch.upserts, patch.removed_codes, patch.removals || [])) return "topk_mismatch";
    return "apply";
  }

  function overlayPatchDecision(patch, payload, currentVersion, strategy) {
    if (!patchVersionValid(patch) || !Array.isArray(patch.quotes)) return "schema_mismatch";
    if (!payload || patch.strategy !== strategy || patch.trade_date !== payload.trade_date) return "identity_mismatch";
    const incomingProjection = patch.projection_version || patch.snapshot_id || "";
    if (!incomingProjection || incomingProjection !== currentVersion || patch.snapshot_id !== payload.snapshot_id) {
      return "overlay_projection_mismatch";
    }
    if (!patch.quotes.every((quote) => quote && typeof quote.code === "string" && quote.code)) {
      return "schema_mismatch";
    }
    return "apply";
  }

  function projectionVersion(payload) {
    if (!payload) return "";
    return payload.projection_version || payload.snapshot_id || "";
  }

  function emptyRecommendationMessage(payload) {
    const diagnostics = payload && payload.selection_diagnostics || {};
    const maximum = Number(diagnostics.maximum_final_score), floor = Number(diagnostics.selection_floor);
    if (diagnostics.empty_reason === "score_below_observation_floor" && diagnostics.maximum_final_score != null
      && diagnostics.selection_floor != null && Number.isFinite(maximum) && Number.isFinite(floor)) {
      return `最高评分 ${maximum.toFixed(2)}，低于观察门槛 ${floor.toFixed(2)}，本轮不荐股`;
    }
    if (diagnostics.empty_reason === "no_scored_candidates") return "本轮没有可评分候选";
    if (diagnostics.empty_reason === "risk_or_execution_blocked") return "候选达到评分门槛，但被风险或执行条件拦截";
    if (diagnostics.empty_reason === "selection_limits") return "候选达到门槛，但未通过最终集中度限制";
    return "当前没有达到正式推荐条件的股票";
  }

  function notReadyMessage(strategy) {
    if (strategy === "long") {
      return {
        message: "长期策略当前尚无可用数据",
        notice: "长期策略只展示当前研究快照",
      };
    }
    return {
      message: "当前暂无可用荐股数据",
      notice: "等待策略数据更新",
    };
  }

  function mergePatchItems(existingItems, upserts, removed) {
    const byCode = new Map((existingItems || []).map((item) => [item.code, item]));
    for (const code of removed) byCode.delete(code);
    for (const item of upserts) {
      if (item && item.code) byCode.set(item.code, item);
    }
    return Array.from(byCode.values()).sort((left, right) => {
      const leftRank = Number(left.rank);
      const rightRank = Number(right.rank);
      if (Number.isFinite(leftRank) && Number.isFinite(rightRank) && leftRank !== rightRank) return leftRank - rightRank;
      return String(left.code || "").localeCompare(String(right.code || ""));
    });
  }

  function patchItemsValid(upserts, removedCodes, removals) {
    const codes = upserts.map((item) => item && item.code);
    const removed = [...removedCodes, ...removals];
    return codes.every((code) => typeof code === "string" && code)
      && removed.every((code) => typeof code === "string" && code)
      && new Set(codes).size === codes.length
      && !codes.some((code) => removed.includes(code));
  }

  function topKValid(items, strategy) {
    if (!Array.isArray(items) || (strategy !== "long" && items.length > 18)) return false;
    const codes = items.map((item) => item && item.code);
    const ranks = items.map((item) => Number(item && item.rank));
    return codes.every((code) => typeof code === "string" && code)
      && new Set(codes).size === codes.length
      && ranks.every((rank) => Number.isInteger(rank) && rank > 0)
      && new Set(ranks).size === ranks.length;
  }

  window.TraderDashboardPatches = Object.freeze({
    emptyRecommendationMessage,
    mergePatchItems,
    notReadyMessage,
    overlayPatchDecision,
    patchVersionValid,
    projectionVersion,
    recommendationPatchDecision,
    topKValid,
  });
})();
