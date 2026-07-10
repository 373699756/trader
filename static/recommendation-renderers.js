window.TraderRecommendationRenderers = {
  reasonLine(text, cls, helpers) {
    const { escapeHtml } = helpers;
    const value = String(text || "").trim() || "-";
    const suffix = cls ? ` ${cls}` : "";
    return `<div class="reason-line${suffix}">${escapeHtml(value)}</div>`;
  },

  uniqueReasonTexts(values, limit = 3) {
    const result = [];
    const seen = new Set();
    (values || []).forEach((value) => {
      const text = String(value || "").trim();
      if (!text || seen.has(text) || result.length >= limit) return;
      seen.add(text);
      result.push(text);
    });
    return result;
  },

  tradeActionIntent(action) {
    if (action === "buy_confirmed") return { label: "买入", cls: "action-buy" };
    if (action === "buy_small") return { label: "试单", cls: "action-buy" };
    return { label: "观察", cls: "action-watch" };
  },

  exitActionIntent(action) {
    if (action === "hold") return { label: "持有", cls: "action-hold" };
    if (action === "trim" || action === "take_profit") return { label: "减仓", cls: "action-reduce" };
    if (action === "stop_loss") return { label: "退出", cls: "action-exit" };
    return { label: "观察", cls: "action-watch" };
  },

  actionLine(intent, detail, note = "", helpers) {
    const { escapeHtml } = helpers;
    const parts = [];
    parts.push(`<span class="action-pill ${intent.cls}">${escapeHtml(intent.label)}</span>`);
    if (detail) {
      parts.push(`<span class="action-detail">${escapeHtml(detail)}</span>`);
    }
    if (note) {
      parts.push(`<span class="action-note">${escapeHtml(note)}</span>`);
    }
    return `<div class="action-line">${parts.join("")}</div>`;
  },

  actionColumn(row, helpers) {
    const { formatNumber } = helpers;
    const tradeAction = row.trade_action || {};
    const positionSize = Number(tradeAction.position_size);
    const executionBlocked = row.execution_allowed === false || (Number.isFinite(positionSize) && positionSize <= 0);
    const tradeIntent = executionBlocked
      ? this.tradeActionIntent("watch_only")
      : this.tradeActionIntent(tradeAction.action);
    const detail = executionBlocked ? (row.tier_label || "备选观察") : (tradeAction.label || tradeIntent.label);
    const tradeNote = executionBlocked
      ? "仓位0 · 不执行"
      : Number(row.trade_action_stats?.sample_count || 0)
        ? `${formatNumber(row.trade_action_stats.sample_count, 0)}样本`
        : "样本不足";
    return this.actionLine(tradeIntent, detail, tradeNote, helpers);
  },

  calibrationMetric(stats, primaryKey, fallbackKey) {
    if (!stats) return null;
    const primary = stats[primaryKey];
    if (primary != null) return Number(primary);
    const fallback = stats[fallbackKey];
    return fallback == null ? null : Number(fallback);
  },

  decisionCalibrationSummary(row, helpers) {
    const { formatNumber } = helpers;
    const bucket = row.decision_calibration || {};
    if (Number(bucket.sample_count || 0)) {
      return `操作分段${bucket.label || "-"}，同段${bucket.sample_count}样本，净胜率${formatNumber(bucket.win_rate, 1)}%，净均${formatNumber(bucket.avg_return, 2)}%`;
    }
    const stats = row.similar_signal_stats || {};
    const sample = Number(stats.sample_count || 0);
    if (!sample) {
      return "操作评分历史样本不足";
    }
    const horizon = stats.primary_horizon_label || "主周期";
    const winRate = this.calibrationMetric(stats, "real_win_rate_primary_net", "win_rate_primary_net");
    const avgReturn = this.calibrationMetric(stats, "real_avg_primary_return_net", "avg_primary_return_net");
    const hit3 = stats.hit_3pct_rate == null ? null : Number(stats.hit_3pct_rate);
    const parts = [`同类${sample}样本`];
    if (winRate != null) parts.push(`${horizon}净胜率${formatNumber(winRate, 1)}%`);
    if (avgReturn != null) parts.push(`净均${formatNumber(avgReturn, 2)}%`);
    if (hit3 != null) parts.push(`命中3% ${formatNumber(hit3, 1)}%`);
    return parts.join("，");
  },

  tradeActionSummary(row, helpers) {
    const { formatNumber } = helpers;
    const stats = row.trade_action_stats || {};
    const action = row.trade_action || {};
    if (!Number(stats.sample_count || 0)) {
      return `${action.label || "买入动作"}历史样本不足`;
    }
    return `${action.label || stats.action} 历史${stats.sample_count}样本，净胜率${formatNumber(stats.win_rate, 1)}%，净均${formatNumber(stats.avg_return, 2)}%`;
  },

  exitActionSummary(row, helpers) {
    const { formatNumber } = helpers;
    const stats = row.exit_action_stats || {};
    const action = row.exit_action || {};
    if (!Number(stats.sample_count || 0)) {
      return `${action.label || "卖出动作"}历史样本不足`;
    }
    return `${action.label || stats.action} 历史${stats.sample_count}样本，净胜率${formatNumber(stats.win_rate, 1)}%，平均最大回撤${formatNumber(stats.avg_drawdown, 2)}%`;
  },

  explanationTags(row, helpers) {
    const explanationTexts = this.uniqueReasonTexts([
      row.deepseek_reason,
      ...(row.reasons || []),
      ...((row.serenity_profile?.evidence || []).map(item => item.label || "")),
      row.trade_action?.label,
      row.exit_action?.label,
    ], 4);
    const riskTexts = this.uniqueReasonTexts([
      ...(row.deepseek_risk_flags || []),
      row.sell_risk?.label || "",
      ...(row.sell_risk?.reasons || []),
    ], 4);
    const validationTexts = this.uniqueReasonTexts([
      row.holding_discipline,
      row.trade_action_stats?.sample_count ? this.tradeActionSummary(row, helpers) : "",
      row.exit_action_stats?.sample_count ? this.exitActionSummary(row, helpers) : "",
      row.decision_calibration?.label,
      row.sell_risk_calibration?.label,
      row.similar_signal_stats?.sample_count ? this.decisionCalibrationSummary(row, helpers) : "",
    ], 4);

    const lines = [
      this.reasonLine(`解释：${explanationTexts.join("；") || "暂无"}`, "", helpers),
      this.reasonLine(`风险：${riskTexts.join("；") || "暂无"}`, riskTexts.length ? "warning" : "stable", helpers),
      this.reasonLine(`验证：${validationTexts.join("；") || "暂无"}`, "validation", helpers),
    ];
    return lines.join("");
  },

  rowIndustryLabel(row) {
    return row.industry || row.theme || "行业未知";
  },

  scoreGradeClass(value) {
    if (!Number.isFinite(value)) {
      return "score-grade-empty";
    }
    if (value >= 80) {
      return "score-grade-high";
    }
    if (value >= 60) {
      return "score-grade-mid";
    }
    if (value >= 40) {
      return "score-grade-low";
    }
    return "score-grade-bad";
  },

  scorePairValue(value, digits, title, label = "", helpers) {
    const { escapeHtml, formatNumber } = helpers;
    const num = Number(value);
    const prefix = escapeHtml(label);
    if (!Number.isFinite(num)) {
      return `<span class="score-pair-value score-grade-empty" title="${escapeHtml(title || "暂无数据")}">${prefix}-</span>`;
    }
    return `<span class="score-pair-value ${this.scoreGradeClass(num)}" title="${escapeHtml(title || "")}">${prefix}${formatNumber(num, digits)}</span>`;
  },

  riskGradeClass(value) {
    if (!Number.isFinite(value)) {
      return "score-grade-empty";
    }
    if (value >= 80) {
      return "risk-grade-high";
    }
    if (value >= 60) {
      return "risk-grade-mid";
    }
    if (value >= 40) {
      return "risk-grade-low";
    }
    return "risk-grade-safe";
  },

  riskScoreValue(value, digits, title, label = "", helpers) {
    const { escapeHtml, formatNumber } = helpers;
    const num = Number(value);
    const prefix = escapeHtml(label);
    if (!Number.isFinite(num)) {
      return `<span class="score-pair-value score-grade-empty" title="${escapeHtml(title || "暂无数据")}">${prefix}-</span>`;
    }
    return `<span class="score-pair-value ${this.riskGradeClass(num)}" title="${escapeHtml(title || "")}">${prefix}${formatNumber(num, digits)}</span>`;
  },

  scoreCell(row, helpers) {
    const { escapeHtml, rowScore } = helpers;
    const overall = this.scorePairValue(rowScore(row), 1, "综合评分", "", helpers);
    const risk = this.riskScoreValue(row.sell_risk?.score ?? row.serenity_profile?.risk_score ?? row.avg_risk, 0, "风险评分", "", helpers);
    const number = `
      <div class="score-stack" title="综合评分 / 风险评分">
        <div class="score-line">${overall}<span class="score-pair-separator">/</span>${risk}</div>
      </div>
    `;
    const tier = row.verdict?.tier ? ` score-${escapeHtml(row.verdict.tier)}` : "";
    return `<td class="num score${tier}">${number}</td>`;
  },

  actionSummaryCard(title, rows, key, formatter, helpers) {
    const { escapeHtml } = helpers;
    const counts = new Map();
    rows.forEach(row => {
      const action = row?.[key]?.action;
      if (key === "exit_action" && action === "hold") return;
      if (!action) return;
      if (!counts.has(action)) counts.set(action, []);
      counts.get(action).push(row);
    });
    if (!counts.size) {
      const emptyText = key === "exit_action" ? "暂无明确卖点" : "暂无可执行买入";
      return `<div class="action-summary-card"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(emptyText)}</span></div>`;
    }
    const lines = [...counts.entries()]
      .sort((left, right) => right[1].length - left[1].length)
      .slice(0, 4)
      .map(([, actionRows]) => formatter(actionRows[0], actionRows.length))
      .join("");
    return `<div class="action-summary-card"><strong>${escapeHtml(title)}</strong>${lines}</div>`;
  },

  summaryRowLabel(row, helpers) {
    const { formatNumber, rowScore } = helpers;
    const name = row.name || row.stock_name || row.code || row.ticker || "未命名";
    const score = Number(rowScore(row));
    const suffix = Number.isFinite(score) ? ` ${formatNumber(score, 1)}分` : "";
    return `${name}${suffix}`;
  },

  topActionRows(rows, predicate, helpers, limit = 3) {
    const { rowScore } = helpers;
    return [...(rows || [])]
      .filter(predicate)
      .sort((left, right) => Number(rowScore(right)) - Number(rowScore(left)))
      .slice(0, limit);
  },

  actionableSummary(rows, helpers) {
    const openRows = this.topActionRows(rows, (row) => {
      const action = row?.trade_action?.action;
      return row?.execution_allowed !== false && (action === "buy_confirmed" || action === "buy_small");
    }, helpers);
    const chaseRiskRows = this.topActionRows(rows, (row) => {
      const tradeAction = row?.trade_action?.action;
      const exitAction = row?.exit_action?.action;
      return tradeAction === "avoid_chase" || exitAction === "stop_loss";
    }, helpers);
    const manageRows = this.topActionRows(rows, (row) => {
      const action = row?.exit_action?.action;
      return action === "take_profit" || action === "trim" || action === "stop_loss";
    }, helpers);

    const lines = [];
    if (openRows.length) {
      lines.push({
        title: "可开仓",
        text: `先看 ${openRows.map((row) => this.summaryRowLabel(row, helpers)).join("、")}，只做分批验证，不追单拉升段。`,
      });
    }
    if (chaseRiskRows.length) {
      lines.push({
        title: "先回避",
        text: `${chaseRiskRows.map((row) => this.summaryRowLabel(row, helpers)).join("、")} 风险更高，强拉或转弱时不做追价。`,
      });
    }
    if (manageRows.length) {
      lines.push({
        title: "持仓处理",
        text: `${manageRows.map((row) => this.summaryRowLabel(row, helpers)).join("、")} 已出现减仓/兑现信号，有持仓先处理再谈加仓。`,
      });
    }
    if (!lines.length) {
      lines.push({
        title: "当前结论",
        text: "暂无明确开仓优势，先观察量价是否继续强化，再决定是否参与。",
      });
    }
    return lines;
  },

  findSummaryLine(lines, title) {
    return (lines || []).find((item) => item.title === title) || null;
  },

  renderRecommendationActionSummaryHtml(rows, helpers) {
    const { escapeHtml } = helpers;
    const summaryLines = this.actionableSummary(rows, helpers);
    const executableRows = (rows || []).filter(row => row?.execution_allowed !== false);
    const openLine = this.findSummaryLine(summaryLines, "可开仓") || summaryLines[0] || {
      title: "当前结论",
      text: "暂无明确开仓优势，先观察量价是否继续强化，再决定是否参与。",
    };
    const extraLines = summaryLines.filter((item) => item !== openLine)
      .map((item) => `
        <div class="action-brief-subline">
          <strong>${escapeHtml(item.title)}</strong>
          <span>${escapeHtml(item.text)}</span>
        </div>
      `)
      .join("");
    const buyCard = this.actionSummaryCard("买入动作", executableRows, "trade_action", (row, count) => {
      const action = row.trade_action || {};
      const intent = this.tradeActionIntent(action.action);
      return `
        <div class="action-summary-item">
          <span class="action-pill ${intent.cls}">${escapeHtml(intent.label)}</span>
          <strong class="action-summary-label">${escapeHtml(action.label || action.action || "-")}</strong>
          <span class="action-summary-count">${count}票</span>
        </div>
      `;
    }, helpers);
    const exitCard = this.actionSummaryCard("卖点提示", rows, "exit_action", (row, count) => {
      const action = row.exit_action || {};
      const intent = this.exitActionIntent(action.action);
      return `
        <div class="action-summary-item">
          <span class="action-pill ${intent.cls}">${escapeHtml(intent.label)}</span>
          <strong class="action-summary-label">${escapeHtml(action.label || action.action || "-")}</strong>
          <span class="action-summary-count">${count}票</span>
        </div>
      `;
    }, helpers);
    return `
      <section class="action-summary-shell">
        <div class="action-summary-layout">
          <section class="action-brief-card">
            <strong class="action-card-title">${escapeHtml(openLine.title)}</strong>
            <p class="action-brief-main">${escapeHtml(openLine.text)}</p>
            ${extraLines ? `<div class="action-brief-extra">${extraLines}</div>` : ""}
          </section>
          <section class="action-signals-card">
            <strong class="action-card-title">买入 / 卖点</strong>
            <div class="action-summary-grid">
              ${buyCard}
              ${exitCard}
            </div>
          </section>
        </div>
      </section>
    `;
  },
};
