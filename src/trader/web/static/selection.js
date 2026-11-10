(function () {
  "use strict";

  const descriptions = Object.freeze({
    tomorrow: "尾盘策略 · 面向 T+1 · 15:00 冻结",
    d25: "尾盘策略 · 面向 T+2 至 T+5 · 15:00 冻结",
    long: "长期研究 · 名单资格待核实\n· 仅展示当前数据",
  });

  function currentViewMatches(strategy, payloadView) {
    return strategy === "long"
      ? payloadView === "current"
      : ["current", "live", "official"].includes(payloadView);
  }

  function resolveStrategyDate(previousStrategy, nextStrategy, selectedDate, availableDates) {
    if (nextStrategy === "long" || previousStrategy === "long" || !selectedDate) {
      return { date: "", availability: "available" };
    }
    if (availableDates === null) return { date: selectedDate, availability: "unknown" };
    return {
      date: selectedDate,
      availability: availableDates.includes(selectedDate) ? "available" : "missing",
    };
  }

  function calendarCells(year, month, availableDates, selectedDate) {
    const firstWeekday = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
    const dayCount = new Date(Date.UTC(year, month, 0)).getUTCDate();
    const cellCount = Math.ceil((firstWeekday + dayCount) / 7) * 7;
    const datesWithFreezes = new Set(availableDates || []);
    return Array.from({ length: cellCount }, (_unused, index) => {
      const day = index - firstWeekday + 1;
      if (day < 1 || day > dayCount) return { date: "", day: "", hasFreeze: false, selected: false };
      const date = `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
      return {
        date,
        day: String(day),
        hasFreeze: datesWithFreezes.has(date),
        selected: date === selectedDate,
      };
    });
  }

  function visibleRecommendations(payload) {
    const items = payload && payload.status === "not_ready" && payload.draft && Array.isArray(payload.draft.items)
      ? payload.draft.items
      : payload && Array.isArray(payload.items) ? payload.items : [];
    return items;
  }

  function observationRecommendations(payload, runtimePhase, statusPayload) {
    const items = payload && payload.status === "not_ready"
      ? payload.draft && Array.isArray(payload.draft.items) ? payload.draft.items : []
      : payload && Array.isArray(payload.items) ? payload.items : [];
    if (observationDisplayState(payload, runtimePhase, statusPayload) !== "open") return [];
    return items.filter((item) => item.action === "observe");
  }

  function detailItem(payload, code) {
    if (!payload || !code) return null;
    const draft = payload.draft || {};
    const candidates = [payload.items, payload.top_scores, draft.items, draft.top_scores];
    for (const items of candidates) {
      if (!Array.isArray(items)) continue;
      const item = items.find((candidate) => candidate && candidate.code === code);
      if (item) return item;
    }
    return null;
  }

  function observationDisplayState(payload, runtimePhase, statusPayload) {
    if (!payload) return "unavailable";
    if (payload.strategy === "long") return "not_applicable";
    if (payload.historical === true) return "hidden_history";
    if (payload.frozen === true) return "closed_frozen";
    if (typeof runtimePhase !== "string" || !runtimePhase) return "unknown";
    const isBeforeClose = runtimePhase === "before_close";
    if (!isBeforeClose) return "closed_market";
    if (payload.status === "not_ready") {
      if (payload.draft) {
        const draftItems = Array.isArray(payload.draft.items) ? payload.draft.items : [];
        return draftItems.some((item) => item.action === "observe") ? "open" : "empty";
      }
      return strategyLaneRunning(statusPayload, payload.strategy) ? "warming" : "unavailable";
    }
    if (payload.status !== "ready") return "unavailable";
    const items = Array.isArray(payload.items) ? payload.items : [];
    return items.some((item) => item.action === "observe") ? "open" : "empty";
  }

  function observationEmptyMessage(displayState) {
    if (displayState === "empty") return "本轮无股票达到观察条件";
    if (displayState === "warming") return "正在生成观察草稿";
    return "本轮尚无可用观察草稿，请查看运行状态";
  }

  function strategyLaneRunning(statusPayload, strategy) {
    const scheduler = statusPayload && statusPayload.scheduler;
    const lanes = scheduler && Array.isArray(scheduler.lanes) ? scheduler.lanes : [];
    const lane = lanes.find((candidate) => {
      if (!candidate || typeof candidate !== "object") return false;
      if (candidate.strategy === strategy) return true;
      return typeof candidate.name === "string" && candidate.name.endsWith(`-${strategy}`);
    });
    return Boolean(lane && (lane.running === true || lane.pending === true));
  }

  function recommendationSummary(payload, recommendations) {
    const scoringApplicable = !payload || payload.score_status !== "not_applicable";
    const scores = recommendations
      .map((item) => item && item.scores ? item.scores.final_score : null)
      .filter((value) => typeof value === "number" && Number.isFinite(value));
    const reviewed = recommendations.filter((item) => item && item.review_outcome).length;
    const degradedReasons = payload && Array.isArray(payload.degraded_reasons) ? payload.degraded_reasons : [];
    const dataQuality = payload && payload.status === "not_ready"
      ? "无数据"
      : payload && payload.stale
        ? "行情过期"
        : degradedReasons.length
          ? `降级 · ${degradedReasons.length}项`
          : "正常";
    return {
      topScore: scoringApplicable && scores.length ? Math.max(...scores).toFixed(2) : "-",
      modelReview: scoringApplicable && recommendations.length ? `${reviewed} / ${recommendations.length}` : "-",
      dataQuality,
      dataQualityTitle: window.TraderRender.reasonLabels(degradedReasons).join("、"),
    };
  }

  function isSnapshotNotFound(error) {
    return Boolean(error && error.code === "snapshot_not_found");
  }

  function strategyLabel(strategy) {
    return ({ tomorrow: "明天", d25: "D25", long: "长期" })[strategy] || strategy;
  }

  window.TraderSelection = Object.freeze({
    calendarCells,
    currentViewMatches,
    detailItem,
    descriptions,
    isSnapshotNotFound,
    observationRecommendations,
    observationDisplayState,
    observationEmptyMessage,
    recommendationSummary,
    resolveStrategyDate,
    strategyLabel,
    visibleRecommendations,
  });
})();
