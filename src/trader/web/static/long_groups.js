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

  function staticFallbackPayload(reason) {
    return displayPayload({
      schema_version: "v2_decision_view_v1",
      status: "not_ready",
      strategy: "long",
      trade_date: null,
      current_trade_date: null,
      historical: false,
      view: "current",
      score_status: "not_applicable",
      decision_version: null,
      published_at: null,
      phase: "current",
      frozen: false,
      degraded_reasons: [reason || "long_current_unavailable"],
      items: [],
    });
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
        quote_status: "missing",
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
      published_at: payload.published_at || null,
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
      .map((group) => {
        const sourceSection = sourceSectionKey(group.source_section);
        return {
          key: `${sourceSection}:${group.name}`,
          name: group.name,
          category: group.category,
          source_section: sourceSection,
          sections: normalizedSections(group, sourceSection),
          codes: Array.isArray(group.codes) ? group.codes.filter((code) => typeof code === "string" && code) : [],
          count: Number.isInteger(group.count) ? group.count : 0,
        };
      })
      .filter((group) => group.codes.length > 0);
  }

  function renderBar(els, state, payload) {
    if (!els.longSidebar) return;
    const isLong = payload && payload.strategy === "long";
    els.longSidebar.hidden = !isLong;
    if (els.longStockHeader) els.longStockHeader.hidden = !isLong;
    if (!isLong) return;
    const scope = categories.includes(state.longScope) ? state.longScope : "chokepoint";
    state.longScope = scope;
    if (els.longTitle) els.longTitle.textContent = scopeLabel(scope);
    els.longScopeTabs.querySelectorAll("button[data-scope]").forEach((button) => {
      const active = button.dataset.scope === scope;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    const scopedGroups = normalized(payload, scope);
    if (!scopedGroups.some((group) => group.key === state.longGroup || group.name === state.longGroup)) {
      state.longGroup = scopedGroups[0] ? scopedGroups[0].key : "";
    } else if (scopedGroups.some((group) => group.name === state.longGroup)) {
      const namedGroup = scopedGroups.find((group) => group.name === state.longGroup);
      state.longGroup = namedGroup ? namedGroup.key : state.longGroup;
    }
    els.longIndustryTabs.innerHTML = scopedGroups
      .map((group) => industryButton(group, state.longGroup, payload.items))
      .join("");
    if (els.longMeta) els.longMeta.textContent = `${scopedGroups.length} 个分组`;
    const activeGroup = scopedGroups.find((group) => group.key === state.longGroup);
    if (els.longStockContext) els.longStockContext.textContent = activeGroup ? activeGroup.name : scopeLabel(scope);
  }

  function groupAveragePct(group, items) {
    const codes = Array.isArray(group && group.codes) ? group.codes : [];
    const byCode = new Map(
      (Array.isArray(items) ? items : [])
        .filter((item) => item && typeof item.code === "string")
        .map((item) => [item.code, item]),
    );
    let sum = 0;
    let validCount = 0;
    codes.forEach((code) => {
      const item = byCode.get(code);
      const value = item && item.pct_change;
      if (value === null || value === undefined || (typeof value === "string" && value.trim() === "")) return;
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return;
      sum += parsed;
      validCount += 1;
    });
    return {
      average: validCount > 0 ? sum / validCount : null,
      validCount,
      totalCount: codes.length,
    };
  }

  function industryButton(group, activeGroup, items) {
    const active = group.key === activeGroup;
    const name = window.TraderRender.escapeHtml(group.name);
    const key = window.TraderRender.escapeHtml(group.key);
    const average = groupAveragePct(group, items);
    const change = average.average === null
      ? { text: "--", className: "is-unavailable" }
      : window.TraderRender.pct(average.average);
    const changeText = window.TraderRender.escapeHtml(change.text);
    const averageClass = `long-industry-average${change.className ? ` ${change.className}` : ""}`;
    const description = window.TraderRender.escapeHtml(
      `${group.name}，平均涨跌幅 ${change.text}，有效行情 ${average.validCount}/${average.totalCount} 只`,
    );
    const className = `long-industry-tab${active ? " is-active" : ""}`;
    return `<button class="${className}" type="button" role="tab" aria-label="${description}" title="${description}" aria-selected="${active ? "true" : "false"}" data-group="${key}"><span class="long-industry-label"><span class="long-industry-name">${name}</span><em class="${averageClass}">${changeText}</em></span><b>${group.codes.length} 只</b></button>`;
  }

  function visibleRecommendations(payload, recommendations, scope, groupName) {
    if (!payload || payload.strategy !== "long") return recommendations;
    const category = categories.includes(scope) ? scope : "chokepoint";
    const groups = normalized(payload, category);
    const group =
      groups.find((candidate) => candidate.key === groupName) ||
      groups.find((candidate) => candidate.name === groupName) ||
      groups[0];
    if (!group) return [];
    const byCode = new Map(recommendations.map((item) => [item.code, item]));
    const sectionByCode = groupSectionByCode(group);
    const sectioned = group.sections.length > 1;
    let previousSection = "";
    return group.codes
      .map((code) => {
        const item = byCode.get(code);
        if (!item) return null;
        if (!sectioned) return item;
        const section = sectionByCode.get(code) || group.source_section;
        const divider = Boolean(previousSection && section !== previousSection);
        previousSection = section;
        return { ...item, long_section: section, long_section_divider: divider };
      })
      .filter(Boolean)
      .map((item, index) => ({ ...item, rank: index + 1 }));
  }

  function normalizedSections(group, fallbackSection) {
    const sections = Array.isArray(group.sections) ? group.sections : [];
    const normalized = sections
      .map((section) => ({
        source_section: sourceSectionKey(section && section.source_section),
        codes: Array.isArray(section && section.codes)
          ? section.codes.filter((code) => typeof code === "string" && code)
          : [],
      }))
      .filter((section) => section.codes.length > 0);
    return normalized.length ? normalized : [{ source_section: fallbackSection, codes: [] }];
  }

  function groupSectionByCode(group) {
    const byCode = new Map();
    group.sections.forEach((section) => {
      section.codes.forEach((code) => byCode.set(code, section.source_section));
    });
    return byCode;
  }

  function sourceSectionKey(value) {
    return value === "document_scan" ? "document_scan" : "current_leaders";
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
    groupAveragePct,
    normalized,
    renderBar,
    scopeLabel,
    staticFallbackPayload,
    visibleRecommendations,
  });
})();
