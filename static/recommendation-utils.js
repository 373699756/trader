window.TraderRecommendationUtils = {
  filterAndSortRows(rows, options) {
    const actionFilter = options.actionFilter || "all";
    const sortMode = options.sortMode || "rank";
    const filtered = (rows || []).filter(row => this.rowMatchesAction(row, actionFilter));
    return [...filtered].sort((left, right) => this.compareRows(left, right, sortMode));
  },

  rowMatchesAction(row, actionFilter) {
    if (actionFilter === "all") {
      return true;
    }
    const label = this.rowActionLabel(row);
    if (actionFilter === "priority") {
      return this.isPriorityAction(label);
    }
    if (actionFilter === "watch") {
      return label.includes("观察") && !label.includes("只观察");
    }
    if (actionFilter === "wait") {
      return label.includes("等待");
    }
    if (actionFilter === "observe") {
      return label.includes("只观察");
    }
    if (actionFilter === "immediate") {
      const strategyName = String(row?.strategy_name || row?.strategy || "").trim();
      const isTodayTerm = strategyName === "today_term";
      return isTodayTerm
        ? (row?.execution_window_status === "immediate" || this.isExecutableActionRow(row))
        : this.isExecutableActionRow(row);
    }
    if (actionFilter === "backup_only") {
      const strategyName = String(row?.strategy_name || row?.strategy || "").trim();
      const isTodayTerm = strategyName === "today_term";
      if (isTodayTerm) {
        return row?.execution_window_status === "backup_only" || row?.execution_allowed === false;
      }
      return row?.execution_allowed === false;
    }
    return true;
  },

  compareRows(left, right, sortMode) {
    if (sortMode === "quality") {
      return this.rowQuality(right) - this.rowQuality(left);
    }
    if (sortMode === "risk") {
      return this.rowRisk(left) - this.rowRisk(right);
    }
    if (sortMode === "score") {
      return this.rowScore(right) - this.rowScore(left);
    }
    if (sortMode === "turnover") {
      return Number(right.turnover || 0) - Number(left.turnover || 0);
    }
    return Number(left.rank || 999) - Number(right.rank || 999);
  },

  rowActionLabel(row) {
    return String(row.action_label || row.serenity_profile?.action_label || "");
  },

  isPriorityAction(label) {
    return String(label || "").includes("优先");
  },

  isExecutableActionRow(row) {
    if (row?.execution_allowed === false) return false;
    const positionSize = Number(row?.trade_action?.position_size);
    if (Number.isFinite(positionSize)) {
      return positionSize > 0;
    }
    return true;
  },

  rowQuality(row) {
    return Number(row.serenity_profile?.quality_score ?? row.score ?? 0);
  },

  rowRisk(row) {
    return Number(row.serenity_profile?.risk_score ?? 999);
  },

  rowScore(row) {
    return Number(row.score ?? 0);
  },

  rowDisplayQuality(row) {
    return Number(row.decision_score ?? row.serenity_profile?.quality_score ?? row.score ?? 0);
  },
};
