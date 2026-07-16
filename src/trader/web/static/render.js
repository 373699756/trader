(function () {
  "use strict";

  const ACTION_LABELS = {
    executable: "可执行",
    observe: "观察",
    unavailable: "不可执行",
  };

  const DIMENSION_LABELS = {
    value_quality: "价值质量",
    financial_health: "财务健康",
    market_flow: "资金量价",
    industry_policy: "行业政策",
    risk_quality: "风险质量",
  };

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function number(value, digits) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "-";
    return parsed.toLocaleString("zh-CN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function compact(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "-";
    const absolute = Math.abs(parsed);
    if (absolute >= 100000000) return `${number(parsed / 100000000, 2)}亿`;
    if (absolute >= 10000) return `${number(parsed / 10000, 1)}万`;
    return number(parsed, 0);
  }

  function pct(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return { text: "-", className: "" };
    return {
      text: `${parsed > 0 ? "+" : ""}${number(parsed, 2)}%`,
      className: parsed > 0 ? "positive" : parsed < 0 ? "negative" : "",
    };
  }

  function currentTable() {
    return {
      columns: [
        '<col style="width:56px">',
        '<col style="width:168px">',
        '<col style="width:92px">',
        '<col style="width:92px">',
        '<col style="width:150px">',
        '<col style="width:112px">',
        '<col style="width:220px">',
        '<col style="width:96px">',
        '<col style="width:190px">',
      ].join(""),
      head: "<tr><th>排名</th><th>股票</th><th>最新价</th><th>今日涨跌</th><th>成交 / 换手</th><th>总市值</th><th>本地 / 模型 / 扣分 / 最终</th><th>动作</th><th>原因</th></tr>",
    };
  }

  function historyTable() {
    return {
      columns: [
        '<col style="width:56px">',
        '<col style="width:168px">',
        '<col style="width:118px">',
        '<col style="width:118px">',
        '<col style="width:118px">',
        '<col style="width:150px">',
      ].join(""),
      head: "<tr><th>排名</th><th>股票</th><th>锚点价格</th><th>锚点涨跌</th><th>今日涨跌</th><th>锚点至今</th></tr>",
    };
  }

  function rows(items, historical) {
    if (!Array.isArray(items) || items.length === 0) return "";
    return items.map((item) => (historical ? historyRow(item) : currentRow(item))).join("");
  }

  function stock(item) {
    return `<span class="stock-name">${escapeHtml(item.name || "-")}</span><span class="stock-code">${escapeHtml(item.code || "-")} · ${escapeHtml(item.industry || "未分类")}</span>`;
  }

  function currentRow(item) {
    const change = pct(item.pct_change);
    const scores = item.scores || {};
    const action = String(item.action || "unavailable");
    const rowClass = action === "unavailable" ? "is-unavailable" : "";
    const deepseek = scores.deepseek_score == null ? "-" : number(scores.deepseek_score, 2);
    return `<tr class="${rowClass}" tabindex="0" data-code="${escapeHtml(item.code)}">
      <td>${number(item.rank, 0)}</td>
      <td>${stock(item)}</td>
      <td>${number(item.price, 2)}</td>
      <td class="${change.className}">${change.text}</td>
      <td>${compact(item.amount)}<span class="stock-code">换手 ${number(item.turnover_rate, 2)}%</span></td>
      <td>${compact(item.market_cap)}</td>
      <td><div class="score-stack"><span><b>${number(scores.local_score, 2)}</b>本地</span><span><b>${deepseek}</b>模型</span><span><b>${number(scores.deepseek_risk_penalty, 2)}</b>扣分</span><span><b>${number(scores.final_score, 2)}</b>最终</span></div></td>
      <td><span class="action-tag" data-action="${escapeHtml(action)}">${escapeHtml(ACTION_LABELS[action] || action)}</span></td>
      <td class="reason-cell"><span class="reason-tag">${escapeHtml(item.action_reason || "-")}</span></td>
    </tr>`;
  }

  function historyRow(item) {
    const anchorChange = pct(item.anchor_daily_return_pct);
    return `<tr tabindex="0" data-code="${escapeHtml(item.code)}">
      <td>${number(item.rank, 0)}</td>
      <td>${stock(item)}</td>
      <td>${number(item.anchor_price, 2)}</td>
      <td class="${anchorChange.className}">${anchorChange.text}</td>
      <td>-</td>
      <td>-</td>
    </tr>`;
  }

  function drawer(item, snapshot) {
    const scores = item.scores || {};
    const review = item.review || {};
    const dimensions = review.dimensions || {};
    const risks = [...(item.local_risk_facts || []), ...(item.deepseek_risk_facts || [])];
    const components = scores.components || {};
    const features = item.features || {};
    return [
      section("评分", detailGrid([
        ["基础分", number(scores.base_score, 2)],
        ["本地风险扣分", number(scores.local_risk_penalty, 2)],
        ["本地分", number(scores.local_score, 2)],
        ["DeepSeek 分", number(scores.deepseek_score, 2)],
        ["DeepSeek 风险扣分", number(scores.deepseek_risk_penalty, 2)],
        ["最终分", number(scores.final_score, 2)],
        ["置信覆盖", `${number((scores.confidence_coverage || 0) * 100, 1)}%`],
        ["融合模式", snapshot.fusion_mode || scores.fusion_mode || "-"],
      ])),
      section("本地组件", keyValueList(components)),
      section("风险事实", riskList(risks)),
      section("DeepSeek 五维", dimensionList(dimensions, review)),
      section("原始指标", keyValueList(features)),
      section("证据", evidenceList(item.evidence || [])),
      section("快照", detailGrid([
        ["策略版本", snapshot.strategy_version || "-"],
        ["融合版本", snapshot.fusion_version || "-"],
        ["数据版本", snapshot.data_version || "-"],
        ["发布时间", formatDateTime(snapshot.published_at)],
      ])),
    ].join("");
  }

  function section(title, body) {
    return `<section class="detail-section"><h3>${escapeHtml(title)}</h3>${body}</section>`;
  }

  function detailGrid(values) {
    return `<div class="detail-grid">${values.map(([label, value]) => `<div class="detail-value"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div>`;
  }

  function keyValueList(values) {
    const entries = Object.entries(values || {}).sort(([left], [right]) => left.localeCompare(right));
    if (entries.length === 0) return '<div class="detail-value"><span>状态</span><strong>无数据</strong></div>';
    return `<ul class="detail-list">${entries.map(([key, value]) => `<li><b>${escapeHtml(key)}</b> · ${escapeHtml(number(value, 2))}</li>`).join("")}</ul>`;
  }

  function riskList(risks) {
    if (!Array.isArray(risks) || risks.length === 0) return '<div class="detail-value"><span>状态</span><strong>未识别风险事实</strong></div>';
    const seen = new Set();
    return `<ul class="detail-list">${risks.map((risk) => {
      const duplicate = seen.has(risk.risk_fact_id);
      seen.add(risk.risk_fact_id);
      const suffix = duplicate ? " · 已跨来源去重" : "";
      return `<li><b>${escapeHtml(risk.risk_code)}</b> · ${escapeHtml(risk.severity)} · 置信 ${number((risk.confidence || 0) * 100, 0)}% · 扣分 ${number(risk.penalty, 2)}${escapeHtml(suffix)}</li>`;
    }).join("")}</ul>`;
  }

  function dimensionList(dimensions, review) {
    const entries = Object.entries(dimensions || {});
    if (entries.length === 0) {
      const outcome = review && review.outcome ? review.outcome : "未复核";
      return `<div class="detail-value"><span>结果</span><strong>${escapeHtml(outcome)}</strong></div>`;
    }
    return `<ul class="detail-list">${entries.map(([name, value]) => `<li><b>${escapeHtml(DIMENSION_LABELS[name] || name)}</b> · ${number(value.score, 2)} / 置信 ${number((value.confidence || 0) * 100, 0)}%<br>${escapeHtml(value.assessment || "-")}</li>`).join("")}</ul>`;
  }

  function evidenceList(items) {
    if (!Array.isArray(items) || items.length === 0) return '<div class="detail-value"><span>状态</span><strong>无结构化证据</strong></div>';
    return `<ul class="detail-list">${items.map((item) => `<li><b>${escapeHtml(item.type || "evidence")}</b> · ${escapeHtml(item.title || "-")}<br>${escapeHtml(item.source || "-")} · ${escapeHtml(formatDateTime(item.published_at))}</li>`).join("")}</ul>`;
  }

  function formatDateTime(value) {
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return "-";
    return parsed.toLocaleString("zh-CN", { hour12: false });
  }

  window.TraderRender = {
    currentTable,
    drawer,
    escapeHtml,
    formatDateTime,
    historyTable,
    number,
    pct,
    rows,
  };
})();
