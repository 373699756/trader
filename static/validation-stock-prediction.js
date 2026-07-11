(function () {
  window.TraderStockPrediction = {
    create(context) {
      const { els, helpers, status } = context;
      const { escapeHtml, formatNumber } = helpers;
      const { renderToolResult, setOpsStatus } = status;

      async function loadStockPrediction() {
        await loadStockPredictionWithMode("prediction");
      }

      async function loadStockOptimization() {
        await loadStockPredictionWithMode("validation");
      }

      async function loadStockPredictionWithMode(mode) {
        const raw = els.stockPredictionInput.value.trim();
        const code = raw.replace(/\D/g, "").slice(0, 6);
        if (code.length !== 6) {
          setOpsStatus(els.stockPredictionStatus, "请输入 6 位股票代码。", "bad");
          renderToolResult('<div class="empty">请输入 6 位股票代码</div>');
          return;
        }
        const label = els.stockPredictionBtn.textContent;
        const tuningLabel = els.generateTuningBtn.textContent;
        els.stockPredictionBtn.disabled = true;
        els.generateTuningBtn.disabled = true;
        if (mode === "prediction") els.stockPredictionBtn.textContent = "预测中…";
        if (mode === "validation") els.generateTuningBtn.textContent = "验证中…";
        setOpsStatus(
          els.stockPredictionStatus,
          mode === "prediction" ? "正在结合量价因子与 DeepSeek 研判走势…" : "正在读取走势预测并复核执行策略…",
          "pending"
        );
        setOpsStatus(els.tuningStatus, mode === "validation" ? "DeepSeek 策略验证中…" : "", mode === "validation" ? "pending" : "");
        try {
          const res = await fetch(`/api/stock-prediction/${encodeURIComponent(code)}?deepseek=1`);
          const payload = await res.json();
          if (!payload.ok) {
            throw new Error(payload.error || "无法给出预测");
          }
          renderStockPrediction(payload);
          const deepseekReady = predictionOptimizationReady(payload.optimization);
          setOpsStatus(
            els.stockPredictionStatus,
            deepseekReady ? "DeepSeek 联合走势预测已更新。" : "本地预测已更新，DeepSeek 暂不可用。",
            deepseekReady ? "ok" : "bad"
          );
          setOpsStatus(
            els.tuningStatus,
            mode === "validation"
              ? deepseekReady ? "DeepSeek 策略验证已更新。" : "DeepSeek 策略验证暂不可用。"
              : "",
            mode === "validation" ? deepseekReady ? "ok" : "bad" : ""
          );
        } catch (err) {
          renderToolResult(`
            <div class="prediction-empty">
              <strong>无法预测</strong>
              <p>${escapeHtml(err.message)}</p>
            </div>
          `);
          setOpsStatus(els.stockPredictionStatus, `预测失败：${err.message}`, "bad");
          if (mode === "validation") setOpsStatus(els.tuningStatus, `验证失败：${err.message}`, "bad");
        } finally {
          els.stockPredictionBtn.disabled = false;
          els.generateTuningBtn.disabled = false;
          els.stockPredictionBtn.textContent = label;
          els.generateTuningBtn.textContent = tuningLabel;
        }
      }

      function renderStockPrediction(payload) {
        const p = payload.prediction || {};
        const deepseekReady = predictionOptimizationReady(payload.optimization);
        const optimization = payload.optimization || null;
        const cls = predictionClass(deepseekReady ? optimization.bias : p.direction);
        const hits = payload.strategy_hits || [];
        const riskFlags = payload.risk_flags || [];
        const actionItems = deepseekReady
          ? uniquePredictionTexts(optimization.entry_plan, optimization.strategy_adjustments).slice(0, 2)
          : uniquePredictionTexts(hits.map(item => item.action), [p.advice]).slice(0, 2);
        const evidenceItems = uniquePredictionTexts(
          deepseekReady ? optimization.reasoning : [],
          hits.flatMap(item => item.reasons || [])
        ).slice(0, 2);
        const riskItems = uniquePredictionTexts(
          deepseekReady ? optimization.risk_controls : [],
          deepseekReady ? optimization.avoid_conditions : [],
          riskFlags
        ).slice(0, 3);
        const summary = deepseekReady && optimization.summary ? optimization.summary : p.advice;
        const sourceLabel = deepseekReady ? "本地量化 + DeepSeek" : "本地量化";
        const nextDayOutlook = deepseekReady ? optimization.next_day_outlook || stockPredictionBias(optimization.bias) : p.label;
        const swingOutlook = deepseekReady ? optimization.swing_outlook || stockPredictionBias(optimization.bias) : "待确认";
        const upProbability = deepseekReady && Number.isFinite(Number(optimization.up_probability))
          ? `${formatNumber(optimization.up_probability, 0)}%`
          : "-";
        renderToolResult(`
          <div class="stock-prediction-result prediction-${cls}">
            <header class="prediction-head">
              <div class="prediction-title-row">
                <span>${escapeHtml(payload.code)} ${escapeHtml(payload.name || "")} · ${formatNumber(payload.price, 3)} · ${formatNumber(payload.pct_chg, 2)}%</span>
                <span class="prediction-source ${deepseekReady ? "is-deepseek" : ""}">${escapeHtml(sourceLabel)}</span>
              </div>
              <div class="prediction-verdict">
                <strong>${escapeHtml(deepseekReady ? stockPredictionBias(optimization.bias) : p.label || "-")}</strong>
                <div class="prediction-inline-metrics">
                  <span>本地量化 ${formatNumber(p.score, 1)}</span>
                  ${deepseekReady ? `<span>DeepSeek 上涨概率 ${upProbability}</span>` : `<span>置信 ${formatNumber(p.confidence, 1)}%</span>`}
                  <span>${deepseekReady ? escapeHtml(stockOptimizationStance(optimization.stance)) : `风险 ${escapeHtml(riskLevelLabel(p.risk_level))}`}</span>
                </div>
              </div>
              <p>${escapeHtml(summary || "暂无有效诊断结论")}</p>
            </header>
            <div class="prediction-levels">
              ${renderPredictionLevel("次日走势", nextDayOutlook, "text")}
              ${renderPredictionLevel("2-5日走势", swingOutlook, "text")}
              ${renderPredictionLevel("上涨概率", upProbability)}
              ${renderPredictionLevel("策略验证", deepseekReady ? `${stockOptimizationStance(optimization.stance)} · ${stockOptimizationTiming(optimization.timing)}` : "待验证", "text")}
            </div>
            <div class="prediction-diagnosis-grid">
              ${renderPredictionDiagnosis("操作", actionItems, "action")}
              ${renderPredictionDiagnosis("依据", evidenceItems, "evidence")}
              ${renderPredictionDiagnosis("风险", riskItems, "risk")}
            </div>
            ${!deepseekReady ? '<div class="prediction-model-note">DeepSeek 未返回有效结果，本次仅展示本地量化诊断。</div>' : ""}
            <p class="prediction-disclaimer">${escapeHtml(payload.data_source || "实时行情")} · ${escapeHtml(payload.disclaimer || "")}</p>
          </div>
        `);
      }

      function predictionOptimizationReady(optimization) {
        return Boolean(optimization?.enabled && ["ok", "cache_hit"].includes(optimization.status));
      }

      function stockPredictionBias(bias) {
        const map = {
          bullish: "短线偏强",
          up: "短线偏强",
          bearish: "短线偏弱",
          down: "短线偏弱",
          neutral: "震荡待确认",
        };
        return map[bias] || "走势待确认";
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

      function stockOptimizationStance(stance) {
        const map = {
          buy_trial: "小仓试单",
          watch_only: "只观察",
          hold_or_wait: "等确认",
          avoid_chase: "不追价",
        };
        return map[stance] || "策略待观察";
      }

      function stockOptimizationTiming(timing) {
        const map = {
          now: "可立即观察执行",
          pullback: "等回踩",
          breakout_confirm: "等突破确认",
          observe: "先观察",
        };
        return map[timing] || "时机待确认";
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
        loadStockOptimization,
        loadStockPrediction,
      };
    },
  };
})();
