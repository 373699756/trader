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
    if (payload && (payload.historical === true || payload.strategy === "long" || payload.phase === "close_fallback")) {
      return items;
    }
    return items.filter((item) => item.action === "executable");
  }

  function recommendationSummary(payload, recommendations) {
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
      topScore: scores.length ? Math.max(...scores).toFixed(2) : "-",
      modelReview: recommendations.length ? `${reviewed} / ${recommendations.length}` : "-",
      dataQuality,
      dataQualityTitle: degradedReasons.join("、"),
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
    recommendationSummary,
    renderDateOptions,
    resolveStrategyDate,
    strategyLabel,
    visibleRecommendations,
  });
})();
