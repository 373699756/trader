window.TraderRecommendationTables = {
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
      const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
      const explanation = explanationTags(row);
      const actions = actionColumn(row);
      return `
        <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
          <td class="num">${row.rank}</td>
          ${this.codeCell(row, { escapeHtml, rowIndustryLabel })}
          <td>${escapeHtml(row.name)}</td>
          <td>${escapeHtml(row.market_label)}</td>
          <td class="num">${formatNumber(row.price, 3)}</td>
          <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
          <td class="num">${formatNumber(row.speed || row.five_min_pct, 2)}%</td>
          <td class="num">${formatNumber(row.volume_ratio, 2)}</td>
          <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
          <td class="num">${formatMoney(row.turnover)}</td>
          ${this.marketCapCell(row, { formatMoney })}
          <td>${escapeHtml(row.industry || row.theme || "-")}</td>
          <td class="num">${formatNumber(row.momentum_score, 1)}</td>
          <td class="num">${formatNumber(row.sentiment_score, 1)}</td>
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
      const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
      const sixtyClass = row.sixty_day_pct >= 0 ? "positive" : "negative";
      const explanation = explanationTags(row);
      const actions = actionColumn(row);
      return `
        <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
          <td class="num">${row.rank}</td>
          ${this.codeCell(row, { escapeHtml, rowIndustryLabel })}
          <td>${escapeHtml(row.name)}</td>
          <td>${escapeHtml(row.market_label)}</td>
          <td class="num">${formatNumber(row.price, 3)}</td>
          <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
          <td class="num">${formatNumber(row.volume_ratio, 2)}</td>
          <td class="num">${formatNumber(row.turnover_rate, 2)}%</td>
          <td class="num">${formatMoney(row.turnover)}</td>
          ${this.marketCapCell(row, { formatMoney })}
          <td class="num ${sixtyClass}">${formatNumber(row.sixty_day_pct, 2)}%</td>
          <td class="num">${formatNumber(row.liquidity_score, 1)}</td>
          <td class="num">${formatNumber(row.momentum_score, 1)}</td>
          <td class="num">${formatNumber(row.trend_score, 1)}</td>
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
      const pctClass = row.pct_chg >= 0 ? "positive" : "negative";
      const ret5Class = row.ret_5d >= 0 ? "positive" : "negative";
      const ret10Class = row.ret_10d >= 0 ? "positive" : "negative";
      const ret20Class = row.ret_20d >= 0 ? "positive" : "negative";
      const explanation = explanationTags(row);
      const actions = actionColumn(row);
      return `
        <tr data-code="${escapeHtml(row.code)}" data-name="${escapeHtml(row.name)}">
          <td class="num">${row.rank}</td>
          ${this.codeCell(row, { escapeHtml, rowIndustryLabel })}
          <td>${escapeHtml(row.name)}</td>
          <td>${escapeHtml(row.market_label)}</td>
          <td class="num">${formatNumber(row.price, 3)}</td>
          <td class="num ${pctClass}">${formatNumber(row.pct_chg, 2)}%</td>
          <td class="num ${ret5Class}">${formatNumber(row.ret_5d, 2)}%</td>
          <td class="num ${ret10Class}">${formatNumber(row.ret_10d, 2)}%</td>
          <td class="num ${ret20Class}">${formatNumber(row.ret_20d, 2)}%</td>
          <td class="num">${formatNumber(row.ma20_gap, 2)}%</td>
          <td class="num">${formatMoney(row.turnover)}</td>
          ${this.marketCapCell(row, { formatMoney })}
          <td class="num">${formatNumber(row.momentum_score, 1)}</td>
          <td class="num">${formatNumber(row.trend_score, 1)}</td>
          ${scoreCell(row)}
          <td class="action-cell">${actions}</td>
          <td class="reasons">${explanation}</td>
        </tr>
      `;
    }).join("");
  },
};
