(function () {
  "use strict";

  const descriptions = Object.freeze({
    today: "盘中短线 · 面向 T+1 · 11:20 冻结",
    tomorrow: "尾盘策略 · 面向 T+1 · 14:50 冻结",
    d25: "尾盘策略 · 面向 T+2 至 T+5 · 14:50 冻结",
    long: "长期研究 · 仅展示当前数据",
  });

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

  function renderDateOptions(select, strategy, availableDates, selectedDate, availability) {
    select.innerHTML = "";
    appendOption(select, "", "当前");
    if (selectedDate && availability !== "available") {
      appendOption(
        select,
        selectedDate,
        availability === "missing" ? `${selectedDate}（无数据）` : selectedDate,
      );
    }
    for (const value of availableDates || []) {
      if (value !== selectedDate || availability === "available") appendOption(select, value, value);
    }
    select.value = selectedDate;
    select.disabled = strategy === "long";
  }

  function markDateAvailability(select, selectedDate, availability) {
    const option = Array.from(select.options).find((candidate) => candidate.value === selectedDate);
    if (!option) return;
    option.textContent = availability === "missing" ? `${selectedDate}（无数据）` : selectedDate;
  }

  function visibleRecommendations(payload) {
    const items = payload && Array.isArray(payload.items) ? payload.items : [];
    if (payload && payload.strategy === "long") return items;
    return items.filter((item) => item.action === "executable");
  }

  function observationRecommendations(payload, runtimePhase) {
    const items = payload && Array.isArray(payload.items) ? payload.items : [];
    if (observationDisplayState(payload, runtimePhase) !== "open") return [];
    return items.filter((item) => item.action === "observe");
  }

  function observationDisplayState(payload, runtimePhase) {
    if (!payload) return "unavailable";
    if (payload.strategy === "long") return "not_applicable";
    if (payload.historical === true) return "hidden_history";
    if (payload.status !== "ready") return "unavailable";
    if (payload.frozen === true) return "closed_frozen";
    if (typeof runtimePhase !== "string" || !runtimePhase) return "unknown";
    const morningPhases = new Set([
      "today_observe",
      "today_main",
      "today_late",
    ]);
    if (payload.strategy === "today") return morningPhases.has(runtimePhase) ? "open" : "closed_market";
    const afternoonPhases = new Set([
      "midday",
      "afternoon",
      "final_review",
      "deepseek_cutoff",
      "final_quote",
    ]);
    return morningPhases.has(runtimePhase) || afternoonPhases.has(runtimePhase) ? "open" : "closed_market";
  }

  function recommendationSummary(payload, recommendations) {
    const scoringApplicable = !payload || payload.score_status !== "not_applicable";
    const scores = recommendations
      .map((item) => item && item.scores ? item.scores.final_score : null)
      .filter((value) => typeof value === "number" && Number.isFinite(value));
    const reviewed = recommendations.filter((item) => item && item.review && item.review.outcome).length;
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
    return ({ today: "今早", tomorrow: "明日", d25: "2-5日", long: "长期" })[strategy] || strategy;
  }

  function appendOption(select, value, text) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    select.append(option);
  }

  window.TraderSelection = Object.freeze({
    descriptions,
    isSnapshotNotFound,
    markDateAvailability,
    observationRecommendations,
    observationDisplayState,
    recommendationSummary,
    renderDateOptions,
    resolveStrategyDate,
    strategyLabel,
    visibleRecommendations,
  });
})();
