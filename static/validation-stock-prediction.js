(function () {
  window.TraderStockPrediction = {
    create(context) {
      const { els, helpers, status } = context;
      const { escapeHtml, formatNumber } = helpers;
      const { renderToolResult, setOpsStatus } = status;

      async function loadStockPrediction() {
        const raw = els.stockPredictionInput.value.trim();
        const code = raw.replace(/\D/g, "").slice(0, 6);
        if (code.length !== 6) {
          setOpsStatus(els.stockPredictionStatus, "请输入 6 位股票代码。", "bad");
          renderToolResult('<div class="empty">请输入 6 位股票代码</div>');
          return;
        }
        const label = els.stockPredictionBtn.textContent;
        els.stockPredictionBtn.disabled = true;
        if (els.generateTuningBtn) els.generateTuningBtn.disabled = true;
        els.stockPredictionBtn.textContent = "预测中…";
        setOpsStatus(els.stockPredictionStatus, "正在计算本地量价与三策略命中…", "pending");
        try {
          const res = await fetch(`/api/stock-prediction/${encodeURIComponent(code)}`);
          const payload = await res.json();
          if (!payload.ok) {
            throw new Error(payload.error || "无法给出预测");
          }
          renderStockPrediction(payload);
          setOpsStatus(
            els.stockPredictionStatus,
            "本地量化预测已更新。",
            "ok"
          );
        } catch (err) {
          renderToolResult(`
            <div class="prediction-empty">
              <strong>无法预测</strong>
              <p>${escapeHtml(err.message)}</p>
            </div>
          `);
          setOpsStatus(els.stockPredictionStatus, `预测失败：${err.message}`, "bad");
        } finally {
          els.stockPredictionBtn.disabled = false;
          if (els.generateTuningBtn) els.generateTuningBtn.disabled = false;
          els.stockPredictionBtn.textContent = label;
        }
      }

      function renderStockPrediction(payload) {
        const p = payload.prediction || {};
        const cls = predictionClass(p.direction);
        const hits = payload.strategy_hits || [];
        const riskFlags = payload.risk_flags || [];
        const actionItems = uniquePredictionTexts(hits.map(item => item.action), [p.advice]).slice(0, 2);
        const evidenceItems = uniquePredictionTexts(
          hits.flatMap(item => item.reasons || [])
        ).slice(0, 2);
        const riskItems = uniquePredictionTexts(
          riskFlags
        ).slice(0, 3);
        const summary = p.advice;
        const sourceLabel = "本地量化";
        const nextDayOutlook = p.label || "待确认";
        const swingHit = hits.find(item => ["swing_picks", "swing_2_5d_picks"].includes(item.strategy));
        const swingOutlook = swingHit?.label || swingHit?.action || "以2-5日策略命中为准";
        renderToolResult(`
          <div class="stock-prediction-result prediction-${cls}">
            <header class="prediction-head">
              <div class="prediction-title-row">
                <span>${escapeHtml(payload.code)} ${escapeHtml(payload.name || "")} · ${formatNumber(payload.price, 3)} · ${formatNumber(payload.pct_chg, 2)}%</span>
                <span class="prediction-source">${escapeHtml(sourceLabel)}</span>
              </div>
              <div class="prediction-verdict">
                <strong>${escapeHtml(p.label || "-")}</strong>
                <div class="prediction-inline-metrics">
                  <span>本地量化 ${formatNumber(p.score, 1)}</span>
                  <span title="规则条件的匹配程度，不是上涨概率">规则一致度 ${formatNumber(p.rule_consistency, 1)}%</span>
                  <span>风险 ${escapeHtml(riskLevelLabel(p.risk_level))}</span>
                </div>
              </div>
              <p>${escapeHtml(summary || "暂无有效诊断结论")}</p>
            </header>
            <div class="prediction-levels">
              ${renderPredictionLevel("次日走势", nextDayOutlook, "text")}
              ${renderPredictionLevel("2-5日走势", swingOutlook, "text")}
              ${renderPredictionLevel("今日策略", hits.some(item => ["today_term", "today_picks"].includes(item.strategy)) ? "命中候选" : "未命中", "text")}
              ${renderPredictionLevel("策略验证", "以真实样本外收益为准", "text")}
            </div>
            <div class="prediction-diagnosis-grid">
              ${renderPredictionDiagnosis("操作", actionItems, "action")}
              ${renderPredictionDiagnosis("依据", evidenceItems, "evidence")}
              ${renderPredictionDiagnosis("风险", riskItems, "risk")}
            </div>
            <div class="prediction-model-note">DeepSeek 证据特征只由盘中后台任务异步生成，不参与本页面同步个股预测。</div>
            <p class="prediction-disclaimer">${escapeHtml(payload.data_source || "实时行情")} · ${escapeHtml(payload.disclaimer || "")}</p>
          </div>
        `);
      }

      function uniquePredictionTexts(...groups) {
        const seen = new Set();
        return groups.flat().filter(text => {
          const normalized = String(text || "").trim();
          if (!normalized || normalized === "-") return false;
          if (seen.has(normalized)) return false;
          seen.add(normalized);
          return true;
        }).map(text => String(text).trim());
      }

      function renderPredictionLevel(label, value, valueType = "number") {
        return `
          <div class="prediction-level ${valueType === "text" ? "is-text" : ""}">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value || "-")}</strong>
          </div>
        `;
      }

      function renderPredictionDiagnosis(label, items, tone) {
        const rows = items.length
          ? items.map(text => `<li>${escapeHtml(text)}</li>`).join("")
          : "<li>暂无明确有效信号</li>";
        return `
          <section class="prediction-diagnosis prediction-diagnosis-${tone}">
            <h3>${escapeHtml(label)}</h3>
            <ul>${rows}</ul>
          </section>
        `;
      }

      function predictionClass(direction) {
        if (direction === "up" || direction === "bullish") return "up";
        if (direction === "down" || direction === "bearish") return "down";
        return "neutral";
      }

      function riskLevelLabel(level) {
        if (level === "high") return "高";
        if (level === "medium") return "中";
        if (level === "low") return "低";
        return "未知";
      }

      return {
        loadStockPrediction,
      };
    },
  };
})();
