(function () {
  "use strict";

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
    if (!isLong) return;
    const scope = state.longScope === "low_price_potential" ? "low_price_potential" : "chokepoint";
    state.longScope = scope;
    els.longScopeTabs.querySelectorAll("button[data-scope]").forEach((button) => {
      const active = button.dataset.scope === scope;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    const industryGroups = normalized(payload, "chokepoint");
    if (scope !== "chokepoint") {
      state.longGroup = "";
      els.longIndustryTabs.innerHTML = "";
      return;
    }
    if (!industryGroups.some((group) => group.name === state.longGroup)) {
      state.longGroup = industryGroups[0] ? industryGroups[0].name : "";
    }
    els.longIndustryTabs.innerHTML = industryGroups.map((group) => industryButton(group, state.longGroup)).join("");
  }

  function industryButton(group, activeGroup) {
    const active = group.name === activeGroup;
    const name = window.TraderRender.escapeHtml(group.name);
    return `<button class="long-industry-tab${active ? " is-active" : ""}" type="button" role="tab" aria-selected="${active ? "true" : "false"}" data-group="${name}">${name}</button>`;
  }

  function visibleRecommendations(payload, recommendations, scope, groupName) {
    if (!payload || payload.strategy !== "long") return recommendations;
    const category = scope === "low_price_potential" ? "low_price_potential" : "chokepoint";
    const groups = normalized(payload, category);
    const group = category === "low_price_potential"
      ? groups[0]
      : groups.find((candidate) => candidate.name === groupName) || groups[0];
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
    return "当前长期分组暂无可展示股票";
  }

  window.TraderLongGroups = Object.freeze({
    emptyMessage,
    normalized,
    renderBar,
    visibleRecommendations,
  });
})();
