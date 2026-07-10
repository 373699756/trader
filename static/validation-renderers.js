window.TraderValidationRenderers = {
  validationDatePageCount(totalRows, pageSize) {
    return Math.max(1, Math.ceil(Number(totalRows || 0) / pageSize));
  },

  clampValidationDatePage(page, totalRows, pageSize) {
    const maxPage = this.validationDatePageCount(totalRows, pageSize) - 1;
    return Math.min(Math.max(0, Number(page || 0)), maxPage);
  },

  validationSkipReason(value) {
    const reason = String(value || "").trim();
    if (!reason) return "";
    if (reason === "unbuyable_limit_up") return "涨停不可买";
    if (reason === "excluded") return "执行剔除";
    return reason;
  },

  primaryValidationLabel(row) {
    const strategy = row.strategy_name || "";
    if (strategy === "swing_picks") return "2-5日退出";
    if (strategy === "short_term") return "次日辅助";
    return "次日";
  },

  primaryValidationReturn(row) {
    const strategy = row.strategy_name || "";
    if (strategy === "swing_picks") {
      return row.signal_exit_return ?? row.exit_return ?? row.signal_hold_5d_return ?? row.hold_5d_return;
    }
    return row.signal_next_close_return ?? row.next_close_return;
  },

  primaryValidationNetReturn(row) {
    const rawValue = this.primaryValidationReturn(row);
    if (rawValue === null || rawValue === undefined || rawValue === "") return null;
    const rawReturn = Number(rawValue);
    if (!Number.isFinite(rawReturn)) return null;
    const tradeCost = Number(row.trade_cost_pct);
    return Number.isFinite(tradeCost) ? rawReturn - tradeCost : rawReturn;
  },

  validationSampleTypeBadge(row) {
    const real = Number(row.real_count || 0);
    const replay = Number(row.replay_count || 0);
    const count = Number(row.count || 0);
    if (row.sample_type === "empty" || (!count && !real && !replay)) {
      return `<span class="tag muted">空批次</span>`;
    }
    if (real > 0 && replay > 0) {
      return `<span class="tag validation">真${real}/回${replay}</span>`;
    }
    if (real > 0) {
      return `<span class="tag stable">真实</span>`;
    }
    return `<span class="tag warning">回放</span>`;
  },

  renderValidationDatePageRows(pageRows, helpers) {
    const { escapeHtml } = helpers;
    return (pageRows || []).map(row => `
      <tr data-date="${escapeHtml(row.signal_date)}" data-strategy="${escapeHtml(row.strategy_name)}" data-sample-type="${escapeHtml(row.sample_type || "")}">
	        <td>${escapeHtml(row.signal_date)}</td>
	        <td>${this.validationSampleTypeBadge(row)}</td>
	        <td class="num">${row.count}</td>
	        <td>${escapeHtml(row.signal_time || "-")}</td>
      </tr>
    `).join("");
  },

  renderValidationDetailRows(rows, helpers) {
    const { escapeHtml, formatNumber, numberClass } = helpers;
    const pctText = (value) => {
      const num = Number(value);
      return Number.isFinite(num) ? `${formatNumber(num, 2)}%` : "-";
    };
    return (rows || []).map(row => {
      const anchorPrice = Number(row.price_at_signal);
      const anchorChange = row.pct_chg_at_signal;
      const todayChange = row.current_pct_chg;
      const anchorToNow = row.anchor_to_now_return;
      const primaryLabel = this.primaryValidationLabel(row);
      const primaryReturn = this.primaryValidationReturn(row);
      const tradeCost = row.trade_cost_pct;
      const skipReason = this.validationSkipReason(row.skip_reason);
      const executionText = skipReason || (row.outcome_updated_at ? "已回填" : "待回填");
      const executionClass = skipReason ? "risk" : row.outcome_updated_at ? "stable" : "warning";
      const isReplay = String(row.strategy_version || "").toLowerCase().includes("replay");
      const anchorPriceText = Number.isFinite(anchorPrice) && anchorPrice > 0 ? formatNumber(anchorPrice, 3) : "-";
      return `
        <tr class="${isReplay ? "validation-replay-row" : ""}" data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
          <td class="num">${row.rank}</td>
          <td>${isReplay ? '<span class="tag warning">回放</span>' : '<span class="tag stable">真实</span>'}</td>
          <td class="validation-stock-cell">
            <span class="validation-stock-name">${escapeHtml(row.name || "-")}</span>
            <span class="validation-stock-code">${escapeHtml(row.code)}</span>
          </td>
          <td class="num">${anchorPriceText}</td>
          <td class="num ${numberClass(anchorChange)}">${pctText(anchorChange)}</td>
          <td>${escapeHtml(primaryLabel)}</td>
          <td class="num ${numberClass(primaryReturn)}">${pctText(primaryReturn)}</td>
          <td class="num">${tradeCost != null ? `${formatNumber(tradeCost, 2)}%` : "-"}</td>
          <td><span class="tag ${executionClass}">${escapeHtml(executionText)}</span></td>
          <td class="num ${numberClass(todayChange)}" data-validation-field="current_pct_chg">${pctText(todayChange)}</td>
          <td class="num ${numberClass(anchorToNow)}" data-validation-field="anchor_to_now_return">${pctText(anchorToNow)}</td>
        </tr>
      `;
    }).join("");
  },

  updateValidationPctCell(cell, value, helpers) {
    if (!cell) return;
    const { numberClass, formatNumber } = helpers;
    const num = Number(value);
    cell.className = `num ${numberClass(num)}`;
    cell.textContent = Number.isFinite(num) ? `${formatNumber(num, 2)}%` : "-";
  },
};
