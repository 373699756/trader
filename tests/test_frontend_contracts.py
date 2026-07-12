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
        self.assertIn("险31", result["scoreHtml"])
        self.assertIn("模型影子", result["scoreHtml"])
        self.assertIn("E+1.25%", result["scoreHtml"])
        self.assertIn("P61%", result["scoreHtml"])
        self.assertIn("收益模型", result["explanation"])
        self.assertIn("预测净收益+1.25%", result["explanation"])
        self.assertIn("模型胜率61.2%", result["explanation"])
        self.assertIn("置信影子", result["explanation"])
        self.assertNotIn("影子排序分", result["explanation"])

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

    def test_stock_diagnosis_always_requests_deepseek_review(self):
        with open("static/app.js", encoding="utf-8") as source:
            app_source = source.read()
        with open("templates/index.html", encoding="utf-8") as source:
            template_source = source.read()

        self.assertIn("/api/stock-prediction/${encodeURIComponent(code)}?deepseek=1", app_source)
        self.assertIn("本地量化 + DeepSeek", app_source)
        self.assertIn('id="stockPredictionBtn" class="primary-action" type="button">走势预测</button>', template_source)
        self.assertIn('id="generateTuningBtn" class="primary-action" type="button">DeepSeek 策略验证</button>', template_source)
        self.assertIn('class="tool-action-divider"', template_source)

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


if __name__ == "__main__":
    unittest.main()
