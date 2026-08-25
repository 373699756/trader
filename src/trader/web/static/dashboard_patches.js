(function () {
  "use strict";

  function patchVersionValid(patch) {
    return Boolean(
      patch && patch.schema_version === "v2_event_v1" && patch.patch_schema_version === 2
    );
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
    const incomingProjection = patch.projection_version || "";
    if (!incomingProjection || patch.snapshot_id !== payload.snapshot_id
      || incomingProjection === currentVersion) {
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

  function eventMatchesCurrent(payload, strategy, tradeDate) {
    if (!payload || payload.strategy !== strategy) return false;
    return !tradeDate || payload.trade_date === tradeDate;
  }

  function emptyRecommendationMessage(payload, observationCount) {
    const observations = Number(observationCount);
    if (Number.isInteger(observations) && observations > 0) {
      return `本轮无正式推荐；${observations}只进入观察池，具体原因见下表`;
    }
    const diagnostics = payload && payload.selection_diagnostics || {};
    const maximum = Number(diagnostics.maximum_final_score);
    const floor = Number(diagnostics.observation_floor);
    const threshold = Number(diagnostics.executable_threshold);
    if (diagnostics.empty_reason === "score_below_observation_floor" && diagnostics.maximum_final_score != null
      && diagnostics.observation_floor != null && Number.isFinite(maximum) && Number.isFinite(floor)) {
      const formal = diagnostics.executable_threshold != null && Number.isFinite(threshold)
        ? `（正式门槛 ${threshold.toFixed(2)}）`
        : "";
      return `最高评分 ${maximum.toFixed(2)}，低于观察门槛 ${floor.toFixed(2)}${formal}，本轮无正式推荐和观察项`;
    }
    if (diagnostics.empty_reason === "no_scored_candidates") return "本轮没有可评分候选";
    if (diagnostics.empty_reason === "risk_or_execution_blocked") {
      const reasons = reasonCountSummary(diagnostics.blocked_reason_counts);
      return reasons
        ? `达到观察门槛的候选均不可执行：${reasons}`
        : "达到观察门槛的候选均不可执行，但快照未记录具体阻断原因";
    }
    if (diagnostics.empty_reason === "selection_limits") {
      const reasons = selectionLimitSummary(diagnostics.selection_skip_reason_counts);
      return reasons
        ? `候选达到门槛，但均受最终选择限制：${reasons}`
        : "候选达到门槛，但均受最终选择限制；快照未记录具体限制";
    }
    return "当前没有达到正式推荐条件的股票";
  }

  function frozenEmptyMessage(payload) {
    return payload && payload.phase === "close_fallback"
      ? "收盘补算未产生正式推荐；观察池已关闭且未保存"
      : "正式冻结结果为空；观察池已关闭且未保存";
  }

  function reasonCountSummary(values) {
    if (!values || typeof values !== "object") return "";
    return Object.entries(values)
      .filter(([reason, count]) => reason && Number.isInteger(count) && count > 0)
      .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
      .map(([reason, count]) => `${window.TraderRender.actionReason(reason)}（${count}只）`)
      .join("、");
  }

  function selectionLimitSummary(values) {
    const labels = {
      top_k_limit: "池容量已满",
      board_fraction_limit: "单板集中度上限",
      competition_group_limit: "竞争组集中度上限",
      industry_limit: "行业集中度上限",
    };
    if (!values || typeof values !== "object") return "";
    return Object.entries(values)
      .filter(([reason, count]) => reason && Number.isInteger(count) && count > 0)
      .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
      .map(([reason, count]) => `${labels[reason] || "其他选择限制"}（${count}只）`)
      .join("、");
  }

  function notReadyMessage(payload) {
    const strategy = payload && payload.strategy;
    const reason = payload && payload.readiness_reason;
    if (strategy === "long" || reason === "long_snapshot_not_ready") {
      return {
        message: "长期策略当前尚无可用数据",
        notice: "长期策略只展示当前研究快照",
      };
    }
    if (reason === "today_freeze_missed") {
      return {
        message: "11:20 前未形成正式快照",
        notice: "按冻结规则今日不补算，当前无推荐",
      };
    }
    if (reason === "afternoon_freeze_pending") {
      return {
        message: "14:50 正式快照尚未形成",
        notice: "冻结流程尚未完成；不会展示上一交易日结果",
      };
    }
    if (reason === "afternoon_close_recovery_pending") {
      return {
        message: "14:50 正式快照缺失",
        notice: "正在等待允许的收盘恢复；不会展示上一交易日结果",
      };
    }
    if (reason === "snapshot_not_published") {
      const label = ({ today: "今早", tomorrow: "明日", d25: "2-5日" })[strategy] || "当前";
      return {
        message: `${label}策略当前快照尚未发布`,
        notice: "当前策略快照尚未形成，等待本地评分发布",
      };
    }
    return {
      message: "当前暂无可用荐股数据",
      notice: "快照未提供未就绪原因，请检查运行状态",
    };
  }

  function snapshotNotice(payload) {
    if (!payload || payload.status === "not_ready") {
      const notReady = notReadyMessage(payload || {});
      return { message: notReady.notice, level: "idle" };
    }
    const reasons = Array.isArray(payload.degraded_reasons) ? payload.degraded_reasons : [];
    const degraded = reasons.length ? reasonLabels(reasons, payload.frozen === true) : "";
    if (payload.historical === true) {
      const kind = payload.phase === "close_fallback"
        ? "收盘补算结果（仅本地评分）"
        : "名单与评分为当日冻结结果";
      return {
        level: degraded ? "warning" : "ok",
        message: `历史快照 · ${kind} · 行情按最新可用报价展示${degraded ? ` · 当日固化时降级：${degraded}` : ""}`,
      };
    }
    if (payload.strategy === "today" && payload.frozen === true
      && payload.historical !== true && payload.phase !== "close_fallback") {
      const quote = payload.stale
        ? "行情已过期，当前报价仅供观察"
        : "行情按最新可用报价展示";
      return {
        level: payload.stale || degraded ? "warning" : "ok",
        message: `11:20 已冻结 · 名单与评分不变 · ${quote}${degraded ? ` · 冻结时降级：${degraded}` : ""}`,
      };
    }
    if (payload.phase === "close_fallback") {
      return {
        level: degraded ? "warning" : "ok",
        message: `已冻结 · 收盘补算 · 仅本地评分 · ${window.TraderRender.formatTime(payload.published_at)}${degraded ? ` · 固化时降级：${degraded}` : ""}`,
      };
    }
    if (payload.stale) {
      return {
        level: "warning",
        message: `行情已过期，当前结果仅供观察${degraded ? ` · 降级：${degraded}` : ""}`,
      };
    }
    if (payload.frozen) {
      const anchor = ["tomorrow", "d25"].includes(payload.strategy) ? "14:50 已冻结" : "已冻结";
      return {
        level: degraded ? "warning" : "ok",
        message: `${anchor} · 名单与评分不变 · 行情按最新可用报价展示${degraded ? ` · 冻结时降级：${degraded}` : ""}`,
      };
    }
    if (payload.strategy === "long") {
      return {
        level: degraded ? "warning" : "ok",
        message: `长期实时数据 · 不评分、不冻结 · ${window.TraderRender.formatTime(payload.published_at)}${degraded ? ` · 降级：${degraded}` : ""}`,
      };
    }
    if (payload.view === "live") {
      return {
        level: "warning",
        message: `实时快照 · ${window.TraderRender.formatTime(payload.published_at)} · 未冻结，名单与评分可能变化${degraded ? ` · 降级：${degraded}` : ""}`,
      };
    }
    return {
      level: degraded ? "warning" : "ok",
      message: `快照 ${window.TraderRender.formatTime(payload.published_at)} · ${window.TraderRender.fusionModeLabel(payload.fusion_mode)}${degraded ? ` · 降级：${degraded}` : ""}`,
    };
  }

  function reasonLabels(values, frozen) {
    return [...new Set(values.map((value) => frozen ? frozenReasonLabel(value) : window.TraderRender.reasonLabel(value)))].join("、");
  }

  function frozenReasonLabel(value) {
    const reason = String(value || "");
    if (reason === "deepseek_pending") return "模型复核未在冻结前完成（已按本地评分固化）";
    if (reason === "deepseek_incomplete") return "模型复核未在冻结前全部完成（已按可用结果固化）";
    if (reason === "deepseek_deferred_until_afternoon") {
      return "冻结前未安排模型复核（已按本地评分固化）";
    }
    if (reason === "deepseek_skipped_no_eligible_candidates") return "冻结时无符合模型复核条件的候选";
    if (reason === "model_unavailable") return "冻结时模型服务不可用（已按本地评分固化）";
    const separator = reason.indexOf(":");
    if (separator > 0 && reason.slice(separator + 1) === "board_data_reliability_below_threshold") {
      const board = ({ main: "主板", chinext: "创业板", star: "科创板" })[reason.slice(0, separator)];
      if (board) return `${board}板块数据可靠度不足`;
    }
    return window.TraderRender.reasonLabel(reason);
  }

  function mergePatchItems(existingItems, upserts, removed) {
    const byCode = new Map((existingItems || []).map((item) => [item.code, item]));
    for (const code of removed) byCode.delete(code);
    for (const item of upserts) {
      if (item && item.code) byCode.set(item.code, item);
    }
    return Array.from(byCode.values()).sort((left, right) => {
      const actionDifference = actionOrder(left && left.action) - actionOrder(right && right.action);
      if (actionDifference !== 0) return actionDifference;
      const leftRank = Number(left.rank);
      const rightRank = Number(right.rank);
      if (Number.isFinite(leftRank) && Number.isFinite(rightRank) && leftRank !== rightRank) return leftRank - rightRank;
      return String(left.code || "").localeCompare(String(right.code || ""));
    });
  }

  function actionOrder(action) {
    return ({ executable: 0, observe: 1, unavailable: 2 })[String(action || "")] ?? 0;
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
    if (!Array.isArray(items) || (strategy !== "long" && items.length > 12)) return false;
    const codes = items.map((item) => item && item.code);
    const ranks = items.map((item) => Number(item && item.rank));
    const executableCount = items.filter((item) => item && item.action === "executable").length;
    const observationCount = items.filter((item) => item && item.action === "observe").length;
    const unsupportedAction = items.some(
      (item) => item && item.action && !["executable", "observe"].includes(item.action),
    );
    const poolRanks = items.map((item, index) => `${actionOrder(item && item.action)}:${ranks[index]}`);
    return codes.every((code) => typeof code === "string" && code)
      && new Set(codes).size === codes.length
      && ranks.every((rank) => Number.isInteger(rank) && rank > 0)
      && (strategy === "long" ? new Set(ranks).size === ranks.length : new Set(poolRanks).size === poolRanks.length)
      && (strategy === "long" || executableCount <= 6 && observationCount <= 6 && !unsupportedAction);
  }

  window.TraderDashboardPatches = Object.freeze({
    emptyRecommendationMessage,
    frozenEmptyMessage,
    mergePatchItems,
    notReadyMessage,
    overlayPatchDecision,
    eventMatchesCurrent,
    patchVersionValid,
    projectionVersion,
    recommendationPatchDecision,
    snapshotNotice,
    topKValid,
  });
})();
