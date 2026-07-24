(function () {
  "use strict";

  const staticData = window.TraderLongWatchlistData || { items: [], groups: [] };
  const categories = ["chokepoint", "future_growth", "low_price_potential"];
  const scopeLabels = Object.freeze({
    chokepoint: "卡脖子行业",
    future_growth: "高成长赛道",
    low_price_potential: "低价潜力股",
  });

  function scopeLabel(scope) {
    return scopeLabels[scope] || scopeLabels.chokepoint;
  }

  function displayPayload(payload) {
    if (!payload || payload.strategy !== "long" || !Array.isArray(staticData.items) || staticData.items.length === 0) {
      return payload;
    }
    const liveByCode = new Map((Array.isArray(payload.items) ? payload.items : []).map((item) => [item.code, item]));
    const items = staticData.items.map((item, index) => {
      const live = liveByCode.get(item.code) || {};
      return {
        rank: index + 1,
        code: item.code,
        name: item.name,
        industry: item.industry,
        price: null,
        pct_change: null,
        turnover_rate: null,
        amount: null,
        market_cap: null,
        source: "long_watchlist",
        source_time: null,
        action: "observe",
        action_reason: "fixed_long_watchlist",
        setup_type: "none",
        downside: null,
        scores: { local_score: 0, deepseek_score: null, deepseek_risk_penalty: 0, final_score: 0 },
        risks: [],
        review: null,
        ...live,
        name: live.name || item.name,
        industry: live.industry || item.industry,
      };
    });
    return {
      ...payload,
      status: "ready",
      snapshot_id: payload.snapshot_id || `long-watchlist:${staticData.watchlist_version || "static"}`,
      trade_date: payload.trade_date || payload.current_trade_date || "",
      phase: payload.phase || "current",
      published_at: payload.published_at || new Date().toISOString(),
      strategy_version: payload.strategy_version || staticData.watchlist_version || "long_watchlist_static",
      fusion_mode: payload.fusion_mode || "local_degraded",
      stale: payload.stale !== false,
      long_groups: Array.isArray(payload.long_groups) && payload.long_groups.length ? payload.long_groups : staticData.groups,
      items,
    };
  }

  function normalized(payload, category) {
    if (!payload || payload.strategy !== "long" || !Array.isArray(payload.long_groups)) return [];
    return payload.long_groups
      .filter((group) => group && group.category === category && typeof group.name === "string")
      .map((group) => ({
        name: group.name,
        category: group.category,
        codes: Array.isArray(group.codes) ? group.codes.filter((code) => typeof code === "string" && code) : [],
        count: Number.isInteger(group.count) ? group.count : 0,
      }))
      .filter((group) => group.codes.length > 0);
  }

  function renderBar(els, state, payload) {
    if (!els.longGroupBar) return;
    const isLong = payload && payload.strategy === "long";
    els.longGroupBar.hidden = !isLong;
    if (els.longStockHeader) els.longStockHeader.hidden = !isLong;
    if (!isLong) return;
    const scope = categories.includes(state.longScope) ? state.longScope : "chokepoint";
    state.longScope = scope;
    if (els.longPanelTitle) els.longPanelTitle.textContent = scopeLabel(scope);
    els.longScopeTabs.querySelectorAll("button[data-scope]").forEach((button) => {
      const active = button.dataset.scope === scope;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    const scopedGroups = normalized(payload, scope);
    if (!scopedGroups.some((group) => group.name === state.longGroup)) {
      state.longGroup = scopedGroups[0] ? scopedGroups[0].name : "";
    }
    els.longIndustryTabs.innerHTML = scopedGroups.map((group) => industryButton(group, state.longGroup)).join("");
    if (els.longPanelMeta) els.longPanelMeta.textContent = `${scopedGroups.length} 个分组`;
    if (els.longStockContext) els.longStockContext.textContent = state.longGroup || scopeLabel(scope);
  }

  function industryButton(group, activeGroup) {
    const active = group.name === activeGroup;
    const name = window.TraderRender.escapeHtml(group.name);
    return `<button class="long-industry-tab${active ? " is-active" : ""}" type="button" role="tab" aria-selected="${active ? "true" : "false"}" data-group="${name}"><span>${name}</span><b>${group.codes.length} 只</b></button>`;
  }

  function visibleRecommendations(payload, recommendations, scope, groupName) {
    if (!payload || payload.strategy !== "long") return recommendations;
    const category = categories.includes(scope) ? scope : "chokepoint";
    const groups = normalized(payload, category);
    const group = groups.find((candidate) => candidate.name === groupName) || groups[0];
    if (!group) return [];
    const byCode = new Map(recommendations.map((item) => [item.code, item]));
    return group.codes
      .map((code) => byCode.get(code))
      .filter(Boolean)
      .map((item, index) => ({ ...item, rank: index + 1 }));
  }

  function emptyMessage(payload, scope) {
    if (payload && payload.strategy === "long" && scope === "low_price_potential") {
      return "低价潜力股暂无可展示股票";
    }
    if (payload && payload.strategy === "long" && scope === "future_growth") {
      return "高成长赛道暂无可展示股票";
    }
    return "当前长期分组暂无可展示股票";
  }

  window.TraderLongGroups = Object.freeze({
    displayPayload,
    emptyMessage,
    normalized,
    renderBar,
    scopeLabel,
    visibleRecommendations,
  });
})();
