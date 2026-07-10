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


if __name__ == "__main__":
    unittest.main()
