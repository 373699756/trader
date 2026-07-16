(function () {
  window.TraderRecommendationApp = {
  create(context) {
      const { state, els, helpers, config, status } = context;
      const { DEFAULT_ACTION_FILTER, DEFAULT_MARKET, DEFAULT_SORT_MODE } = config;
      const { escapeHtml, formatMoney, formatNumber, hasRows, rememberFingerprint } = helpers;
      const { renderMetrics, setStatus, startPushStatusCountdown } = status;
      const RecommendationUtils = window.TraderRecommendationUtils;
      const RecommendationRenderers = window.TraderRecommendationRenderers;
      const RecommendationTables = window.TraderRecommendationTables;
      const DEFAULT_LONG_TERM_TOP_N = Number((window.APP_CONFIG || {}).defaultTopN || 18);
      const LONG_TERM_LONG_TERM_SOURCE_ORDER = ["today", "tomorrow"];
      const LONG_TERM_LONG_HORIZON = {
        weights: {
          value: 0.45,
          growth: 0.35,
          support: 0.20,
        },
        themeGroups: {
          value: ["低估", "低估值", "估值修复", "价值", "pe", "pb", "低位", "低价", "折价", "便宜"],
          chokepoint: ["卡脖子", "卡脖子产业", "核心零部件", "关键", "替代", "自主", "国产化", "供应链", "技术突破", "产业链安全", "产业链", "安全"],
          growth: ["成长", "高成长", "景气", "扩张", "增速", "高增长", "需求复苏", "业绩", "业绩提升", "国产替代", "龙头"],
          support: ["国家", "政策", "扶持", "补贴", "基金", "入局", "增持", "战略", "专项", "战略性", "纳入", "支持"],
        },
        valueHints: {
          valueWords: ["低估", "估值", "价值", "低位", "低价", "折价", "便宜"],
          qualityWords: ["质量", "经营", "现金流", "负债", "ROE", "roe", "毛利", "盈利能力"],
        },
        growthHints: {
          growthWords: ["增长", "扩张", "景气", "上升", "复苏", "回暖", "国产替代", "龙头", "业绩"],
          longCycleWords: ["中长期", "未来", "1-5", "多年", "三五年", "五年"],
        },
        supportHints: {
          supportWords: ["政策", "扶持", "专项", "战略", "受益", "入局", "增持", "基金", "资金", "订单", "上车", "国家"],
          securityWords: ["卡脖子", "关键", "替代", "自主", "国产化", "供应链", "产业链", "安全"],
        },
        thresholds: {
          predictedFloor: 1.0,
          riskLimit: 90,
          potentialFloor: 0.34,
          themeGroupHitFloor: 1,
        },
      };

      function longTermToNumber(value) {
        const num = Number(value);
        return Number.isFinite(num) ? num : null;
      }

      function longTermNormalize(value, min, max, betterHigher) {
        if (value == null || min === max) return null;
        if (betterHigher) {
          return Math.max(0, Math.min(1, (value - min) / (max - min)));
        }
        return Math.max(0, Math.min(1, (max - value) / (max - min)));
      }

      function longTermTextBag(row) {
        return [
          row?.name,
          row?.industry,
          row?.theme,
          row?.sub_theme,
          row?.reason,
          ...(Array.isArray(row?.reasons) ? row.reasons : []),
          row?.deepseek_features?.event_type,
          row?.deepseek_features?.reason,
          row?.deepseek_features?.evidence_summary,
          row?.notes,
          row?.summary,
          row?.note,
        ]
          .filter((value) => value != null)
          .map((value) => String(value).toLowerCase())
          .join(" ");
      }

      function longTermThemeSignalMatches(row, bagText = "") {
        const text = (bagText || longTermTextBag(row));
        const groups = {};
        const groupEntries = Object.entries(LONG_TERM_LONG_HORIZON.themeGroups || {});
        for (const [groupName, words] of groupEntries) {
          if (!Array.isArray(words)) continue;
          const hitWords = [];
          for (const rawWord of words) {
            const normalizedWord = String(rawWord || "").trim().toLowerCase();
            if (!normalizedWord || !text.includes(normalizedWord)) continue;
            hitWords.push(normalizedWord);
          }
          if (!hitWords.length) continue;
          groups[groupName] = {
            matched: true,
            words: Array.from(new Set(hitWords)),
          };
        }
        return { groups, count: Object.keys(groups).length };
      }

      function longTermContainsAny(text, words) {
        return (words || []).some((word) => text.includes(String(word).toLowerCase()));
      }

      function longTermValuePotential(row, bagText) {
        const fundamentalValue = longTermToNumber(row.fundamental_value_score);
        const valueFactor = fundamentalValue == null ? null : longTermNormalize(fundamentalValue, 0, 100, true);
        const qualityFactor = longTermNormalize(longTermToNumber(row.fundamental_quality_score), 0, 100, true);
        const pe = longTermToNumber(row.pe_dynamic ?? row.pe);
        const pb = longTermToNumber(row.pb);
        const roe = longTermToNumber(row.roe ?? (row.fundamentals && row.fundamentals.roe));

        let score = 0;
        if (valueFactor != null) score += 0.52 * valueFactor;
        if (qualityFactor != null) score += 0.2 * qualityFactor;
        if (pe != null && pe > 0) score += 0.16 * longTermNormalize(pe, 0, 50, false);
        if (pb != null && pb > 0) score += 0.09 * longTermNormalize(pb, 0.2, 8, false);
        if (roe != null) score += 0.03 * Math.max(0, Math.min(1, (roe / 30)));

        if (valueFactor == null && qualityFactor == null) {
          if (longTermContainsAny(bagText, LONG_TERM_LONG_HORIZON.valueHints.valueWords)) score += 0.25;
          if (longTermContainsAny(bagText, LONG_TERM_LONG_HORIZON.valueHints.qualityWords)) score += 0.1;
        }
        const reasons = [];
        if (valueFactor != null) reasons.push(`估值修复线索${Math.round(valueFactor * 100)}分`);
        if (qualityFactor != null) reasons.push(`经营质量${Math.round(qualityFactor * 100)}分`);
        if (pe != null && pe > 0) reasons.push(`PE${Math.round(Math.min(100, pe) * 100) / 100}`);
        if (pb != null && pb > 0) reasons.push(`PB${Math.round(Math.min(20, pb) * 100) / 100}`);
        return { score: Math.min(1, score), reasons };
      }

      function longTermGrowthPotential(row, bagText) {
        const revYoy = longTermToNumber(row.revenue_yoy ?? (row.fundamentals && row.fundamentals.revenue_yoy));
        const profitYoy = longTermToNumber(row.net_profit_yoy ?? (row.fundamentals && row.fundamentals.net_profit_yoy));
        const revScore = revYoy == null ? null : longTermNormalize(revYoy, -20, 60, true);
        const profitScore = profitYoy == null ? null : longTermNormalize(profitYoy, -20, 60, true);
        const ytd = longTermToNumber(row.ytd_pct);
        const sixty = longTermToNumber(row.sixty_day_pct);
        const ytdScore = ytd == null ? null : longTermNormalize(ytd, -60, 45, true);
        const sixtyScore = sixty == null ? null : longTermNormalize(sixty, -30, 30, true);
        const vol = longTermToNumber(row.volatility_20d);
        const volScore = vol == null ? null : longTermNormalize(vol, 8, 55, false);
        const industryGrowth = longTermToNumber(row.industry_revenue_growth);

        let score = 0;
        if (revScore != null) score += 0.26 * revScore;
        if (profitScore != null) score += 0.22 * profitScore;
        if (industryGrowth != null) score += 0.12 * longTermNormalize(industryGrowth, -10, 60, true);
        if (ytdScore != null) score += 0.18 * ytdScore;
        if (sixtyScore != null) score += 0.12 * sixtyScore;
        if (volScore != null) score += 0.1 * volScore;

        if (longTermContainsAny(bagText, LONG_TERM_LONG_HORIZON.growthHints.growthWords)) score += 0.15;
        if (longTermContainsAny(bagText, LONG_TERM_LONG_HORIZON.growthHints.longCycleWords)) score += 0.08;
        const reasons = [];
        if (revScore != null) reasons.push(`营收增速${Math.round(revScore * 100)}分`);
        if (profitScore != null) reasons.push(`利润增速${Math.round(profitScore * 100)}分`);
        if (industryGrowth != null) reasons.push(`景气${Math.round(longTermNormalize(industryGrowth, -10, 60, true) * 100)}分`);
        if (ytdScore != null) reasons.push(`YTD${Math.round(ytdScore * 100)}分`);
        if (sixtyScore != null) reasons.push(`60日${Math.round(sixtyScore * 100)}分`);
        return { score: Math.min(1, score), reasons };
      }

      function longTermSupportPotential(row, bagText) {
        const themeScore = longTermNormalize(longTermToNumber(row.theme_score), 0, 100, true);
        const industryScore = longTermNormalize(longTermToNumber(row.industry_score), 0, 100, true);
        let score = 0;

        if (themeScore != null) score += 0.28 * themeScore;
        if (industryScore != null) score += 0.22 * industryScore;

        if (longTermContainsAny(bagText, LONG_TERM_LONG_HORIZON.supportHints.supportWords)) score += 0.28;
        if (longTermContainsAny(bagText, LONG_TERM_LONG_HORIZON.supportHints.securityWords)) score += 0.22;
        const reasons = [];
        if (themeScore != null) reasons.push(`主题景气${Math.round(themeScore * 100)}分`);
        if (industryScore != null) reasons.push(`行业景气${Math.round(industryScore * 100)}分`);
        return { score: Math.min(1, score), reasons };
      }

      function longTermLongTermPotentialScore(row) {
        const bagText = longTermTextBag(row);
        const value = longTermValuePotential(row, bagText);
        const growth = longTermGrowthPotential(row, bagText);
        const support = longTermSupportPotential(row, bagText);
        const themeSignals = longTermThemeSignalMatches(row, bagText);
        const longTermPotential = value.score * LONG_TERM_LONG_HORIZON.weights.value
          + growth.score * LONG_TERM_LONG_HORIZON.weights.growth
          + support.score * LONG_TERM_LONG_HORIZON.weights.support;
        return {
          bagText,
          valueScore: Math.round(value.score * 1000) / 1000,
          growthScore: Math.round(growth.score * 1000) / 1000,
          supportScore: Math.round(support.score * 1000) / 1000,
          valueReasons: value.reasons,
          growthReasons: growth.reasons,
          supportReasons: support.reasons,
          longTermPotential: Math.round(longTermPotential * 1000) / 1000,
          themeSignals: themeSignals.groups,
          themeSignalCount: themeSignals.count,
        };
      }

      function normalizeRowsForLongTerm(rows) {
        const safeRows = Array.isArray(rows) ? rows : [];
        const seen = new Map();
        for (const row of safeRows) {
          const code = String(row?.code || "").trim();
          if (!code) continue;
          const current = seen.get(code);
          if (!current) {
            seen.set(code, row);
            continue;
          }
          const currentPred = Number(current?.predicted_net_return ?? current?.expected_return_net);
          const nextPred = Number(row?.predicted_net_return ?? row?.expected_return_net);
          if (!Number.isFinite(nextPred) || (Number.isFinite(currentPred) && nextPred <= currentPred)) continue;
          seen.set(code, row);
        }
        return Array.from(seen.values());
      }

      function longTermSeedRowsFromRecommendations({ shortTerm = [], tomorrow = [], swing = [] }) {
        const rows = [];
        for (const sourceName of LONG_TERM_LONG_TERM_SOURCE_ORDER) {
          let sourceRows = [];
          if (sourceName === "today") {
            sourceRows = shortTerm;
          } else if (sourceName === "tomorrow") {
            sourceRows = tomorrow;
          } else if (sourceName === "swing") {
            sourceRows = swing;
          }
          if (!Array.isArray(sourceRows)) continue;
          rows.push(...sourceRows);
        }
        return normalizeRowsForLongTerm(rows);
      }

      function longTermCandidateRows(rows) {
        const candidates = Array.isArray(rows) ? rows : [];
        const scored = candidates
          .map(row => {
            const predicted = Number(row.predicted_net_return ?? row.expected_return_net);
            const longTermPotential = longTermLongTermPotentialScore(row);
            const stableRisk = Number(row.sell_risk?.score ?? row.serenity_profile?.risk_score ?? row.avg_risk ?? 100);
            const todayPct = Number(row.pct_chg ?? 0);
            const longTermProfile = {
              valueScore: longTermPotential.valueScore,
              growthScore: longTermPotential.growthScore,
              supportScore: longTermPotential.supportScore,
              longTermPotential: longTermPotential.longTermPotential,
              themeSignals: longTermPotential.themeSignals || {},
              themeSignalCount: longTermPotential.themeSignalCount,
              valueReasons: longTermPotential.valueReasons,
              growthReasons: longTermPotential.growthReasons,
              supportReasons: longTermPotential.supportReasons,
            };
            return {
              row: {
                ...row,
                longTermProfile,
              },
              predicted: Number.isFinite(predicted) ? predicted : Number.NEGATIVE_INFINITY,
              todayPct,
              stableRisk,
              longTermPotential: longTermPotential.longTermPotential,
              valueScore: longTermPotential.valueScore,
              growthScore: longTermPotential.growthScore,
              supportScore: longTermPotential.supportScore,
              themeSignalCount: longTermPotential.themeSignalCount,
            };
          })
          .filter(item => item.longTermPotential >= LONG_TERM_LONG_HORIZON.thresholds.potentialFloor
            && item.stableRisk <= LONG_TERM_LONG_HORIZON.thresholds.riskLimit
            && item.predicted >= LONG_TERM_LONG_HORIZON.thresholds.predictedFloor
            && item.themeSignalCount >= LONG_TERM_LONG_HORIZON.thresholds.themeGroupHitFloor)
          .sort((left, right) => {
            if (right.longTermPotential !== left.longTermPotential) return right.longTermPotential - left.longTermPotential;
            if (right.predicted !== left.predicted) return right.predicted - left.predicted;
            if (right.todayPct !== left.todayPct) return right.todayPct - left.todayPct;
            return left.stableRisk - right.stableRisk;
          });

        if (scored.length) {
          return scored.slice(0, DEFAULT_LONG_TERM_TOP_N).map(item => item.row);
        }

        return candidates
          .map(row => {
            const predicted = Number(row.predicted_net_return ?? row.expected_return_net);
            const longTermPotential = longTermLongTermPotentialScore(row);
            const todayPct = Number(row.pct_chg ?? 0);
            const longTermProfile = {
              valueScore: longTermPotential.valueScore,
              growthScore: longTermPotential.growthScore,
              supportScore: longTermPotential.supportScore,
              longTermPotential: longTermPotential.longTermPotential,
              themeSignals: longTermPotential.themeSignals || {},
              themeSignalCount: longTermPotential.themeSignalCount,
              valueReasons: longTermPotential.valueReasons,
              growthReasons: longTermPotential.growthReasons,
              supportReasons: longTermPotential.supportReasons,
            };
            return {
              row: {
                ...row,
                longTermProfile,
              },
              predicted: Number.isFinite(predicted) ? predicted : Number.NEGATIVE_INFINITY,
              todayPct,
              themeSignalCount: longTermPotential.themeSignalCount,
              longTermPotential: longTermPotential.longTermPotential,
            };
          })
          .filter(item => item.themeSignalCount >= LONG_TERM_LONG_HORIZON.thresholds.themeGroupHitFloor)
          .sort((left, right) => {
            if (right.predicted !== left.predicted) return right.predicted - left.predicted;
            return right.todayPct - left.todayPct;
          })
          .slice(0, DEFAULT_LONG_TERM_TOP_N)
          .map(item => item.row);
      }

      function payloadMarketTimestamp(payload) {
        const values = [
          payload.meta?.quote_timestamp,
          payload.meta?.data_source_timestamp,
          payload.health?.last_quote_refresh,
          payload.meta?.generated_at,
          payload.snapshot?.saved_at,
        ];
        for (const value of values) {
          const timestamp = Date.parse(String(value || ""));
          if (Number.isFinite(timestamp)) return timestamp;
        }
        return 0;
      }

      function snapshotPhaseLabel(payload) {
        const phase = String(payload?.meta?.snapshot_phase || payload?.snapshot?.snapshot_phase || "");
        if (phase === "preclose_tradeable") return "盘中冻结";
        if (phase === "close_fallback") return "收盘补充";
        if (phase === "mixed") return "阶段混合";
        return "";
      }

      function recommendationStatusText(payload, fallbackPrefix = "推荐更新") {
        const phaseLabel = snapshotPhaseLabel(payload);
        const quoteAt = payload.meta?.quote_timestamp || payload.health?.last_quote_refresh;
        const generatedAt = payload.meta?.as_of || payload.meta?.generated_at || payload.snapshot?.saved_at || "最近快照";
        const phaseSuffix = phaseLabel ? ` · ${phaseLabel}` : "";
        if (quoteAt) {
          return `行情更新 ${generatedAt} · 排名 ${quoteAt}${phaseSuffix}`;
        }
        return `${fallbackPrefix} ${generatedAt}${phaseSuffix}`;
      }

      function applyRecommendationsPayload(payload) {
        if (!payload.ok) {
          throw new Error(payload.error || "接口返回异常");
        }
        const marketTimestamp = payloadMarketTimestamp(payload);
        if (marketTimestamp && state.recommendationDataTimestamp && marketTimestamp < state.recommendationDataTimestamp) {
          return false;
        }
        const recommendations = payload.recommendations || {};
        const hasToday = Object.prototype.hasOwnProperty.call(recommendations, "today_term");
        const shortTerm = recommendations.today_term || payload.data || [];
        const hasTomorrow = Object.prototype.hasOwnProperty.call(recommendations, "tomorrow_picks");
        const hasSwing = Object.prototype.hasOwnProperty.call(recommendations, "swing_picks");
        const tomorrow = hasTomorrow ? (recommendations.tomorrow_picks || []) : state.lastRows.tomorrow;
        const swing = hasSwing ? (recommendations.swing_picks || []) : state.lastRows.swing;
        const hasLongTerm = Object.prototype.hasOwnProperty.call(recommendations, "long_term_watch");
        const swingLongTerm = hasLongTerm
          ? (recommendations.long_term_watch || [])
          : (state.lastRows.swingLongTerm || []);
        const marketRegime = payload.meta?.market_regime || {};
        const shouldRenderTables = rememberFingerprint("recommendations", {
          shortTerm,
          tomorrow,
          swing,
          swingLongTerm,
          marketRegime,
        });
        state.lastRows.shortTerm = hasToday ? shortTerm : (state.lastRows.shortTerm || payload.data || []);
        state.lastRows.tomorrow = tomorrow;
        state.lastRows.swing = hasSwing ? swing : state.lastRows.swing;
        state.lastRows.swingLongTerm = swingLongTerm;
        state.tomorrowLoaded = state.tomorrowLoaded || hasTomorrow;
        state.horizonLoaded = state.horizonLoaded || hasSwing;
        state.recommendationHasPayload = true;
        if (marketTimestamp) state.recommendationDataTimestamp = marketTimestamp;
        state.marketRegime = marketRegime;
        renderMetrics(payload);
        if (shouldRenderTables) {
          rerenderCurrentTables();
        }
        if (!hasTomorrow || !hasSwing) {
          prefetchRecommendationPools();
        }
        if (shouldRenderTables) {
          setStatus(recommendationStatusText(payload));
        }
        return true;
      }

      async function loadRecommendations(options = {}) {
        const requestSeq = options.requestSeq || ++state.recommendationRequestSeq;
        const background = Boolean(options.background);
        if (!background && !state.recommendationHasPayload) {
          setStatus("刷新中...");
        }
        const params = new URLSearchParams({
          top_n: String((window.APP_CONFIG || {}).defaultTopN || 18),
          market: DEFAULT_MARKET,
          _: String(Date.now()),
        });
        try {
          const res = await fetch(`/api/recommendations?${params.toString()}`, { cache: "no-store" });
          const payload = await res.json();
          if (requestSeq !== state.recommendationRequestSeq) return false;
          return applyRecommendationsPayload(payload);
        } catch (err) {
          if (requestSeq !== state.recommendationRequestSeq) return false;
          if (!state.recommendationHasPayload) {
          const message = `<tr><td colspan="10" class="empty">${escapeHtml(err.message)}</td></tr>`;
            els.shortTermBody.innerHTML = message;
          }
          if (!background) setStatus(`刷新失败：${err.message}`);
          return false;
        }
      }

      async function loadLatestRecommendationSnapshot(requestSeq) {
        const params = new URLSearchParams({
          top_n: String((window.APP_CONFIG || {}).defaultTopN || 18),
          market: DEFAULT_MARKET,
          max_age: String((window.APP_CONFIG || {}).recommendationSnapshotMaxAgeSeconds || 300),
        });
        try {
          const res = await fetch(`/api/recommendations/latest?${params.toString()}`);
          if (!res.ok) {
            return false;
          }
          const payload = await res.json();
          if (requestSeq !== state.recommendationRequestSeq) return false;
          if (!applyRecommendationsPayload(payload)) return false;
          return true;
        } catch (err) {
          return false;
        }
      }

      function stopRecommendationStream(options = {}) {
        if (options.invalidate !== false) state.recommendationRequestSeq += 1;
        if (state.eventSource) {
          state.eventSource.close();
          state.eventSource = null;
        }
        if (state.streamRetryTimer) {
          clearTimeout(state.streamRetryTimer);
          state.streamRetryTimer = null;
        }
        if (state.timer) {
          clearInterval(state.timer);
          state.timer = null;
        }
      }

      function connectRecommendationStream() {
        stopRecommendationStream({ invalidate: false });
        state.countdown = (window.APP_CONFIG || {}).refreshSeconds || 0;
        if (!state.recommendationHasPayload) {
          setStatus("正在连接实时推荐流...");
        }
        startPushStatusCountdown();
        state.timer = setInterval(() => {
          void loadRecommendations({ background: true });
        }, Math.max(60, (window.APP_CONFIG || {}).refreshSeconds || 60) * 1000);
        if (!("EventSource" in window)) {
          setStatus("浏览器不支持实时推送，已使用60秒轮询");
          return;
        }
        const params = new URLSearchParams({
          top_n: String((window.APP_CONFIG || {}).defaultTopN || 18),
          market: DEFAULT_MARKET,
        });
        const eventSource = new EventSource(`/api/recommendations/stream?${params.toString()}`);
        state.eventSource = eventSource;
        eventSource.onopen = () => {
          if (state.eventSource !== eventSource) return;
          if (els.streamStatus) {
            els.streamStatus.textContent = "实时";
            els.streamStatus.dataset.state = "live";
          }
        };
        eventSource.addEventListener("recommendations", event => {
          if (state.eventSource !== eventSource) return;
          try {
            const payload = JSON.parse(event.data);
            if (!applyRecommendationsPayload(payload)) return;
            const quoteAt = payload.meta?.quote_timestamp || payload.health?.last_quote_refresh || "";
            if (els.streamQuoteTime) els.streamQuoteTime.textContent = String(quoteAt).replace("T", " ");
            if (els.streamStatus) {
              const now = new Date();
              const minutes = now.getHours() * 60 + now.getMinutes();
              const marketOpen = now.getDay() >= 1 && now.getDay() <= 5
                && ((minutes >= 555 && minutes <= 695) || (minutes >= 780 && minutes <= 910));
              const quoteAge = Number(payload.meta?.quote_p95_age_seconds ?? payload.meta?.quote_age_seconds);
              const staleCount = Number(payload.meta?.quote_stale_count || 0);
              const missingCount = Number(payload.meta?.quote_missing_count || 0);
              const mismatchCount = Number(payload.meta?.quote_source_mismatch_count || 0);
              const poolLive = payload.meta?.quote_scope === "recommendation_pool";
              if (!marketOpen && poolLive) {
                els.streamStatus.textContent = "休市最新";
                els.streamStatus.dataset.state = "live";
              } else if (poolLive && staleCount === 0 && missingCount === 0 && mismatchCount === 0
                && Number.isFinite(quoteAge) && quoteAge <= 30) {
                els.streamStatus.textContent = "实时";
                els.streamStatus.dataset.state = "live";
              } else {
                const issueCount = staleCount + missingCount + mismatchCount;
                els.streamStatus.textContent = issueCount > 0
                  ? `${issueCount}只异常`
                  : (Number.isFinite(quoteAge) ? `延迟 ${Math.round(quoteAge)}s` : "回退");
                els.streamStatus.dataset.state = "delayed";
              }
            }
          } catch (err) {
            setStatus(`实时数据异常: ${err.message}`);
          }
        });
        eventSource.onerror = () => {
          if (state.eventSource !== eventSource || !els.streamStatus) return;
          els.streamStatus.textContent = "重连中";
          els.streamStatus.dataset.state = "connecting";
        };
      }

      async function startRecommendationStreamWithSnapshot() {
        stopRecommendationStream();
        const requestSeq = ++state.recommendationRequestSeq;
        let needFreshData = false;
        if (!state.recommendationHasPayload) {
          const hasFreshSnapshot = await loadLatestRecommendationSnapshot(requestSeq);
          if (requestSeq !== state.recommendationRequestSeq) return;
          needFreshData = !hasFreshSnapshot;
        }
        if (requestSeq !== state.recommendationRequestSeq) return;
        if (needFreshData) {
          await loadRecommendations({ requestSeq, background: false });
          if (requestSeq !== state.recommendationRequestSeq) return;
        }
        if (requestSeq !== state.recommendationRequestSeq) return;
        connectRecommendationStream();
      }

      async function loadTomorrowPicks(options = {}) {
        if (state.tomorrowLoading) {
          return state.tomorrowLoading;
        }
        state.tomorrowLoaded = true;
        const background = Boolean(options.background);
        const hasCachedRows = hasRows(state.lastRows.tomorrow);
            if (hasCachedRows) {
          renderTomorrowTable(state.lastRows.tomorrow);
          if (!background) {
            setStatus("明日已显示；证据特征由盘中后台任务预计算");
          }
        } else {
          els.tomorrowBody.innerHTML = '<tr><td colspan="10" class="empty">加载中...</td></tr>';
        }
        const params = new URLSearchParams({
          top_n: String((window.APP_CONFIG || {}).defaultTopN || 18),
          market: DEFAULT_MARKET,
        });
        state.tomorrowLoading = (async () => {
          try {
            const res = await fetch(`/api/tomorrow-picks?${params.toString()}`);
            const payload = await res.json();
            if (!payload.ok) {
              throw new Error(payload.error || "接口返回异常");
            }
            const marketTimestamp = payloadMarketTimestamp(payload);
            if (marketTimestamp && state.recommendationDataTimestamp && marketTimestamp < state.recommendationDataTimestamp) {
              return;
            }
          const rows = payload.data || [];
            const shouldRender = rememberFingerprint("tomorrow", { rows, meta: payload.meta || {} });
            state.lastRows.tomorrow = rows;
            if (marketTimestamp) state.recommendationDataTimestamp = marketTimestamp;
            renderMetrics({ health: payload.health, meta: payload.meta, market_sentiment: {} });
            if (shouldRender) {
              renderTomorrowTable(state.lastRows.tomorrow);
            }
            if (!background) {
            setStatus(`明日本次更新时间 ${payload.meta?.as_of || payload.meta?.generated_at || "最近快照"}${snapshotPhaseLabel(payload) ? ` · ${snapshotPhaseLabel(payload)}` : ""}`);
            }
          } catch (err) {
            state.tomorrowLoaded = false;
            if (!background || !hasRows(state.lastRows.tomorrow)) {
              els.tomorrowBody.innerHTML = `<tr><td colspan="10" class="empty">${escapeHtml(err.message)}</td></tr>`;
            }
            if (!background) {
              setStatus(`明日加载失败：${err.message}`);
            }
          } finally {
            state.tomorrowLoading = null;
          }
        })();
        return state.tomorrowLoading;
      }

      async function loadHorizonPicks(options = {}) {
        if (state.horizonLoading) {
          return state.horizonLoading;
        }
        state.horizonLoaded = true;
        const background = Boolean(options.background);
        if (!background || !hasRows(state.lastRows.swing)) {
          els.swingBody.innerHTML = '<tr><td colspan="10" class="empty">加载中...</td></tr>';
          if (els.swingLongTermBody) {
            els.swingLongTermBody.innerHTML = '<tr><td colspan="4" class="empty">加载中...</td></tr>';
          }
        }
        const params = new URLSearchParams({
          top_n: String((window.APP_CONFIG || {}).defaultTopN || 18),
          market: DEFAULT_MARKET,
        });
        state.horizonLoading = (async () => {
          try {
            const swingRes = await fetch(`/api/swing-picks?${params.toString()}`);
            const swingPayload = await swingRes.json();
            if (!swingPayload.ok) {
              throw new Error(swingPayload.error || "波段接口返回异常");
            }
            const marketTimestamp = payloadMarketTimestamp(swingPayload);
            if (marketTimestamp && state.recommendationDataTimestamp && marketTimestamp < state.recommendationDataTimestamp) {
              return;
            }
            const swingRows = swingPayload.data || [];
            const shouldRenderSwing = rememberFingerprint("swing", swingRows);
            state.lastRows.swing = swingRows;
            if (marketTimestamp) state.recommendationDataTimestamp = marketTimestamp;
            renderMetrics({ health: swingPayload.health, meta: swingPayload.meta, market_sentiment: {} });
            if (shouldRenderSwing) {
              renderSwingTable(state.lastRows.swing);
              if (Array.isArray(state.lastRows.swingLongTerm) && state.lastRows.swingLongTerm.length) {
                renderSwingLongTermTable(state.lastRows.swingLongTerm);
              }
            }
            if (!background) {
            setStatus(`2-5日更新时间 ${swingPayload.meta?.as_of || swingPayload.meta?.generated_at || "最近快照"}${snapshotPhaseLabel(swingPayload) ? ` · ${snapshotPhaseLabel(swingPayload)}` : ""}`);
            }
          } catch (err) {
            state.horizonLoaded = false;
            if (!background || !hasRows(state.lastRows.swing)) {
              els.swingBody.innerHTML = `<tr><td colspan="10" class="empty">${escapeHtml(err.message)}</td></tr>`;
              if (els.swingLongTermBody) {
                els.swingLongTermBody.innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(err.message)}</td></tr>`;
              }
            }
            if (!background) {
            setStatus(`2-5日加载失败：${err.message}`);
            }
          } finally {
            state.horizonLoading = null;
          }
        })();
        return state.horizonLoading;
      }

      function currentPoolRows() {
        const filter = activePoolFilter();
        if (filter === "today") return state.lastRows.shortTerm || [];
        if (filter === "next") return state.lastRows.tomorrow || [];
        if (filter === "swing") return state.lastRows.swing || [];
        if (filter === "swingLongTerm") return state.lastRows.swingLongTerm || [];
        return state.lastRows.shortTerm || [];
      }

      function renderRecommendationActionSummary() {
        if (!els.recommendationActionSummary) return;
        const rows = RecommendationUtils.filterAndSortRows(currentPoolRows(), {
          actionFilter: DEFAULT_ACTION_FILTER,
          sortMode: DEFAULT_SORT_MODE,
        });
        els.recommendationActionSummary.innerHTML = RecommendationRenderers.renderRecommendationActionSummaryHtml(rows, {
          escapeHtml,
          formatNumber,
          rowScore: RecommendationUtils.rowScore.bind(RecommendationUtils),
        });
      }

      function renderShortTermTable(rows) {
        const rawRows = Array.isArray(rows) ? rows : [];
        const executableRows = rawRows.filter(row => RecommendationUtils.isExecutableActionRow(row));
        if (rawRows.length && !executableRows.length) {
          els.shortTermBody.innerHTML = '<tr><td colspan="10" class="empty">暂无可执行推荐，当前仅有观察备选</td></tr>';
          return;
        }
        // Short-term recommendations now include observation rows for today strategy visibility.
        const displayRows = RecommendationUtils.filterAndSortRows(rawRows, {
          actionFilter: DEFAULT_ACTION_FILTER,
          sortMode: DEFAULT_SORT_MODE,
        });
        if (!displayRows.length) {
          let emptyText = "暂无符合条件的股票";
          if (rawRows.length) {
            emptyText = "暂无可执行推荐，当前仅有观察备选";
          }
          els.shortTermBody.innerHTML = `<tr><td colspan="10" class="empty">${escapeHtml(emptyText)}</td></tr>`;
          return;
        }
        els.shortTermBody.innerHTML = RecommendationTables.renderShortTermTableRows(displayRows, {
          escapeHtml,
          formatNumber,
          formatMoney,
          rowIndustryLabel: RecommendationRenderers.rowIndustryLabel.bind(RecommendationRenderers),
          explanationTags: (row) => RecommendationRenderers.explanationTags(row, { formatNumber, escapeHtml }),
          actionColumn: (row) => RecommendationRenderers.actionColumn(row, { formatNumber, escapeHtml }),
          scoreCell: (row) => RecommendationRenderers.scoreCell(row, {
            escapeHtml,
            formatNumber,
            rowScore: RecommendationUtils.rowScore,
          }),
        });
      }

      function renderTomorrowTable(rows) {
        const displayRows = RecommendationUtils.filterAndSortRows(rows, {
          actionFilter: DEFAULT_ACTION_FILTER,
          sortMode: DEFAULT_SORT_MODE,
        });
        if (!displayRows.length) {
          els.tomorrowBody.innerHTML = '<tr><td colspan="10" class="empty">暂无符合条件的股票</td></tr>';
          return;
        }
        els.tomorrowBody.innerHTML = RecommendationTables.renderTomorrowTableRows(displayRows, {
          escapeHtml,
          formatNumber,
          formatMoney,
          rowIndustryLabel: RecommendationRenderers.rowIndustryLabel.bind(RecommendationRenderers),
          explanationTags: (row) => RecommendationRenderers.explanationTags(row, { formatNumber, escapeHtml }),
          actionColumn: (row) => RecommendationRenderers.actionColumn(row, { formatNumber, escapeHtml }),
          scoreCell: (row) => RecommendationRenderers.scoreCell(row, {
            escapeHtml,
            formatNumber,
            rowScore: RecommendationUtils.rowScore,
          }),
        });
      }

      function renderSwingTable(rows) {
        const displayRows = RecommendationUtils.filterAndSortRows(rows, {
          actionFilter: DEFAULT_ACTION_FILTER,
          sortMode: DEFAULT_SORT_MODE,
        });
        if (!displayRows.length) {
          els.swingBody.innerHTML = '<tr><td colspan="10" class="empty">暂无符合条件的2-5日股票</td></tr>';
          return;
        }
        els.swingBody.innerHTML = RecommendationTables.renderSwingTableRows(displayRows, {
          escapeHtml,
          formatNumber,
          formatMoney,
          rowIndustryLabel: RecommendationRenderers.rowIndustryLabel.bind(RecommendationRenderers),
          explanationTags: (row) => RecommendationRenderers.explanationTags(row, { formatNumber, escapeHtml }),
          actionColumn: (row) => RecommendationRenderers.actionColumn(row, { formatNumber, escapeHtml }),
          scoreCell: (row) => RecommendationRenderers.scoreCell(row, {
            escapeHtml,
            formatNumber,
            rowScore: RecommendationUtils.rowScore,
          }),
        });
      }

      function renderSwingLongTermTable(rows) {
        const displayRows = RecommendationUtils.filterAndSortRows(rows, {
          actionFilter: "all",
          sortMode: DEFAULT_SORT_MODE,
        });
        if (!displayRows.length) {
          if (els.swingLongTermBody) {
            els.swingLongTermBody.innerHTML = '<tr><td colspan="5" class="empty">暂无符合条件的长期股</td></tr>';
          }
          return;
        }
        if (els.swingLongTermBody) {
          els.swingLongTermBody.innerHTML = RecommendationTables.renderSwingLongTermTableRows(displayRows, {
            escapeHtml,
            formatNumber,
            explanationTags: (row) => RecommendationRenderers.longTermExplanationTags(row, {
              escapeHtml,
              formatNumber,
            }),
          });
        }
      }

      function rerenderCurrentTables() {
        renderShortTermTable(state.lastRows.shortTerm);
        if (state.tomorrowLoaded) {
          renderTomorrowTable(state.lastRows.tomorrow);
        }
        if (state.horizonLoaded) {
          renderSwingTable(state.lastRows.swing);
          renderSwingLongTermTable(state.lastRows.swingLongTerm);
        }
        renderRecommendationActionSummary();
      }

      function activePoolFilter() {
        return document.querySelector(".pool-tab.active")?.dataset.poolFilter || "today";
      }

      function applyRecommendationPoolFilter(filter = activePoolFilter()) {
        els.poolTabs.forEach(tab => {
          tab.classList.toggle("active", tab.dataset.poolFilter === filter);
        });
        els.poolGroups.forEach(group => {
          group.hidden = group.dataset.poolGroup !== filter;
        });
        renderRecommendationActionSummary();
      }

      function ensureRecommendationPoolData(options = {}) {
        const background = Boolean(options.background);
        const filter = activePoolFilter();
        if (filter === "next" && !state.tomorrowLoaded) {
          loadTomorrowPicks({ background });
        }
        if ((filter === "swing" || filter === "swingLongTerm") && !state.horizonLoaded) {
          loadHorizonPicks({ background });
        }
      }

      function prefetchRecommendationPools() {
        const tasks = [];
        if (!state.tomorrowLoaded || !hasRows(state.lastRows.tomorrow)) {
          tasks.push(loadTomorrowPicks({ background: true }));
        }
        if (!state.horizonLoaded || !hasRows(state.lastRows.swing)) {
          tasks.push(loadHorizonPicks({ background: true }));
        }
        return Promise.allSettled(tasks);
      }

      function selectRecommendationPool(filter) {
        applyRecommendationPoolFilter(filter);
        ensureRecommendationPoolData();
      }

      return {
        applyRecommendationPoolFilter,
        ensureRecommendationPoolData,
        loadRecommendations,
        prefetchRecommendationPools,
        selectRecommendationPool,
        startRecommendationStreamWithSnapshot,
        stopRecommendationStream,
      };
    },
  };
})();
