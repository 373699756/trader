"""Render the production score column and drawer in isolated desktop Chrome."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from datetime import datetime
from html import unescape
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from trader.recommendation.domain.evidence.review import RiskFact
from trader.recommendation.domain.publication.score_ledger import ScoreAdjustmentItem, ScoreLedgerPayload
from trader.recommendation.domain.scoring.company import (
    BuildScoreLedger,
    CompanyAdjustments,
    CompanyScoringPolicy,
    ScoreLedgerInput,
)

from .reporting import emit_report


def Main() -> int:
    browser = os.environ.get("TRADER_DIAGNOSTIC_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
    if browser is None:
        emit_report({"schema_version": "company-ledger-browser", "status": "failed", "reason": "chrome_missing"})
        return 1
    static = Path(__file__).resolve().parents[2] / "src/trader/web/static"
    now = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    adjustments = CompanyAdjustments(
        (
            ScoreAdjustmentItem(
                "buyback",
                "positive_bonus",
                "实际回购",
                3,
                3,
                0,
                "eligible",
                ("fixture-notice",),
                now,
            ),
        ),
        (),
        0,
        now.date(),
    )
    ledger = BuildScoreLedger(
        ScoreLedgerInput(99, adjustments, (RiskFact("market", "high_volatility", "high", 8, "structured", now),)),
        strategy="d25",
        policy=CompanyScoringPolicy(enabled=True),
        risk_cap=25,
    )
    item = {
        "code": "600001",
        "name": "示例公司",
        "board": "main",
        "industry": "制造",
        "rank": 1,
        "tier": "recommend",
        "action": "executable",
        "action_reason": "threshold_met",
        "price": 10,
        "pct_change": 1,
        "turnover_rate": 2,
        "amount": 100000000,
        "market_cap": 2000000000,
        "review_outcome": "applied",
        "scores": {"local_score": 92, "final_score": 92, "ledger": ScoreLedgerPayload(ledger)},
    }
    script = """
const item = FIXTURE;
const results = [];
for (const profile of ['v2', 'v1', 'legacy']) {
  const snapshot = {strategy: 'd25', input_versions: {score_scale: profile === 'v1' ? 'learned_rank_percentile_0_100' : 'weighted_evidence_quality_0_100'}};
  const current = JSON.parse(JSON.stringify(item));
  if (profile === 'v1') current.scores.final_score = 97;
  if (profile === 'legacy') delete current.scores.ledger;
  const container = document.createElement('section');
  const table = TraderRender.currentTable(snapshot);
  container.innerHTML = `<table><colgroup>${table.columns}</colgroup><thead>${table.head}</thead><tbody>${TraderRender.tableRows([current], snapshot)}</tbody></table><article>${TraderRender.drawer(current, snapshot)}</article>`;
  document.body.append(container);
  const row = container.querySelector('tbody tr');
  const score = row.cells[9].textContent;
  const detail = container.querySelector('article').textContent;
  results.push({profile, columns: row.cells.length, score, detail,
    ledgerVisible: profile === 'legacy' ? score.includes('无加减分明细') : score.includes('+1.00') && score.includes('−8.00'),
    drawerVisible: profile === 'legacy' ? detail.includes('未保存加减分账本') : detail.includes('实际加分') && detail.includes('实际扣分'),
    evidenceButton: profile === 'legacy' || !!container.querySelector('[data-company-documents]'),
    finiteLayout: Array.from(container.querySelectorAll('.score-stack span')).every(element => element.getBoundingClientRect().width > 0 && element.scrollWidth <= element.clientWidth + 1),
    modelDistinct: profile !== 'v1' || score.includes('本地 92.00') && detail.includes('模型排序分')});
}
const result = document.createElement('pre'); result.id = 'company-probe';
result.textContent = JSON.stringify(results.map(({detail, ...rest}) => rest)); document.body.append(result);
""".replace("FIXTURE", json.dumps(item, ensure_ascii=False))
    html = (
        "<!doctype html><meta charset='utf-8'><title>Company ledger fixture</title>"
        f"<link rel='stylesheet' href='{(static / 'dashboard.css').as_uri()}'>"
        "<style>body{padding:20px}section{margin-bottom:24px}table{width:1350px;table-layout:fixed}article{width:760px}</style>"
        f"<script src='{(static / 'render.js').as_uri()}'></script><body><script>{script}</script></body>"
    )
    with TemporaryDirectory(prefix="trader-company-browser-") as directory:
        root = Path(directory)
        page = root / "ledger.html"
        page.write_text(html, encoding="utf-8")
        completed = _run_browser(
            [
                browser,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--no-first-run",
                "--no-default-browser-check",
                "--timeout=10000",
                "--virtual-time-budget=3000",
                f"--user-data-dir={root / 'profile'}",
                "--window-size=1500,1100",
                "--dump-dom",
                page.as_uri(),
            ],
        )
        match = re.search(r'<pre id="company-probe">(.*?)</pre>', completed.stdout, re.DOTALL)
        rows = json.loads(unescape(match[1])) if match is not None else []
        passed = (
            completed.returncode == 0
            and len(rows) == 3
            and all(
                row["columns"] == 11
                and all(
                    row[key]
                    for key in ("ledgerVisible", "drawerVisible", "evidenceButton", "finiteLayout", "modelDistinct")
                )
                for row in rows
            )
        )
        version = subprocess.run(
            [browser, "--version"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        emit_report(
            {
                "schema_version": "company-ledger-browser",
                "status": "passed" if passed else "failed",
                "summary": {
                    "fixture": True,
                    "browser": version,
                    "results": rows,
                    "data_source": "isolated_static_fixture",
                },
                "findings": []
                if passed
                else [
                    {
                        "reason": "browser_fixture_failed",
                        "process_code": completed.returncode,
                        "stderr": completed.stderr[-800:],
                    }
                ],
            }
        )
        return 0 if passed else 1


def _run_browser(command: list[str]) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(command, 124, stdout, "browser_timeout")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


if __name__ == "__main__":
    raise SystemExit(Main())
