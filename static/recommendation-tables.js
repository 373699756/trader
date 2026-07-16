window.TraderRecommendationTables = {
  formatMetricText(value, formatNumber, digits = 2, options = {}) {
    const num = Number(value);
    if (!Number.isFinite(num)) {
      return "<span>-</span>";
    }
    const sign = options.forceSign && num > 0 ? "+" : "";
    const suffix = options.suffix || "";
    const cls = options.withClass ? (num > 0 ? "positive" : num < 0 ? "negative" : "") : "";
    const classAttr = cls ? ` class="${cls}"` : "";
    return `<span${classAttr}>${sign}${formatNumber(num, digits)}${suffix}</span>`;
  },

  formatMoneyText(value, formatMoney) {
    const num = Number(value);
    if (!Number.isFinite(num)) {
      return "<span>-</span>";
    }
    return `<span>${formatMoney(num)}</span>`;
  },

  codeCell(row, helpers) {
    const { escapeHtml, rowIndustryLabel } = helpers;
    return `
      <td class="stock-cell">
        <span class="code-main">${escapeHtml(row.code)}</span>
        <span class="stock-name-inline">${escapeHtml(row.name || "-")}</span>
        <span class="code-sub">${escapeHtml(rowIndustryLabel(row))}</span>
      </td>`;
  },

  marketCapCell(row, helpers) {
    const { formatMoney } = helpers;
    const cap = Number(row.market_cap);
    return `<td class="num market-cap-cell">${Number.isFinite(cap) && cap > 0 ? formatMoney(cap) : "-"}</td>`;
  },

  renderShortTermTableRows(rows, helpers) {
    const { escapeHtml, formatNumber, formatMoney, explanationTags, actionColumn, scoreCell, rowIndustryLabel } = helpers;
    return (rows || []).map(row => {
      const explanation = explanationTags(row);
      const actions = actionColumn(row);
      const latestPrice = Number.isFinite(Number(row.price)) ? `<span>${formatNumber(row.price, 3)}</span>` : "<span>-</span>";
      const todayPct = this.formatMetricText(row.pct_chg, formatNumber, 2, { withClass: true, forceSign: true, suffix: "%" });
      const turnoverRate = this.formatMetricText(row.turnover_rate, formatNumber, 2, { withClass: true, suffix: "%" });
      const turnover = this.formatMoneyText(row.turnover, formatMoney);
      return `
        <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
          <td class="num" title="生产排序序号">${row.selection_rank ?? row.rank}</td>
          ${this.codeCell(row, { escapeHtml, rowIndustryLabel })}
          <td class="num col-latest-price">${latestPrice}</td>
          <td class="col-pct-today">${todayPct}</td>
          <td class="col-turnover-rate">${turnoverRate}</td>
          <td class="num col-turnover">${turnover}</td>
          ${this.marketCapCell(row, { formatMoney })}
          ${scoreCell(row)}
          <td class="action-cell">${actions}</td>
          <td class="reasons">${explanation}</td>
        </tr>
      `;
    }).join("");
  },

  renderTomorrowTableRows(rows, helpers) {
    const { escapeHtml, formatNumber, formatMoney, explanationTags, actionColumn, scoreCell, rowIndustryLabel } = helpers;
    return (rows || []).map(row => {
      const explanation = explanationTags(row);
      const actions = actionColumn(row);
      const latestPrice = Number.isFinite(Number(row.price)) ? `<span>${formatNumber(row.price, 3)}</span>` : "<span>-</span>";
      const todayPct = this.formatMetricText(row.pct_chg, formatNumber, 2, { withClass: true, forceSign: true, suffix: "%" });
      const turnoverRate = this.formatMetricText(row.turnover_rate, formatNumber, 2, { withClass: true, suffix: "%" });
      const turnover = this.formatMoneyText(row.turnover, formatMoney);
      return `
        <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
          <td class="num" title="生产排序序号">${row.selection_rank ?? row.rank}</td>
          ${this.codeCell(row, { escapeHtml, rowIndustryLabel })}
          <td class="num col-latest-price">${latestPrice}</td>
          <td class="col-pct-today">${todayPct}</td>
          <td class="col-turnover-rate">${turnoverRate}</td>
          <td class="num col-turnover">${turnover}</td>
          ${this.marketCapCell(row, { formatMoney })}
          ${scoreCell(row)}
          <td class="action-cell">${actions}</td>
          <td class="reasons">${explanation}</td>
        </tr>
      `;
    }).join("");
  },

  renderSwingTableRows(rows, helpers) {
    const { escapeHtml, formatNumber, formatMoney, explanationTags, actionColumn, scoreCell, rowIndustryLabel } = helpers;
    return (rows || []).map(row => {
      const explanation = explanationTags(row);
      const actions = actionColumn(row);
      const latestPrice = Number.isFinite(Number(row.price)) ? `<span>${formatNumber(row.price, 3)}</span>` : "<span>-</span>";
      const todayPct = this.formatMetricText(row.pct_chg, formatNumber, 2, { withClass: true, forceSign: true, suffix: "%" });
      const turnoverRate = this.formatMetricText(row.turnover_rate, formatNumber, 2, { withClass: true, suffix: "%" });
      const turnover = this.formatMoneyText(row.turnover, formatMoney);
      return `
        <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
          <td class="num" title="生产排序序号">${row.selection_rank ?? row.rank}</td>
          ${this.codeCell(row, { escapeHtml, rowIndustryLabel })}
          <td class="num col-latest-price">${latestPrice}</td>
          <td class="col-pct-today">${todayPct}</td>
          <td class="col-turnover-rate">${turnoverRate}</td>
          <td class="num col-turnover">${turnover}</td>
          ${this.marketCapCell(row, { formatMoney })}
          ${scoreCell(row)}
          <td class="action-cell">${actions}</td>
          <td class="reasons">${explanation}</td>
        </tr>
      `;
    }).join("");
  },

  renderSwingLongTermTableRows(rows, helpers) {
    const { escapeHtml, formatNumber, explanationTags } = helpers;
    return (rows || []).map(row => {
      const explanation = explanationTags(row);
      const profile = row.long_term_profile || {};
      const valueScore = Number(profile.valuation_score ?? row.longTermProfile?.valueScore);
      const longTermScore = Number(profile.long_term_potential ?? row.longTermProfile?.longTermPotential);
      const valueCell = Number.isFinite(valueScore) ? `<span>${formatNumber(valueScore * 100, 0)}</span>` : "<span>-</span>";
      const longTermCell = Number.isFinite(longTermScore) ? `<span>${formatNumber(longTermScore * 100, 0)}</span>` : "<span>-</span>";
      const localScore = Number(row.local_score ?? row.score);
      const deepseekScore = row.deepseek_score == null || row.deepseek_score === "" ? Number.NaN : Number(row.deepseek_score);
      const finalScore = Number(row.final_score ?? row.score);
      const penalty = Number(row.risk_penalty || 0);
      const scoreTrace = `${Number.isFinite(localScore) ? formatNumber(localScore, 0) : "-"} / ${Number.isFinite(deepseekScore) ? formatNumber(deepseekScore, 0) : "-"} / -${formatNumber(penalty, 0)} / ${Number.isFinite(finalScore) ? formatNumber(finalScore, 0) : "-"}`;
      return `
        <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
          <td class="stock-cell stock-cell-wide">
            <span class="code-main">${escapeHtml(row.code)}</span>
            <span class="stock-name-inline">${escapeHtml(row.name || "-")}</span>
            <span class="code-sub">${escapeHtml(row.industry || row.theme || "行业未知")}</span>
          </td>
          <td class="col-pct-today">${valueCell}</td>
          <td class="col-pct-after">${longTermCell}</td>
          <td class="num col-score" title="本地分 / DeepSeek分 / 风险扣分 / 最终分">${scoreTrace}</td>
          <td class="reasons">${explanation}</td>
        </tr>
      `;
    }).join("");
  },

};
