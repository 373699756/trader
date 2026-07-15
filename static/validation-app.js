(function () {
  window.TraderValidationApp = {
    create(context) {
      const { state, els, helpers, config, status } = context;
      const { VALIDATION_AUTO_REFRESH_MS, VALIDATION_DATE_PAGE_SIZE } = config;
      const {
        escapeHtml,
        formatNumber,
        numberClass,
        strategyLabel,
        validationSnapshotStrategiesText,
      } = helpers;
      const { renderToolResult, setOpsStatus, setStatus } = status;
      const ValidationUI = window.TraderValidationUI;
      const ValidationRenderers = window.TraderValidationRenderers;

      async function loadValidation() {
        state.validationLoaded = true;
        const options = arguments[0] || {};
        const isSilent = Boolean(options.silent);
        const fromAutoRefresh = Boolean(options.fromAutoRefresh);
        const skipAutoOutcomeUpdate = Boolean(options.skipAutoOutcomeUpdate);
        const strategy = currentValidationStrategy();
        updateValidationChrome(strategy);
        const cacheKey = `${strategy}:${els.validationDaysSelect.value}`;
        const cached = state.validationCache[cacheKey];
        if (cached) {
          applyValidationPayload(cached);
        } else {
          els.validationDatesBody.innerHTML = '<tr><td colspan="4" class="empty">加载中...</td></tr>';
          els.validationDetailBody.innerHTML = '<tr><td colspan="11" class="empty">选择左侧批次查看明细</td></tr>';
        }
        const params = new URLSearchParams({
          strategy,
          days: els.validationDaysSelect.value,
          light: "1",
        });
        const requestSeq = ++state.validationRequestSeq;
        try {
          const res = await fetch(`/api/strategy-validation?${params.toString()}`);
          const payload = await res.json();
          if (!payload.ok) {
            throw new Error(payload.error || "接口返回异常");
          }
          if (requestSeq !== state.validationRequestSeq || strategy !== currentValidationStrategy()) {
            return;
          }
          state.validationCache[cacheKey] = payload;
          applyValidationPayload(payload);
          window.setTimeout(() => {
            loadValidationMetrics(strategy, els.validationDaysSelect.value, requestSeq, cacheKey, {
              skipAutoOutcomeUpdate,
            });
          }, 80);
          if (!els.updateStatus.textContent || els.updateStatus.textContent.includes("后台")) {
            loadValidationAutoUpdateStatus();
          }
          if (!isSilent) {
            setStatus("策略验证已更新");
          }
          if (!fromAutoRefresh) {
            startValidationAutoRefreshLoop();
          }
        } catch (err) {
          if (requestSeq !== state.validationRequestSeq) {
            return;
          }
          els.validationDatesBody.innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(err.message)}</td></tr>`;
          setStatus(`策略验证加载失败：${err.message}`);
        }
      }

      async function loadValidationMetrics(strategy, days, requestSeq, cacheKey, options = {}) {
        const params = new URLSearchParams({ strategy, days });
        try {
          const res = await fetch(`/api/strategy-validation?${params.toString()}`);
          const payload = await res.json();
          if (!payload.ok) {
            return;
          }
          if (requestSeq !== state.validationRequestSeq || strategy !== currentValidationStrategy()) {
            return;
          }
          state.validationCache[cacheKey] = payload;
          if (payload.metrics) {
            state.validationMetrics = payload.metrics;
            renderValidationMetrics(payload.metrics, payload.validation_gate || {});
            loadValidationOosReport(strategy, days, requestSeq);
          }
          if (!options.skipAutoOutcomeUpdate) {
            autoFillMissingValidationOutcomes(payload.metrics || {}, payload.dates || []);
          }
        } catch (err) {
          /* 指标慢或失败不影响批次列表和明细查看 */
        }
      }

      async function loadValidationOosReport(strategy, days, requestSeq) {
        if (!els.validationOosReport) return;
        els.validationOosReport.className = "validation-oos-report oos-waiting";
        els.validationOosReport.textContent = "OOS 状态加载中";
        const params = new URLSearchParams({ strategy, days });
        try {
          const payload = await fetchValidationOosReportPayload(params);
          if (requestSeq !== state.validationRequestSeq || strategy !== currentValidationStrategy()) {
            return;
          }
          renderValidationOosReport(payload);
          renderValidationPortfolioBaseline(payload.portfolio_baseline || {});
          maybeAutoBackfillCurrentBaseline(payload, strategy, days, requestSeq);
        } catch (err) {
          if (requestSeq !== state.validationRequestSeq) {
            return;
          }
          els.validationOosReport.className = "validation-oos-report oos-watch";
          els.validationOosReport.textContent = "OOS 状态暂不可用";
        }
      }

      async function fetchValidationOosReportPayload(params = null) {
        const query = params || new URLSearchParams({
          strategy: currentValidationStrategy(),
          days: els.validationDaysSelect.value,
        });
        const res = await fetch(`/api/strategy-validation/oos-report?${query.toString()}`);
        const payload = await res.json();
        if (!payload.ok) {
          throw new Error(payload.error || "OOS report unavailable");
        }
        return payload;
      }

      function applyValidationPayload(payload) {
        if (payload.metrics) {
          state.validationMetrics = payload.metrics || {};
          renderValidationMetrics(state.validationMetrics, payload.validation_gate || {});
        }
        renderValidationDates(payload.dates || []);
        syncValidationSelection(payload.dates || []);
      }

      function tuningRunMarkup(payload, error = "") {
        if (error) {
          return `
            <section class="strategy-tuning-report" aria-label="影子调参建议">
              <div class="tuning-line">
                <strong>影子调参建议暂不可用</strong>
                <span>${escapeHtml(error)}</span>
              </div>
            </section>
          `;
        }
        const run = payload?.latest || {};
        const plan = run?.plan || payload?.plan || null;
        if (!plan || !Object.keys(plan).length) {
          return `
            <section class="strategy-tuning-report" aria-label="影子调参建议">
              <div class="empty">暂无影子调参建议</div>
            </section>
          `;
        }
        const issues = plan.issues || [];
        const suggestions = plan.suggestions || [];
        const gate = plan.gate || {};
        const gateItems = gate.items || [];
        const statusText = plan.can_apply
          ? "影子建议满足门槛"
          : plan.shadow_mode
          ? "影子验证"
          : "仅记录";
        return `
          <section class="strategy-tuning-report" aria-label="影子调参建议">
            <div class="tuning-line">
              <strong>${escapeHtml(statusText)}</strong>
              <span class="tag muted">${payload?.reused ? "复用已有建议" : "建议已更新"}</span>
              <span>${escapeHtml(plan.reason || "-")}</span>
              <span>${escapeHtml(plan.generated_at || run?.run_time || "")}</span>
            </div>
            <div class="tuning-tags">
              ${issues.length ? issues.map(item => `<span class="tag warning">${escapeHtml(item)}</span>`).join("") : '<span class="tag muted">暂无主要问题</span>'}
            </div>
            <div class="tuning-tags">
              ${suggestions.length ? suggestions.map(item => `<span class="tag validation">${escapeHtml(item.parameter)}：${escapeHtml(formatTuningValue(item.value))}</span>`).join("") : '<span class="tag muted">暂无参数建议</span>'}
            </div>
            <div class="tuning-tags">
              ${gateItems.map(item => `<span class="tag ${item.passed ? "stable" : "risk"}">${escapeHtml(item.name)} ${item.passed ? "通过" : "阻断"} · 实际 ${escapeHtml(formatTuningValue(item.actual))} · 门槛 ${escapeHtml(formatTuningValue(item.required))}</span>`).join("")}
            </div>
          </section>
        `;
      }

      async function loadStrategyValidationReport() {
        const strategy = currentValidationStrategy();
        const days = String(els.validationDaysSelect.value || "20");
        const requestSeq = Number(state.validationReportRequestSeq || 0) + 1;
        state.validationReportRequestSeq = requestSeq;
        const isCurrentRequest = () => (
          requestSeq === state.validationReportRequestSeq &&
          strategy === currentValidationStrategy() &&
          days === String(els.validationDaysSelect.value || "20")
        );
        const buttonLabel = els.generateTuningBtn?.textContent || "策略验证";
        if (els.generateTuningBtn) {
          els.generateTuningBtn.disabled = true;
          els.generateTuningBtn.textContent = "验证中…";
        }
        if (els.stockPredictionBtn) els.stockPredictionBtn.disabled = true;
        setOpsStatus(
          els.tuningStatus,
          `正在读取${strategyLabel(strategy)}策略近${days}日历史验证报告…`,
          "pending",
        );
        renderToolResult('<div class="empty">正在汇总真实样本与验证门控...</div>');
        const params = new URLSearchParams({ strategy, days });
        try {
          const validationRes = await fetch(`/api/strategy-validation?${params.toString()}`);
          const validationPayload = await validationRes.json();
          if (!validationRes.ok || !validationPayload.ok) {
            throw new Error(validationPayload.error || "策略验证报告接口返回异常");
          }
          if (!isCurrentRequest()) return;
          if (validationPayload.metrics && els.validationSampleCount) {
            state.validationMetrics = validationPayload.metrics;
            renderValidationMetrics(validationPayload.metrics, validationPayload.validation_gate || {});
          }

          let tuningPayload = null;
          let tuningError = "";
          try {
            const tuningRes = await fetch(`/api/strategy-validation/tuning?${params.toString()}`, {
              method: "POST",
            });
            tuningPayload = await tuningRes.json();
            if (!tuningRes.ok || !tuningPayload.ok) {
              throw new Error(tuningPayload.error || "影子调参建议接口返回异常");
            }
          } catch (err) {
            tuningError = err.message || String(err);
          }
          if (!isCurrentRequest()) return;

          renderStrategyValidationReport(validationPayload, strategy, days, tuningPayload, tuningError);
          if (tuningError) {
            setOpsStatus(
              els.tuningStatus,
              `${strategyLabel(strategy)}策略验证已完成；调参建议生成失败：${tuningError}`,
              "bad",
            );
          } else {
            const tuningStatus = tuningPayload?.reused ? "已复用现有调参建议" : "调参建议已更新";
            setOpsStatus(
              els.tuningStatus,
              `${strategyLabel(strategy)}策略近${days}日验证已完成，${tuningStatus}。`,
              "ok",
            );
          }
        } catch (err) {
          if (!isCurrentRequest()) return;
          renderToolResult(`
            <div class="prediction-empty">
              <strong>策略验证失败</strong>
              <p>${escapeHtml(err.message)}</p>
            </div>
          `);
          setOpsStatus(els.tuningStatus, `验证失败：${err.message}`, "bad");
        } finally {
          if (isCurrentRequest() && els.generateTuningBtn) {
            els.generateTuningBtn.disabled = false;
            els.generateTuningBtn.textContent = buttonLabel;
          }
          if (isCurrentRequest() && els.stockPredictionBtn) els.stockPredictionBtn.disabled = false;
        }
      }

      function renderStrategyValidationReport(payload, strategy, days, tuningPayload = null, tuningError = "") {
        const metrics = payload.metrics || {};
        const gate = payload.validation_gate || {};
        const sampleCount = Number(metrics.sample_count || 0);
        const realDays = Number(metrics.real_day_count || 0);
        const pending = Number(metrics.pending_outcome_count || 0);
        const winRate = metrics.real_win_rate_primary_net ?? metrics.win_rate_primary_net;
        const avgReturn = metrics.real_avg_primary_return_net ?? metrics.avg_primary_return_net;
        const drawdown = metrics.real_portfolio_max_drawdown_pct ??
          metrics.real_avg_max_drawdown_primary ??
          metrics.avg_max_drawdown_primary;
        const verdict = sampleCount <= 0
          ? "暂无可验证样本"
          : gate.blocked
          ? "验证门控未通过"
          : "验证门控通过";
        const verdictClass = sampleCount <= 0 || gate.blocked ? "warning" : "stable";
        renderToolResult(`
          <div class="strategy-validation-combined">
            <section class="strategy-validation-report" aria-label="策略验证报告">
              <div class="tuning-line">
                <strong>${escapeHtml(strategyLabel(strategy))} · 近${escapeHtml(days)}日</strong>
                <span class="tag ${verdictClass}">${escapeHtml(verdict)}</span>
              </div>
              <div class="tuning-tags">
                <span class="tag validation">真实交易日 ${realDays}</span>
                <span class="tag validation">有效样本 ${sampleCount}</span>
                <span class="tag ${pending ? "warning" : "muted"}">待回填 ${pending}</span>
                <span class="tag validation">净胜率 ${winRate == null ? "-" : `${formatNumber(winRate, 1)}%`}</span>
                <span class="tag validation">净收益 ${formatSignedPct(avgReturn)}</span>
                <span class="tag validation">回撤 ${formatSignedPct(drawdown)}</span>
              </div>
              <p>${escapeHtml(gate.reason || "以真实样本、交易成本和当前策略版本统计。")}</p>
            </section>
            ${tuningRunMarkup(tuningPayload, tuningError)}
          </div>
        `);
      }

      function formatTuningValue(value) {
        if (value && typeof value === "object") {
          return JSON.stringify(value);
        }
        return String(value ?? "-");
      }

      function isValidationPanelActive() {
        const panel = document.getElementById("validationPanel");
        return Boolean(panel && panel.classList.contains("active"));
      }

      function currentValidationStrategy() {
        return els.validationStrategySelect?.value || "today_term";
      }

      function syncValidationStrategyTabs(strategy) {
        els.validationStrategyTabs.forEach(button => {
          const isActive = button.dataset.validationStrategy === strategy;
          button.classList.toggle("active", isActive);
          button.setAttribute("aria-selected", isActive ? "true" : "false");
        });
      }

      function updateValidationChrome(strategy) {
        const meta = ValidationUI.validationStrategyMeta(strategy, strategyLabel);
        syncValidationStrategyTabs(strategy);
        if (els.validationTitle) {
          els.validationTitle.textContent = `${meta.label}复盘`;
        }
        if (els.validationSubtitle) {
          els.validationSubtitle.textContent = `查看${meta.label}已保存样本、真实回填和回放表现，主看${meta.outcome}与执行状态。`;
        }
        if (els.validationWinRateLabel) {
          els.validationWinRateLabel.textContent = `${meta.horizon}净胜率`;
        }
        if (els.validationAvgReturnLabel) {
          els.validationAvgReturnLabel.textContent = `${meta.horizon}净收益`;
        }
        if (els.validationDetailTitle) {
          els.validationDetailTitle.textContent = `${meta.label}股票主周期明细`;
        }
      }

      function startValidationAutoRefreshLoop() {
        if (state.validationAutoRefreshTimer) {
          return;
        }
        state.validationAutoRefreshTimer = setInterval(() => {
          if (!isValidationPanelActive()) {
            return;
          }
          void loadValidation({ silent: true, fromAutoRefresh: true });
        }, VALIDATION_AUTO_REFRESH_MS);
      }

      function stopValidationAutoRefreshLoop() {
        if (!state.validationAutoRefreshTimer) {
          return;
        }
        clearInterval(state.validationAutoRefreshTimer);
        state.validationAutoRefreshTimer = null;
      }

      async function autoFillMissingValidationOutcomes(metrics, dates) {
        if (state.validationAutoRefreshInFlight) {
          return;
        }
        const outcomeSampleCount = Number(metrics?.outcome_sample_count || 0);
        const realSampleCount = Number(metrics?.real_sample_count || 0);
        if (outcomeSampleCount > 0 && realSampleCount > 0) {
          return;
        }
        const latestDate = Array.isArray(dates) && dates.length ? String(dates[0]?.signal_date || "").trim() : "";
        if (!latestDate) {
          return;
        }
        const strategy = currentValidationStrategy();
        const refreshKey = `${strategy}:${latestDate}`;
        const now = Date.now();
        if (state.validationAutoRefreshDate === refreshKey && now - state.validationAutoRefreshAt < VALIDATION_AUTO_REFRESH_MS) {
          return;
        }
        state.validationAutoRefreshInFlight = true;
        state.validationAutoRefreshDate = refreshKey;
        state.validationAutoRefreshAt = now;
        try {
          setOpsStatus(els.updateStatus, `检测到 ${strategyLabel(strategy)} ${latestDate} 无真实验证结果，正在回填收益`, "pending");
          const params = new URLSearchParams({ date: latestDate, strategy });
          const res = await fetch(`/api/strategy-validation/update?${params.toString()}`, {
            method: "POST",
          });
          const payload = await res.json();
          if (!payload.ok) {
            throw new Error(payload.error || "后台回填失败");
          }
          const result = payload.result || {};
          setOpsStatus(
            els.updateStatus,
            `已触发 ${strategyLabel(strategy)} ${latestDate} 回填，新增 ${result.updated || 0} 条，跳过 ${result.skipped || 0} 条，执行跳过 ${result.execution_skipped || 0} 条`,
            "ok",
          );
          if (isValidationPanelActive()) {
            await loadValidation({ silent: true, fromAutoRefresh: true, skipAutoOutcomeUpdate: true });
          }
        } catch (err) {
          setOpsStatus(els.updateStatus, `自动回填失败：${escapeHtml(err.message)}`, "bad");
        } finally {
          state.validationAutoRefreshInFlight = false;
        }
      }

      function shouldAutoBackfillCurrentBaseline(report) {
        const baseline = report?.baseline_status || {};
        const pending = Number(baseline.pending_current_baseline_count || 0);
        const mismatch = Number(baseline.mismatched_baseline_outcome_count || 0);
        return report?.oos_status === "needs_backfill" || Boolean(baseline.needs_backfill) || pending > 0 || mismatch > 0;
      }

      async function maybeAutoBackfillCurrentBaseline(report, strategy, days, requestSeq) {
        if (!shouldAutoBackfillCurrentBaseline(report)) {
          return;
        }
        if (state.validationBaselineAutoBackfillInFlight) {
          return;
        }
        const baseline = report?.baseline_status || {};
        const key = [
          strategy,
          days,
          baseline.validation_baseline_id || "",
          baseline.pending_current_baseline_count || 0,
          baseline.mismatched_baseline_outcome_count || 0,
        ].join(":");
        const now = Date.now();
        if (
          state.validationBaselineAutoBackfillKey === key
          && now - state.validationBaselineAutoBackfillAt < VALIDATION_AUTO_REFRESH_MS
        ) {
          return;
        }
        state.validationBaselineAutoBackfillInFlight = true;
        state.validationBaselineAutoBackfillKey = key;
        state.validationBaselineAutoBackfillAt = now;
        setOpsStatus(els.validationBaselineStatus, "检测到 current baseline 待回填，正在自动执行...", "pending");
        try {
          const params = new URLSearchParams({ strategy, days, execute: "1" });
          const res = await fetch(`/api/strategy-validation/backfill-current-baseline?${params.toString()}`, {
            method: "POST",
          });
          const payload = await res.json();
          if (!payload.ok) {
            throw new Error(payload.error || "current baseline 自动回填失败");
          }
          const afterOos = await fetchValidationOosReportPayloadSafe(new URLSearchParams({ strategy, days }));
          payload.before_oos = report;
          payload.after_oos = afterOos;
          if (requestSeq === state.validationRequestSeq && strategy === currentValidationStrategy()) {
            if (afterOos.ok) {
              renderValidationOosReport(afterOos);
              renderValidationPortfolioBaseline(afterOos.portfolio_baseline || {});
            }
            renderValidationBaselineBackfillResult(payload, true);
            const outcome = payload.outcome || {};
            const prefetch = payload.prefetch || {};
            setOpsStatus(
              els.validationBaselineStatus,
              `current baseline 自动回填完成：候选 ${Number(payload.candidates?.candidate_count || 0)}，更新 ${Number(outcome.updated || 0)}，下载 ${Number(prefetch.downloaded || 0)}`,
              "ok",
            );
            delete state.validationCache[`${strategy}:${days}`];
            state.validationDailyCache = {};
            await loadValidation({ silent: true, skipAutoOutcomeUpdate: true });
          }
        } catch (err) {
          setOpsStatus(els.validationBaselineStatus, `current baseline 自动回填失败：${escapeHtml(err.message)}`, "bad");
        } finally {
          state.validationBaselineAutoBackfillInFlight = false;
        }
      }

      // 就地操作反馈：在操作块下方的状态行显示进度/成功/失败。

      async function loadValidationAutoUpdateStatus() {
        try {
          const res = await fetch("/api/strategy-validation/auto-update-status");
          const payload = await res.json();
          if (!payload.ok) {
            return;
          }
          const status = payload.auto_update || {};
          const snapshot = payload.auto_snapshot || {};
          const config = status.config || {};
          const strategiesText = validationSnapshotStrategiesText(config.strategies);
          const snapshotText = snapshotStatusText(snapshot, config.strategies);
          if (!status.enabled) {
            setOpsStatus(els.updateStatus, joinStatusText(["验证自动更新已关闭", snapshotText]), "pending");
            return;
          }
          if (status.running) {
            setOpsStatus(els.updateStatus, joinStatusText([`正在更新${strategiesText}验证结果…`, snapshotText]), "pending");
            return;
          }
          const result = status.last_result || {};
          if (status.last_error) {
            setOpsStatus(els.updateStatus, joinStatusText([`验证自动更新上次失败：${status.last_error}`, snapshotText]), "bad");
            return;
          }
          if (status.last_finished_at) {
            const savedText = snapshotSaveText(result);
            setOpsStatus(
              els.updateStatus,
              joinStatusText([
                `验证自动更新 ${status.last_finished_at} 已完成${savedText ? `：${savedText}` : ""}`,
                snapshotText,
              ]),
              "ok"
            );
            return;
          }
          setOpsStatus(
            els.updateStatus,
            joinStatusText([
              `自动更新已启动：${config.start_time || "14:30"} 之后每 ${Math.round((config.interval_seconds || 0) / 60)} 分钟更新${strategiesText}验证结果`,
              snapshotText,
            ]),
            "pending"
          );
        } catch (err) {
          /* 状态提示不影响验证主流程 */
        }
      }

      function joinStatusText(parts) {
        return parts.filter(Boolean).join("；");
      }

      function snapshotStatusText(snapshot, configuredStrategies = []) {
        if (!snapshot || snapshot.enabled === false) {
          return "荐股快照自动保存已关闭";
        }
        if (snapshot.running) {
          const strategies = configuredStrategies.length
            ? configuredStrategies
            : snapshot.last_result?.strategies || [];
          return `正在保存${validationSnapshotStrategiesText(strategies)}快照`;
        }
        if (snapshot.last_error) {
          return `荐股快照自动保存上次失败：${snapshot.last_error}`;
        }
        const snapshots = snapshot.last_result?.snapshots || [];
        const savedParts = snapshots
          .filter(item => item && item.ok && item.saved)
          .map(item => `${strategyLabel(item.strategy)} ${item.saved.saved || 0}条`);
        if (savedParts.length) {
          const signalDate = snapshots.find(item => item?.saved?.signal_date)?.saved?.signal_date || "";
          const tuningText = snapshot.last_tuning_date
            ? `；DeepSeek复盘 ${snapshot.last_tuning_date} 已生成`
            : "";
          return `已自动保存 ${signalDate} ${savedParts.join(" / ")}${tuningText}`;
        }
        if (snapshot.next_run_at) {
          return `下次自动保存 ${snapshot.next_run_at}`;
        }
        return "荐股快照自动保存已启动";
      }

      function snapshotSaveText(result) {
        const snapshots = result?.snapshots || [];
        return snapshots
          .filter(item => item && item.ok && item.saved)
          .map(item => `${strategyLabel(item.strategy)} ${item.saved.saved || 0}条`)
          .join(" / ");
      }

      async function loadValidationDaily(date, strategy) {
        state.selectedValidation = { date, strategy };
        renderValidationSelection();
        markSelectedValidationRow();
        const cacheKey = `${strategy}:${date}`;
        const cached = state.validationDailyCache[cacheKey];
        if (cached) {
          renderValidationDetail(cached.data || [], cached.batch || null);
          renderValidationBatchSummary(cached.data || [], date, strategy, cached.summary || null);
        } else {
          els.validationDetailBody.innerHTML = '<tr><td colspan="11" class="empty">加载中...</td></tr>';
        }
        const params = new URLSearchParams({ date, strategy });
        const requestSeq = ++state.validationDailyRequestSeq;
        try {
          const res = await fetch(`/api/strategy-validation/daily?${params.toString()}`);
          const payload = await res.json();
          if (!payload.ok) {
            throw new Error(payload.error || "接口返回异常");
          }
          if (
            requestSeq !== state.validationDailyRequestSeq ||
            state.selectedValidation.date !== date ||
            state.selectedValidation.strategy !== strategy
          ) {
            return;
          }
          state.validationDailyCache[cacheKey] = payload;
          renderValidationDetail(payload.data || [], payload.batch || null);
          renderValidationBatchSummary(payload.data || [], date, strategy, payload.summary || null);
          if ((payload.data || []).length) {
            refreshValidationDailyQuotes(date, strategy, cacheKey);
          }
        } catch (err) {
          if (requestSeq !== state.validationDailyRequestSeq) {
            return;
          }
          els.validationDetailBody.innerHTML = `<tr><td colspan="11" class="empty">${escapeHtml(err.message)}</td></tr>`;
          renderValidationBatchSummary([], date, strategy);
        }
      }

      async function refreshValidationDailyQuotes(date, strategy, cacheKey) {
        const requestSeq = ++state.validationQuotesRequestSeq;
        const params = new URLSearchParams({ date, strategy, quotes: "1" });
        try {
          const res = await fetch(`/api/strategy-validation/daily?${params.toString()}`);
          const payload = await res.json();
          if (!payload.ok) {
            throw new Error(payload.error || "接口返回异常");
          }
          if (
            requestSeq !== state.validationQuotesRequestSeq ||
            state.selectedValidation.date !== date ||
            state.selectedValidation.strategy !== strategy
          ) {
            return;
          }
          state.validationDailyCache[cacheKey] = payload;
          patchValidationQuoteColumns(payload.data || []);
        } catch (err) {
          /* 实时行情补列失败不影响批次明细查看 */
        }
      }

      function formatSignedPct(value) {
        if (value == null || value === "") return "-";
        const num = Number(value);
        if (!Number.isFinite(num)) return "-";
        const sign = num > 0 ? "+" : "";
        return `${sign}${formatNumber(num, 2)}%`;
      }

      function renderValidationMetrics(metrics, validationGate = {}) {
        const strategy = metrics.strategy_name || currentValidationStrategy();
        const sample = Number(metrics.sample_count || 0);
        const outcome = Number(metrics.outcome_sample_count || 0);
        const replay = Number(metrics.replay_sample_count || 0);
        const realDayCount = Number(metrics.real_day_count || 0);
        const pendingOutcome = Number(metrics.pending_outcome_count || 0);
        const horizon = metrics.primary_horizon_label || "主周期";
        const winRateValue = metrics.real_win_rate_primary_net ?? metrics.win_rate_primary_net;
        const avgReturnValue = metrics.real_avg_primary_return_net ?? metrics.avg_primary_return_net;
        const winRate = winRateValue == null ? null : Number(winRateValue);
        const avgReturn = avgReturnValue == null ? null : Number(avgReturnValue);
        const drawdownValue = metrics.real_avg_max_drawdown_primary ?? metrics.avg_max_drawdown_primary;
        const drawdown = drawdownValue == null ? null : Number(drawdownValue);
        els.validationSampleCount.textContent = `${realDayCount}日 / ${sample}条`;
        els.validationWinRate.textContent = winRate != null ? `${formatNumber(winRate, 1)}%` : "-";
        els.validationAvgReturn.textContent = avgReturn != null ? `${horizon} ${formatNumber(avgReturn, 2)}%` : "-";
        ValidationUI.renderValidationSimpleDecision(
          els.validationSimpleDecision,
          {
            strategy, sample, outcome, replay, realDayCount, winRate, avgReturn, drawdown,
            horizon, pendingOutcome, validationGate,
          },
          { formatNumber },
        );
      }

      function renderValidationOosReport(report) {
        if (!els.validationOosReport) return;
        const summary = report.summary || {};
        const baseline = report.baseline_status || {};
        const gate = report.validation_gate || {};
        const blockers = Array.isArray(report.blockers) ? report.blockers : [];
        const status = report.oos_status || "unknown";
        const statusMeta = {
          oos_passed: ["oos-passed", "OOS 通过"],
          needs_backfill: ["oos-watch", "需回填当前口径"],
          empty: ["oos-watch", "暂无真实 OOS"],
          insufficient_oos_days: ["oos-watch", "OOS 天数不足"],
          gate_blocked: ["oos-blocked", "验证门控阻断"],
          portfolio_blocked: ["oos-blocked", "日级组合亏损阻断"],
        }[status] || ["oos-watch", "OOS 待观察"];
        const readyDays = Number(baseline.current_primary_ready_day_count || 0);
        const minDays = Number(baseline.min_oos_days || 0);
        const avgNet = summary.real_avg_primary_return_net ?? summary.avg_primary_return_net;
        const winRate = summary.real_win_rate_primary_net ?? summary.win_rate_primary_net;
        const ciLow = summary.real_avg_primary_return_net_ci95_low;
        const drawdown = summary.real_portfolio_max_drawdown_pct;
        const coverage = baseline.current_baseline_coverage_pct;
        const reason = gate.blocked && gate.reason ? ` · ${escapeHtml(gate.reason)}` : "";
        const blockerText = blockers.length
          ? ` · ${blockers.map(item => escapeHtml(item.message || item.code || "门槛未通过")).join(" / ")}`
          : "";
        const ciText = ciLow == null ? "" : ` · CI低 ${formatSignedPct(ciLow)}`;
        const drawdownText = drawdown == null ? "" : ` · 回撤 ${formatSignedPct(drawdown)}`;
        const coverageText = coverage == null ? "" : ` · 覆盖 ${formatNumber(coverage, 1)}%`;
        els.validationOosReport.className = `validation-oos-report ${statusMeta[0]}`;
        els.validationOosReport.innerHTML = `
          <strong>${statusMeta[1]}</strong>
          · ready ${readyDays}/${minDays || "-"}日
          · 净收益 ${formatSignedPct(avgNet)}
          · 净胜率 ${winRate == null ? "-" : `${formatNumber(winRate, 1)}%`}
          ${ciText}${drawdownText}${coverageText}${reason}${blockerText}
        `;
      }

      function renderValidationPortfolioBaseline(report) {
        if (!els.validationPortfolioBaseline) return;
        const groups = report.groups || {};
        const frozen = groups.frozen_rule_top_k || {};
        const random = groups.random_equal_weight || {};
        const index = groups.major_index || {};
        const current = groups.current_rule_top_k || {};
        const days = Number(report.day_count || 0);
        const percentile = report.rule_vs_random_percentile;
        const statusClass = days > 0 ? "oos-passed" : "oos-watch";
        els.validationPortfolioBaseline.className = `validation-oos-report ${statusClass}`;
        els.validationPortfolioBaseline.innerHTML = `
          <strong>日级组合基线</strong>
          · 配对 ${days}日
          · 冻结 Top-5 ${formatSignedPct(frozen.total_return_pct)}
          · 回撤 ${formatSignedPct(frozen.max_drawdown_pct)}
          · Sortino ${frozen.sortino == null ? "-" : formatNumber(frozen.sortino, 2)}
          · 随机分位 ${percentile == null ? "-" : `${formatNumber(percentile, 1)}%`}
          · 当前规则 ${formatSignedPct(current.total_return_pct)}
          · 随机 ${formatSignedPct(random.total_return_pct)}
          · 指数 ${formatSignedPct(index.total_return_pct)}
        `;
      }

      function baselineStatusText(status) {
        if (!status || !Object.keys(status).length) return "OOS -";
        const stateText = status.status || "-";
        const readyDays = Number(status.current_primary_ready_day_count || 0);
        const minDays = Number(status.min_oos_days || 0);
        const pending = Number(status.pending_current_baseline_count || 0);
        const mismatch = Number(status.mismatched_baseline_outcome_count || 0);
        const coverage = status.current_baseline_coverage_pct == null
          ? "-"
          : `${formatNumber(status.current_baseline_coverage_pct, 1)}%`;
        return `OOS ${stateText} · ready ${readyDays}/${minDays || "-"}日 · 待回填 ${pending} · 旧口径 ${mismatch} · 覆盖 ${coverage}`;
      }

      function oosReportStatusText(report) {
        if (!report || !Object.keys(report).length) return "";
        const summary = report.summary || {};
        const baseline = report.baseline_status || {};
        const status = report.oos_status || baseline.status || "-";
        const readyDays = Number(baseline.current_primary_ready_day_count || summary.real_day_count || 0);
        const minDays = Number(baseline.min_oos_days || report.requirements?.min_oos_days || 0);
        const avgNet = summary.real_avg_primary_return_net ?? summary.avg_primary_return_net;
        const winRate = summary.real_win_rate_primary_net ?? summary.win_rate_primary_net;
        const coverage = baseline.current_baseline_coverage_pct == null
          ? "-"
          : `${formatNumber(baseline.current_baseline_coverage_pct, 1)}%`;
        return `OOS ${status} · ready ${readyDays}/${minDays || "-"}日 · 净收益 ${formatSignedPct(avgNet)} · 净胜率 ${winRate == null ? "-" : `${formatNumber(winRate, 1)}%`} · 覆盖 ${coverage}`;
      }

      function renderValidationBaselineBackfillResult(payload, execute = false) {
        const candidates = payload.candidates || {};
        const before = payload.before_oos || {};
        const after = payload.after_oos || {};
        const candidateCount = Number(candidates.candidate_count || 0);
        const outcome = payload.outcome || {};
        const prefetch = payload.prefetch || {};
        const updatedText = execute
          ? ` · 更新 ${Number(outcome.updated || 0)} · 跳过 ${Number(outcome.skipped || 0)} · 下载 ${Number(prefetch.downloaded || 0)}`
          : "";
        const sampleCodes = (candidates.codes || [])
          .slice(0, 4)
          .map(item => `${item.code}${item.name ? ` ${item.name}` : ""}`)
          .join(" / ");
        renderToolResult(`
          <div class="baseline-backfill-result">
            <div class="baseline-backfill-head">
              <strong>${execute ? "current baseline execute" : "current baseline dry-run"}</strong>
              <span class="tag ${candidateCount ? "warning" : "stable"}">候选 ${candidateCount}</span>
            </div>
            <div class="baseline-backfill-lines">
              <div><span>回填前</span><strong>${escapeHtml(oosReportStatusText(before) || baselineStatusText(payload.before || {}))}</strong></div>
              <div><span>回填后</span><strong>${escapeHtml(oosReportStatusText(after) || baselineStatusText(payload.after || {}))}</strong></div>
              <div><span>执行结果</span><strong>${execute ? `已执行${updatedText}` : "未执行，仅预览候选"}</strong></div>
              <div><span>候选样本</span><strong>${escapeHtml(sampleCodes || "-")}</strong></div>
            </div>
          </div>
        `);
      }

      async function fetchValidationOosReportPayloadSafe(params) {
        try {
          return await fetchValidationOosReportPayload(params);
        } catch (err) {
          return {};
        }
      }

      async function runValidationBaselineBackfill(execute = false) {
        const strategy = currentValidationStrategy();
        const days = els.validationDaysSelect.value;
        if (execute) {
          const confirmed = window.confirm("确认执行 current baseline 回填？该操作会更新验证回填结果并刷新 OOS 状态。");
          if (!confirmed) {
            setOpsStatus(els.validationBaselineStatus, "已取消 execute 回填。", "pending");
            return;
          }
        }
        const dryRunLabel = els.validationBaselineDryRunBtn?.textContent || "";
        const executeLabel = els.validationBaselineExecuteBtn?.textContent || "";
        if (els.validationBaselineDryRunBtn) els.validationBaselineDryRunBtn.disabled = true;
        if (els.validationBaselineExecuteBtn) els.validationBaselineExecuteBtn.disabled = true;
        if (execute && els.validationBaselineExecuteBtn) {
          els.validationBaselineExecuteBtn.textContent = "回填中...";
        } else if (els.validationBaselineDryRunBtn) {
          els.validationBaselineDryRunBtn.textContent = "预览中...";
        }
        setOpsStatus(
          els.validationBaselineStatus,
          execute ? "正在执行 current baseline 回填..." : "正在 dry-run current baseline 候选...",
          "pending",
        );
        try {
          const params = new URLSearchParams({ strategy, days });
          if (execute) {
            params.set("execute", "1");
          }
          const beforeOos = await fetchValidationOosReportPayloadSafe(new URLSearchParams({
            strategy,
            days,
          }));
          const res = await fetch(`/api/strategy-validation/backfill-current-baseline?${params.toString()}`, {
            method: "POST",
          });
          const payload = await res.json();
          if (!payload.ok) {
            throw new Error(payload.error || "current baseline 回填失败");
          }
          const afterOos = await fetchValidationOosReportPayloadSafe(new URLSearchParams({
            strategy,
            days,
          }));
          payload.before_oos = beforeOos;
          payload.after_oos = afterOos;
          if (afterOos.ok && strategy === currentValidationStrategy() && days === els.validationDaysSelect.value) {
            renderValidationOosReport(afterOos);
          }
          renderValidationBaselineBackfillResult(payload, execute);
          const count = Number(payload.candidates?.candidate_count || 0);
          const before = oosReportStatusText(beforeOos) || baselineStatusText(payload.before || {});
          const after = oosReportStatusText(afterOos) || baselineStatusText(payload.after || {});
          setOpsStatus(
            els.validationBaselineStatus,
            `${execute ? "execute 完成" : "dry-run 完成"}：候选 ${count}；回填前 ${before}；回填后 ${after}`,
            "ok",
          );
          if (execute) {
            delete state.validationCache[`${strategy}:${days}`];
            state.validationDailyCache = {};
            await loadValidation({ silent: true, skipAutoOutcomeUpdate: true });
          }
        } catch (err) {
          setOpsStatus(els.validationBaselineStatus, `current baseline 操作失败：${err.message}`, "bad");
          renderToolResult(`
            <div class="prediction-empty">
              <strong>current baseline 操作失败</strong>
              <p>${escapeHtml(err.message)}</p>
            </div>
          `);
        } finally {
          if (els.validationBaselineDryRunBtn) {
            els.validationBaselineDryRunBtn.disabled = false;
            els.validationBaselineDryRunBtn.textContent = dryRunLabel;
          }
          if (els.validationBaselineExecuteBtn) {
            els.validationBaselineExecuteBtn.disabled = false;
            els.validationBaselineExecuteBtn.textContent = executeLabel;
          }
        }
      }

      function renderValidationBatchSummary(rows, date, strategy, summary = null) {
        const meta = ValidationUI.validationStrategyMeta(strategy, strategyLabel);
        const localSummary = summary || ValidationUI.validationBatchSummaryFromRows(rows, {
          primaryValidationNetReturn: ValidationRenderers.primaryValidationNetReturn.bind(ValidationRenderers),
          validationSkipReason: ValidationRenderers.validationSkipReason.bind(ValidationRenderers),
        });
        const sample = Number(localSummary.sample_count || 0);
        const up = Number(localSummary.up_count || 0);
        const down = Number(localSummary.down_count || 0);
        const flat = Number(localSummary.flat_count || 0);
        const pending = Number(localSummary.pending_count || 0);
        const winRate = localSummary.win_rate == null ? null : Number(localSummary.win_rate);
        const avgReturn = localSummary.avg_return == null ? null : Number(localSummary.avg_return);
        if (els.validationSelectionLabel) {
          els.validationSelectionLabel.textContent = date
            ? `${date} ${strategyLabel(strategy)}`
            : `未选择${meta.label}批次`;
        }
        if (els.validationSampleCount) {
          els.validationSampleCount.textContent = pending > 0 ? `${sample}（待回填${pending}）` : `${sample}`;
        }
        if (els.validationWinRate) {
          const flatText = flat > 0 ? ` / 平${flat}` : "";
          els.validationWinRate.textContent = winRate == null
            ? (pending > 0 ? `-（待回填${pending}）` : "-")
            : `${formatNumber(winRate, 1)}%（涨${up} / 跌${down}${flatText}）`;
        }
        if (els.validationAvgReturn) {
          els.validationAvgReturn.textContent = avgReturn == null ? "-" : `${formatNumber(avgReturn, 2)}%`;
          els.validationAvgReturn.className = avgReturn == null ? "" : numberClass(avgReturn);
        }
      }

      function renderValidationDates(rows) {
        state.validationDateRows = rows || [];
        if (!rows.length) {
          els.validationDatesBody.innerHTML = '<tr><td colspan="4" class="empty">暂无保存记录</td></tr>';
          els.validationDetailBody.innerHTML = '<tr><td colspan="11" class="empty">暂无可查看明细</td></tr>';
          state.selectedValidation = { date: "", strategy: currentValidationStrategy() };
          renderValidationBatchSummary([], "", currentValidationStrategy());
          updateValidationDatesPager();
          return;
        }
        clampValidationDatePage();
        renderValidationDatePage();
      }

      function validationDatePageCount() {
        return ValidationRenderers.validationDatePageCount(state.validationDateRows.length, VALIDATION_DATE_PAGE_SIZE);
      }

      function clampValidationDatePage() {
        state.validationDatePage = ValidationRenderers.clampValidationDatePage(
          state.validationDatePage,
          state.validationDateRows.length,
          VALIDATION_DATE_PAGE_SIZE,
        );
      }

      function renderValidationDatePage() {
        clampValidationDatePage();
        const start = state.validationDatePage * VALIDATION_DATE_PAGE_SIZE;
        const pageRows = state.validationDateRows.slice(start, start + VALIDATION_DATE_PAGE_SIZE);
        els.validationDatesBody.innerHTML = ValidationRenderers.renderValidationDatePageRows(pageRows, { escapeHtml });
        [...els.validationDatesBody.querySelectorAll("tr")].forEach(row => {
          row.addEventListener("click", () => loadValidationDaily(row.dataset.date, row.dataset.strategy));
        });
        markSelectedValidationRow();
        updateValidationDatesPager();
      }

      function updateValidationDatesPager() {
        if (!els.validationDatesPager) return;
        const totalRows = state.validationDateRows.length;
        const totalPages = validationDatePageCount();
        const hasMultiplePages = totalRows > VALIDATION_DATE_PAGE_SIZE;
        els.validationDatesPager.hidden = !totalRows;
        if (els.validationDatesPageLabel) {
          els.validationDatesPageLabel.textContent = totalRows
            ? `${state.validationDatePage + 1}/${totalPages} 共${totalRows}条`
            : "0/0";
        }
        if (els.validationDatesPrev) {
          els.validationDatesPrev.disabled = !hasMultiplePages || state.validationDatePage <= 0;
        }
        if (els.validationDatesNext) {
          els.validationDatesNext.disabled = !hasMultiplePages || state.validationDatePage >= totalPages - 1;
        }
      }

      function moveValidationDatePage(delta) {
        state.validationDatePage += delta;
        clampValidationDatePage();
        renderValidationDatePage();
      }

      function syncValidationDatePageToSelection() {
        const index = state.validationDateRows.findIndex(row =>
          row.signal_date === state.selectedValidation.date &&
          row.strategy_name === state.selectedValidation.strategy
        );
        if (index < 0) return;
        const nextPage = Math.floor(index / VALIDATION_DATE_PAGE_SIZE);
        if (nextPage !== state.validationDatePage) {
          state.validationDatePage = nextPage;
          renderValidationDatePage();
        } else {
          markSelectedValidationRow();
          updateValidationDatesPager();
        }
      }

      function getValidationDateRow(date, strategy) {
        return state.validationDateRows.find(item =>
          item.signal_date === (date || "") &&
          item.strategy_name === (strategy || "")
        ) || null;
      }

      function renderValidationDetail(rows, batch = null) {
        if (!rows.length) {
          const selectedBatch = batch || getValidationDateRow(state.selectedValidation.date, state.selectedValidation.strategy);
          const emptyText = selectedBatch && selectedBatch.sample_type === "empty"
            ? "空批次（无可执行样本）"
            : "暂无明细";
          els.validationDetailBody.innerHTML = `<tr><td colspan="11" class="empty">${escapeHtml(emptyText)}</td></tr>`;
          return;
        }
        els.validationDetailBody.innerHTML = ValidationRenderers.renderValidationDetailRows(rows, {
          escapeHtml,
          formatNumber,
          numberClass,
        });
      }

      function patchValidationQuoteColumns(rows) {
        const lookup = new Map((rows || []).map(row => [String(row.code || ""), row]));
        [...els.validationDetailBody.querySelectorAll("tr[data-code]")].forEach(tr => {
          const row = lookup.get(String(tr.dataset.code || ""));
          if (!row) return;
          ValidationRenderers.updateValidationPctCell(
            tr.querySelector('[data-validation-field="current_pct_chg"]'),
            row.current_pct_chg,
            { numberClass, formatNumber },
          );
          ValidationRenderers.updateValidationPctCell(
            tr.querySelector('[data-validation-field="anchor_to_now_return"]'),
            row.anchor_to_now_return,
            { numberClass, formatNumber },
          );
        });
      }

      function syncValidationSelection(rows) {
        const exists = rows.some(row =>
          row.signal_date === state.selectedValidation.date &&
          row.strategy_name === state.selectedValidation.strategy
        );
        if (!exists) {
          const first = rows[0];
          state.selectedValidation = first
            ? { date: first.signal_date, strategy: first.strategy_name }
            : { date: "", strategy: "" };
        }
        renderValidationSelection();
        syncValidationDatePageToSelection();
        if (state.selectedValidation.date && state.selectedValidation.strategy) {
          loadValidationDaily(state.selectedValidation.date, state.selectedValidation.strategy);
        }
      }

      function renderValidationSelection() {
        if (!state.selectedValidation.date) {
          els.validationSelectionLabel.textContent = `未选择${strategyLabel(currentValidationStrategy())}批次`;
          return;
        }
        const batch = getValidationDateRow(state.selectedValidation.date, state.selectedValidation.strategy);
        const batchSuffix = batch && batch.sample_type === "empty" ? "（空批次）" : "";
        els.validationSelectionLabel.textContent = `${state.selectedValidation.date} ${strategyLabel(state.selectedValidation.strategy)}${batchSuffix}`;
      }

      function markSelectedValidationRow() {
        [...els.validationDatesBody.querySelectorAll("tr")].forEach(row => {
          row.classList.toggle(
            "selected",
            row.dataset.date === state.selectedValidation.date &&
            row.dataset.strategy === state.selectedValidation.strategy
          );
        });
      }

      function invalidateStrategyValidationReportRequest() {
        state.validationReportRequestSeq = Number(state.validationReportRequestSeq || 0) + 1;
        if (els.generateTuningBtn) {
          els.generateTuningBtn.disabled = false;
          els.generateTuningBtn.textContent = "策略验证";
        }
        if (els.stockPredictionBtn) els.stockPredictionBtn.disabled = false;
      }

      function resetValidationView() {
        invalidateStrategyValidationReportRequest();
        state.selectedValidation = { date: "", strategy: "" };
        state.validationAutoRefreshDate = "";
        state.validationDatePage = 0;
        setOpsStatus(els.validationBaselineStatus, "", "");
        setOpsStatus(els.tuningStatus, "", "");
        renderToolResult('<div class="empty">点击左侧按钮后在这里显示结果</div>');
      }

      function selectValidationStrategy(strategy) {
        if (els.validationStrategySelect) {
          els.validationStrategySelect.value = strategy || "today_term";
        }
        resetValidationView();
        loadValidation();
      }

      function handleValidationStrategyChange() {
        resetValidationView();
        loadValidation();
      }

      function handleValidationDaysChange() {
        invalidateStrategyValidationReportRequest();
        state.validationDatePage = 0;
        setOpsStatus(els.validationBaselineStatus, "", "");
        setOpsStatus(els.tuningStatus, "", "");
        renderToolResult('<div class="empty">点击左侧按钮后在这里显示结果</div>');
        loadValidation();
      }

      return {
        handleValidationDaysChange,
        handleValidationStrategyChange,
        loadStrategyValidationReport,
        loadValidation,
        moveValidationDatePage,
        runValidationBaselineBackfill,
        selectValidationStrategy,
        startValidationAutoRefreshLoop,
        stopValidationAutoRefreshLoop,
      };
    },
  };
})();
