import json
import shutil
import subprocess
import textwrap
import unittest


@unittest.skipUnless(shutil.which("node"), "node is required for frontend contract tests")
class FrontendContractTest(unittest.TestCase):
    def run_node(self, source):
        result = subprocess.run(
            ["node", "-e", textwrap.dedent(source)],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def test_swing_validation_uses_executable_exit_return(self):
        result = self.run_node(
            """
            global.window = {};
            require('./static/validation-renderers.js');
            const renderer = window.TraderValidationRenderers;
            const row = {
              strategy_name: 'swing_picks',
              signal_exit_return: 3.25,
              signal_hold_5d_return: -2.0,
            };
            process.stdout.write(JSON.stringify({
              label: renderer.primaryValidationLabel(row),
              value: renderer.primaryValidationReturn(row),
            }));
            """
        )

        self.assertEqual(result, {"label": "2-5日退出", "value": 3.25})

    def test_blocked_recommendation_cannot_render_as_buy(self):
        result = self.run_node(
            """
            global.window = {};
            require('./static/recommendation-renderers.js');
            const renderer = window.TraderRecommendationRenderers;
            const html = renderer.actionColumn({
              execution_allowed: false,
              tier_label: '备选观察',
              trade_action: {action: 'buy_confirmed', label: '确认买入', position_size: 0},
            }, {
              formatNumber: value => String(value),
              escapeHtml: value => String(value),
            });
            process.stdout.write(JSON.stringify({html}));
            """
        )

        self.assertIn("观察", result["html"])
        self.assertIn("备选观察", result["html"])
        self.assertIn("仓位0 · 不执行", result["html"])
        self.assertNotIn("确认买入", result["html"])

    def test_recommendation_score_cell_exposes_expected_return_shadow_fields(self):
        result = self.run_node(
            """
            global.window = {};
            require('./static/recommendation-renderers.js');
            const renderer = window.TraderRecommendationRenderers;
            const row = {
              score: 72.4,
              decision_score: 91.7,
              avg_risk: 31,
              predicted_net_return: 1.25,
              expected_return_net: 1.25,
              p_win: 0.612,
              downside_p10: -2.4,
              model_confidence: 'shadow',
              expected_return_sample_count: 36,
            };
            const helpers = {
              rowScore: item => Number(item.score || 0),
              formatNumber: (value, digits) => Number(value).toFixed(digits),
              escapeHtml: value => String(value),
            };
            process.stdout.write(JSON.stringify({
              scoreHtml: renderer.scoreCell(row, helpers),
              explanation: renderer.explanationTags(row, helpers),
            }));
            """
        )

        self.assertIn("综72.4", result["scoreHtml"])
        self.assertIn("质91.7", result["scoreHtml"])
        self.assertIn("险31", result["scoreHtml"])
        self.assertIn("模型影子", result["scoreHtml"])
        self.assertIn("E+1.25%", result["scoreHtml"])
        self.assertIn("P61%", result["scoreHtml"])
        self.assertIn("收益模型", result["explanation"])
        self.assertIn("预测净收益+1.25%", result["explanation"])
        self.assertIn("模型胜率61.2%", result["explanation"])
        self.assertIn("置信影子", result["explanation"])
        self.assertNotIn("影子排序分", result["explanation"])

    def test_stock_prediction_labels_rule_consistency_not_probability_confidence(self):
        with open("static/validation-stock-prediction.js", encoding="utf-8") as source:
            prediction_source = source.read()

        self.assertIn("规则一致度", prediction_source)
        self.assertIn("formatNumber(p.rule_consistency, 1)", prediction_source)
        self.assertNotIn("p.signal_coverage", prediction_source)
        self.assertNotIn("p.confidence", prediction_source)
        self.assertNotIn("本地置信", prediction_source)

    def test_empty_recommendation_action_summary_keeps_card_layout(self):
        result = self.run_node(
            """
            global.window = {};
            require('./static/recommendation-renderers.js');
            const renderer = window.TraderRecommendationRenderers;
            const html = renderer.renderRecommendationActionSummaryHtml([], {
              rowScore: () => 0,
              formatNumber: value => String(value),
              escapeHtml: value => String(value),
            });
            const appSource = require('fs').readFileSync('./static/recommendation-app.js', 'utf8');
            process.stdout.write(JSON.stringify({html, appSource}));
            """
        )

        self.assertIn("action-summary-shell", result["html"])
        self.assertIn("action-signals-card", result["html"])
        self.assertIn("买入动作", result["html"])
        self.assertIn("卖点提示", result["html"])
        self.assertIn("暂无可执行买入", result["html"])
        self.assertIn("暂无明确卖点", result["html"])
        self.assertNotIn("当前筛选下暂无动作汇总", result["appSource"])

    def test_recommendation_status_marks_close_fallback_phase(self):
        result = self.run_node(
            """
            global.window = {};
            global.document = {
              querySelector: () => ({ dataset: { poolFilter: "today" } }),
            };
            require('./static/recommendation-utils.js');
            require('./static/recommendation-renderers.js');
            require('./static/recommendation-tables.js');
            require('./static/recommendation-app.js');

            const state = {
              recommendationRequestSeq: 0,
              recommendationDataTimestamp: 0,
              recommendationHasPayload: false,
              renderFingerprints: {},
              tomorrowLoaded: false,
              horizonLoaded: false,
              lastRows: { shortTerm: [], tomorrow: [], swing: [] },
            };
            const statuses = [];
            const app = window.TraderRecommendationApp.create({
              state,
              els: {
                shortTermBody: { innerHTML: "" },
                tomorrowBody: { innerHTML: "" },
                swingBody: { innerHTML: "" },
                recommendationActionSummary: { innerHTML: "" },
              },
              helpers: {
                escapeHtml: value => String(value ?? ""),
                formatMoney: value => String(value ?? ""),
                formatNumber: value => String(value ?? ""),
                hasRows: rows => Array.isArray(rows) && rows.length > 0,
                rememberFingerprint: (key, value) => {
                  const next = JSON.stringify(value ?? null);
                  if (state.renderFingerprints[key] === next) return false;
                  state.renderFingerprints[key] = next;
                  return true;
                },
              },
              config: {
                DEFAULT_ACTION_FILTER: "all",
                DEFAULT_MARKET: "all",
                DEFAULT_SORT_MODE: "rank",
              },
              status: {
                renderMetrics: () => {},
                setStatus: text => statuses.push(text),
                startPushStatusCountdown: () => {},
              },
            });
            global.fetch = async () => ({
              json: async () => ({
                ok: true,
                recommendations: {
                  short_term: [{ code: "600001", name: "测试", price: 10, trade_action: { position_size: 1 } }],
                  tomorrow_picks: [],
                  swing_picks: [],
                },
                meta: {
                  generated_at: "2026-07-14T15:01:00",
                  as_of: "2026-07-14T15:01:00",
                  snapshot_phase: "close_fallback",
                },
                health: {},
              }),
            });
            (async () => {
              await app.loadRecommendations({ background: true });
              process.stdout.write(JSON.stringify({ statuses }));
            })().catch(error => {
              process.stderr.write(String(error));
              process.exit(1);
            });
            """
        )

        self.assertTrue(any("收盘补充" in item for item in result["statuses"]))

    def test_recommendation_table_width_contract_keeps_stock_and_price_compact(self):
        with open("static/styles.css", encoding="utf-8") as source:
            styles_source = source.read()

        self.assertIn("--stock-col: 88px;", styles_source)
        self.assertIn("--latest-price-col: 60px;", styles_source)
        self.assertIn("th.col-latest-price", styles_source)

    def test_backup_only_recommendation_summary_is_not_executable(self):
        result = self.run_node(
            """
            global.window = {};
            require('./static/recommendation-renderers.js');
            const renderer = window.TraderRecommendationRenderers;
            const rows = [{
              name: '测试股份',
              score: 88,
              execution_allowed: false,
              tier_label: '备选观察',
              trade_action: {action: 'buy_confirmed', label: '确认买入', position_size: 0},
              exit_action: {action: 'hold', label: '持有'},
            }];
            const html = renderer.renderRecommendationActionSummaryHtml(rows, {
              rowScore: row => Number(row.score || 0),
              formatNumber: (value, digits = 0) => Number(value).toFixed(digits),
              escapeHtml: value => String(value),
            });
            process.stdout.write(JSON.stringify({html}));
            """
        )

        self.assertIn("无可执行推荐", result["html"])
        self.assertIn("仓位为0", result["html"])
        self.assertIn("暂无可执行买入", result["html"])
        self.assertNotIn("可开仓", result["html"])
        self.assertNotIn("确认买入", result["html"])

    def test_validation_decision_uses_backend_gate_reason(self):
        result = self.run_node(
            """
            global.window = {};
            require('./static/validation-ui.js');
            const target = {};
            window.TraderValidationUI.renderValidationSimpleDecision(target, {
              strategy: 'tomorrow_picks',
              sample: 30,
              outcome: 30,
              replay: 0,
              realDayCount: 20,
              winRate: 45,
              avgReturn: -0.2,
              horizon: '次日',
              pendingOutcome: 0,
              validationGate: {
                blocked: true,
                state: 'retired',
                reason: '真实交易日净表现不达标，仅保留备选观察',
              },
            }, {formatNumber: value => String(value)});
            process.stdout.write(JSON.stringify({text: target.textContent, cls: target.className}));
            """
        )

        self.assertIn("真实交易日净表现不达标", result["text"])
        self.assertIn("仓位0", result["text"])
        self.assertEqual(result["cls"], "validation-current-decision decision-bad")

    def test_stock_diagnosis_stays_local_without_synchronous_deepseek_review(self):
        with open("static/app.js", encoding="utf-8") as source:
            app_source = source.read()
        with open("static/validation-stock-prediction.js", encoding="utf-8") as source:
            prediction_source = source.read()
        with open("templates/index.html", encoding="utf-8") as source:
            template_source = source.read()

        self.assertNotIn("?deepseek=1", app_source + prediction_source)
        self.assertIn("/api/stock-prediction/${encodeURIComponent(code)}", prediction_source)
        self.assertIn("本地量化", prediction_source)
        self.assertIn('id="stockPredictionBtn" class="primary-action" type="button">走势预测</button>', template_source)
        self.assertIn('id="generateTuningBtn" class="primary-action" type="button">策略验证</button>', template_source)
        self.assertNotIn("shadowTuningBtn", template_source + app_source + prediction_source)
        self.assertIn('class="tool-action-divider"', template_source)

    def test_strategy_validation_uses_history_without_stock_code(self):
        result = self.run_node(
            """
            global.window = {
              TraderValidationUI: {},
              TraderValidationRenderers: {},
            };
            require('./static/validation-app.js');
            const calls = [];
            let failTuning = false;
            global.fetch = async (url, options = {}) => {
              calls.push({url, method: options.method || 'GET'});
              if (!url.includes('/tuning?')) {
                return {
                  ok: true,
                  json: async () => ({
                    ok: true,
                    metrics: {
                      sample_count: 12,
                      real_day_count: 8,
                      pending_outcome_count: 1,
                      real_win_rate_primary_net: 52.5,
                      real_avg_primary_return_net: 0.4,
                      real_portfolio_max_drawdown_pct: -3.2,
                    },
                    validation_gate: {blocked: false, reason: '真实样本验证完成'},
                  }),
                };
              }
              if (failTuning) {
                return {
                  ok: false,
                  json: async () => ({ok: false, error: '调参服务暂不可用'}),
                };
              }
              const plan = {
                can_apply: false,
                shadow_mode: true,
                reason: '调参建议只进入影子验证',
                generated_at: '2026-07-14T18:00:00',
                issues: [],
                suggestions: [],
                gate: {items: [
                  {name: 'min_day_count', passed: true, actual: 60, required: 30},
                  {name: 'min_real_day_count', passed: true, actual: 40, required: 20},
                  {name: 'no_pending_outcomes', passed: false, actual: 1, required: 0},
                  {name: 'positive_avg_net_return', passed: true, actual: 0.4, required: '> 0'},
                  {name: 'max_primary_drawdown', passed: true, actual: -3.2, required: '> -8'},
                ]},
              };
              return {
                ok: true,
                json: async () => ({ok: true, reused: true, plan, latest: {plan}}),
              };
            };
            const resultPane = {innerHTML: ''};
            const tuningStatus = {};
            const app = window.TraderValidationApp.create({
              state: {},
              els: {
                validationStrategySelect: {value: 'tomorrow_picks'},
                validationDaysSelect: {value: '60'},
                generateTuningBtn: {textContent: '策略验证', disabled: false},
                stockPredictionBtn: {disabled: false},
                tuningStatus,
                toolResultPane: resultPane,
              },
              helpers: {
                escapeHtml: value => String(value),
                formatNumber: (value, digits = 0) => Number(value).toFixed(digits),
                numberClass: () => '',
                strategyLabel: strategy => strategy === 'tomorrow_picks' ? '明日' : strategy,
                validationSnapshotStrategiesText: () => '',
              },
              config: {VALIDATION_AUTO_REFRESH_MS: 30000, VALIDATION_DATE_PAGE_SIZE: 5},
              status: {
                renderToolResult: html => { resultPane.innerHTML = html; },
                setOpsStatus: (target, text, level) => {
                  target.textContent = text;
                  target.level = level;
                },
                setStatus: () => {},
              },
            });
            (async () => {
              await app.loadStrategyValidationReport();
              const success = {
                calls: calls.slice(),
                status: {...tuningStatus},
                html: resultPane.innerHTML,
              };
              failTuning = true;
              await app.loadStrategyValidationReport();
              process.stdout.write(JSON.stringify({
                success,
                partial: {
                  status: tuningStatus,
                  html: resultPane.innerHTML,
                },
              }));
            })().catch(error => {
              process.stderr.write(String(error));
              process.exit(1);
            });
            """
        )

        self.assertEqual(
            result["success"]["calls"],
            [
                {
                    "url": "/api/strategy-validation?strategy=tomorrow_picks&days=60",
                    "method": "GET",
                },
                {
                    "url": "/api/strategy-validation/tuning?strategy=tomorrow_picks&days=60",
                    "method": "POST",
                },
            ],
        )
        self.assertEqual(result["success"]["status"]["level"], "ok")
        self.assertIn("近60日验证已完成", result["success"]["status"]["textContent"])
        self.assertIn("真实样本验证完成", result["success"]["html"])
        self.assertIn("真实交易日 8", result["success"]["html"])
        self.assertIn("回撤 -3.20%", result["success"]["html"])
        self.assertIn("调参建议只进入影子验证", result["success"]["html"])
        self.assertIn("复用已有建议", result["success"]["html"])
        self.assertIn("max_primary_drawdown", result["success"]["html"])
        self.assertIn("实际 -3.2", result["success"]["html"])
        self.assertIn("门槛 > -8", result["success"]["html"])
        self.assertEqual(result["partial"]["status"]["level"], "bad")
        self.assertIn("策略验证已完成", result["partial"]["status"]["textContent"])
        self.assertIn("真实交易日 8", result["partial"]["html"])
        self.assertIn("影子调参建议暂不可用", result["partial"]["html"])
        self.assertIn("调参服务暂不可用", result["partial"]["html"])

    def test_strategy_switch_ignores_stale_validation_failure(self):
        result = self.run_node(
            """
            global.window = {
              TraderValidationUI: {
                validationStrategyMeta: strategy => ({label: strategy, outcome: '', horizon: ''}),
              },
              TraderValidationRenderers: {},
            };
            require('./static/validation-app.js');
            const pending = [];
            global.fetch = url => new Promise(resolve => pending.push({url, resolve}));
            const state = {
              validationCache: {},
              validationRequestSeq: 0,
              validationReportRequestSeq: 0,
              selectedValidation: {date: '', strategy: ''},
            };
            const strategySelect = {value: 'tomorrow_picks'};
            const resultPane = {innerHTML: ''};
            const tuningStatus = {};
            const button = {textContent: '策略验证', disabled: false};
            const app = window.TraderValidationApp.create({
              state,
              els: {
                validationStrategySelect: strategySelect,
                validationStrategyTabs: [],
                validationDaysSelect: {value: '60'},
                validationDatesBody: {innerHTML: ''},
                validationDetailBody: {innerHTML: ''},
                validationBaselineStatus: {},
                updateStatus: {textContent: ''},
                generateTuningBtn: button,
                stockPredictionBtn: {disabled: false},
                tuningStatus,
                toolResultPane: resultPane,
              },
              helpers: {
                escapeHtml: value => String(value),
                formatNumber: value => String(value),
                numberClass: () => '',
                strategyLabel: value => value,
                validationSnapshotStrategiesText: () => '',
              },
              config: {VALIDATION_AUTO_REFRESH_MS: 30000, VALIDATION_DATE_PAGE_SIZE: 5},
              status: {
                renderToolResult: html => { resultPane.innerHTML = html; },
                setOpsStatus: (target, text, level) => Object.assign(target, {textContent: text, level}),
                setStatus: () => {},
              },
            });
            (async () => {
              const staleRequest = app.loadStrategyValidationReport();
              strategySelect.value = 'swing_picks';
              app.handleValidationStrategyChange();
              pending[0].resolve({
                ok: false,
                json: async () => ({ok: false, error: 'old request failed'}),
              });
              await staleRequest;
              process.stdout.write(JSON.stringify({
                html: resultPane.innerHTML,
                status: tuningStatus,
                button,
                calls: pending.map(item => item.url),
              }));
            })().catch(error => {
              process.stderr.write(String(error));
              process.exit(1);
            });
            """
        )

        self.assertEqual(len(result["calls"]), 2)
        self.assertIn("strategy=swing_picks", result["calls"][1])
        self.assertNotIn("old request failed", result["html"])
        self.assertEqual(result["status"].get("textContent", ""), "")
        self.assertFalse(result["button"]["disabled"])
        self.assertEqual(result["button"]["textContent"], "策略验证")

    def test_validation_snapshot_status_uses_backend_strategy_list(self):
        with open("static/app.js", encoding="utf-8") as source:
            app_source = source.read()

        self.assertIn("validationSnapshotStrategiesText(config.strategies)", app_source)
        self.assertIn("snapshotStatusText(snapshot, config.strategies)", app_source)
        self.assertNotIn("三类策略验证快照", app_source)

    def test_validation_panel_loads_oos_report(self):
        with open("static/app.js", encoding="utf-8") as source:
            app_source = source.read()
        with open("templates/index.html", encoding="utf-8") as source:
            template_source = source.read()

        self.assertIn('class="validation-status-panel"', template_source)
        self.assertIn('id="validationOosReport"', template_source)
        self.assertIn('id="validationPortfolioBaseline"', template_source)
        self.assertNotIn('id="validationBaselineDryRunBtn"', template_source)
        self.assertNotIn('id="validationBaselineExecuteBtn"', template_source)
        self.assertIn('id="validationBaselineStatus"', template_source)
        self.assertIn("current baseline 自动回填状态", template_source)
        with open("static/styles.css", encoding="utf-8") as source:
            styles_source = source.read()
        with open("static/recommendation-status.js", encoding="utf-8") as source:
            status_source = source.read()
        self.assertIn(".validation-baseline-actions.has-status", styles_source)
        self.assertIn('classList.toggle("has-status"', status_source)
        self.assertIn("/api/strategy-validation/oos-report", app_source)
        self.assertIn("/api/strategy-validation/readiness", app_source)
        self.assertIn("/api/strategy-validation/backfill-current-baseline", app_source)
        self.assertIn('params.set("execute", "1")', app_source)
        self.assertIn("window.confirm", app_source)
        self.assertIn("maybeAutoBackfillCurrentBaseline", app_source)
        self.assertIn("shouldAutoBackfillCurrentBaseline", app_source)
        self.assertIn('execute: "1"', app_source)
        self.assertIn("current baseline 自动回填完成", app_source)
        self.assertIn("renderValidationOosReport", app_source)
        self.assertIn("renderValidationBaselineBackfillResult", app_source)
        self.assertIn("候选", app_source)
        self.assertIn("回填前", app_source)
        self.assertIn("回填后", app_source)
        self.assertIn("oos_status", app_source)
        self.assertIn("blockers", app_source)
        self.assertIn("暂无真实 OOS", app_source)

    def test_short_term_recommendations_empty_message_when_only_observation_rows_are_filtered_out(self):
        result = self.run_node(
            """
            global.window = {};
            global.document = {
              querySelector: () => ({ dataset: { poolFilter: "today" } }),
            };

            require('./static/recommendation-utils.js');
            require('./static/recommendation-renderers.js');
            require('./static/recommendation-tables.js');
            require('./static/recommendation-app.js');

            const state = {
              recommendationRequestSeq: 0,
              recommendationDataTimestamp: 0,
              recommendationHasPayload: false,
              renderFingerprints: {},
              tomorrowLoaded: false,
              horizonLoaded: false,
              lastRows: {
                shortTerm: [],
                tomorrow: [],
                swing: [],
              },
            };

            const shortTermBody = { innerHTML: "" };
            const tomorrowBody = { innerHTML: "" };
            const swingBody = { innerHTML: "" };
            const recommendationActionSummary = { innerHTML: "" };
            const config = {
              DEFAULT_ACTION_FILTER: "wait",
              DEFAULT_MARKET: "all",
              DEFAULT_SORT_MODE: "rank",
            };

            const helpers = {
              escapeHtml: (value) => String(value ?? "")
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/\"/g, "&quot;")
                .replace(/'/g, "&#039;"),
              formatMoney: (value) => String(value ?? ""),
              formatNumber: (value) => String(Number(value)).replace(/\\.([0-9]+)$/, ".0$1"),
              hasRows: (rows) => Array.isArray(rows) && rows.length > 0,
              rememberFingerprint: (key, value) => {
                const next = JSON.stringify(value ?? null);
                if (state.renderFingerprints[key] === next) return false;
                state.renderFingerprints[key] = next;
                return true;
              },
            };

            const els = {
              shortTermBody,
              tomorrowBody,
              swingBody,
              recommendationActionSummary,
            };

            const status = {
              renderMetrics: () => {},
              setStatus: () => {},
              startPushStatusCountdown: () => {},
            };

            const app = window.TraderRecommendationApp.create({
              state,
              els,
              helpers,
              config,
              status,
            });

            global.fetch = async () => ({
              json: async () => ({
                ok: true,
                recommendations: {
                  short_term: [
                    {
                      code: "000001",
                      name: "测试股份",
                      action_label: "只观察",
                      execution_allowed: false,
                      trade_action: { action: "watch_only", label: "只观察", position_size: 0 },
                    },
                  ],
                  tomorrow_picks: [],
                  swing_picks: [],
                },
                meta: {
                  short_term_observation_count: 1,
                  generated_at: "2026-07-14T14:30:00",
                  quote_timestamp: "2026-07-14T14:30:00",
                },
                health: {},
                market_sentiment: {},
              }),
            });

            (async () => {
              await app.loadRecommendations({ background: true });
              process.stdout.write(JSON.stringify({ shortTermBody: shortTermBody.innerHTML }));
            })();
            """
        )
        self.assertIn("暂无可执行推荐，当前仅有观察备选", result["shortTermBody"])

    def test_short_term_recommendations_empty_message_when_observation_rows_without_meta_count(self):
        result = self.run_node(
            """
            global.window = {};
            global.document = {
              querySelector: () => ({ dataset: { poolFilter: "today" } }),
            };

            require('./static/recommendation-utils.js');
            require('./static/recommendation-renderers.js');
            require('./static/recommendation-tables.js');
            require('./static/recommendation-app.js');

            const state = {
              recommendationRequestSeq: 0,
              recommendationDataTimestamp: 0,
              recommendationHasPayload: false,
              renderFingerprints: {},
              tomorrowLoaded: false,
              horizonLoaded: false,
              lastRows: {
                shortTerm: [],
                tomorrow: [],
                swing: [],
              },
            };

            const shortTermBody = { innerHTML: "" };
            const tomorrowBody = { innerHTML: "" };
            const swingBody = { innerHTML: "" };
            const recommendationActionSummary = { innerHTML: "" };
            const config = {
              DEFAULT_ACTION_FILTER: "all",
              DEFAULT_MARKET: "all",
              DEFAULT_SORT_MODE: "rank",
            };

            const helpers = {
              escapeHtml: (value) => String(value ?? "")
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/\"/g, "&quot;")
                .replace(/'/g, "&#039;"),
              formatMoney: (value) => String(value ?? ""),
              formatNumber: (value) => String(Number(value)),
              hasRows: (rows) => Array.isArray(rows) && rows.length > 0,
              rememberFingerprint: (key, value) => {
                const next = JSON.stringify(value ?? null);
                if (state.renderFingerprints[key] === next) return false;
                state.renderFingerprints[key] = next;
                return true;
              },
            };

            const els = {
              shortTermBody,
              tomorrowBody,
              swingBody,
              recommendationActionSummary,
            };

            const status = {
              renderMetrics: () => {},
              setStatus: () => {},
              startPushStatusCountdown: () => {},
            };

            const app = window.TraderRecommendationApp.create({
              state,
              els,
              helpers,
              config,
              status,
            });

            global.fetch = async () => ({
              json: async () => ({
                ok: true,
                recommendations: {
                  short_term: [
                    {
                      code: "000001",
                      name: "测试股份",
                      action_label: "只观察",
                      execution_allowed: false,
                      trade_action: { action: "watch_only", label: "只观察", position_size: 0 },
                    },
                  ],
                  tomorrow_picks: [],
                  swing_picks: [],
                },
                meta: {
                  generated_at: "2026-07-14T14:30:00",
                  quote_timestamp: "2026-07-14T14:30:00",
                },
                health: {},
                market_sentiment: {},
              }),
            });

            (async () => {
              await app.loadRecommendations({ background: true });
              process.stdout.write(JSON.stringify({ shortTermBody: shortTermBody.innerHTML }));
            })();
            """
        )
        self.assertIn("暂无可执行推荐，当前仅有观察备选", result["shortTermBody"])

    def test_short_term_recommendations_empty_message_when_no_rows(self):
        result = self.run_node(
            """
            global.window = {};
            global.document = {
              querySelector: () => ({ dataset: { poolFilter: "today" } }),
            };

            require('./static/recommendation-utils.js');
            require('./static/recommendation-renderers.js');
            require('./static/recommendation-tables.js');
            require('./static/recommendation-app.js');

            const state = {
              recommendationRequestSeq: 0,
              recommendationDataTimestamp: 0,
              recommendationHasPayload: false,
              renderFingerprints: {},
              tomorrowLoaded: false,
              horizonLoaded: false,
              lastRows: {
                shortTerm: [],
                tomorrow: [],
                swing: [],
              },
            };

            const shortTermBody = { innerHTML: "" };
            const tomorrowBody = { innerHTML: "" };
            const swingBody = { innerHTML: "" };
            const recommendationActionSummary = { innerHTML: "" };
            const config = {
              DEFAULT_ACTION_FILTER: "all",
              DEFAULT_MARKET: "all",
              DEFAULT_SORT_MODE: "rank",
            };

            const helpers = {
              escapeHtml: (value) => String(value ?? "")
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/\"/g, "&quot;")
                .replace(/'/g, "&#039;"),
              formatMoney: (value) => String(value ?? ""),
              formatNumber: (value) => String(Number(value)),
              hasRows: (rows) => Array.isArray(rows) && rows.length > 0,
              rememberFingerprint: (key, value) => {
                const next = JSON.stringify(value ?? null);
                if (state.renderFingerprints[key] === next) return false;
                state.renderFingerprints[key] = next;
                return true;
              },
            };

            const els = {
              shortTermBody,
              tomorrowBody,
              swingBody,
              recommendationActionSummary,
            };

            const status = {
              renderMetrics: () => {},
              setStatus: () => {},
              startPushStatusCountdown: () => {},
            };

            const app = window.TraderRecommendationApp.create({
              state,
              els,
              helpers,
              config,
              status,
            });

            global.fetch = async () => ({
              json: async () => ({
                ok: true,
                recommendations: {
                  short_term: [],
                  tomorrow_picks: [],
                  swing_picks: [],
                },
                meta: {
                  generated_at: "2026-07-14T14:30:00",
                  quote_timestamp: "2026-07-14T14:30:00",
                },
                health: {},
                market_sentiment: {},
              }),
            });

            (async () => {
              await app.loadRecommendations({ background: true });
              process.stdout.write(JSON.stringify({ shortTermBody: shortTermBody.innerHTML }));
            })();
            """
        )
        self.assertIn("暂无符合条件的股票", result["shortTermBody"])


if __name__ == "__main__":
    unittest.main()
