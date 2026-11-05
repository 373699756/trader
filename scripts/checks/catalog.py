"""Single source for public selectors and private worker allowlisting."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class CheckDefinition:
    identity: str
    business: str
    module: str
    check: str
    commands: tuple[str, ...]
    worker: str | None = None

    def projection(self) -> dict[str, str]:
        return {"business": self.business, "module": self.module, "check": self.check}

CHECKS = (
    CheckDefinition('disk', 'infra', 'disk', 'profile', ('disk_profile',)),
    CheckDefinition('snapshot', 'recommendation', 'pipeline', 'snapshot', ('configuration', 'history_maintenance', 'research_readiness', 'production_performance')),
    CheckDefinition('web', 'recommendation', 'web', 'health', ('web_health',)),
    CheckDefinition('history', 'download', 'history', 'sources', ('history_sources',)),
    CheckDefinition('security-master', 'download', 'market', 'security-master', ('exchange_security_master',)),
    CheckDefinition('tencent', 'download', 'market', 'quotes', ('tencent_quotes',)),
    CheckDefinition('tencent-transport', 'download', 'market', 'transport', ('tencent_transport',)),
    CheckDefinition('tushare', 'download', 'market', 'daily', ('tushare_daily',)),
    CheckDefinition('history-daily-capability', 'download', 'history', 'daily-capability', ('history_daily_capability',)),
    CheckDefinition('baostock-concurrency', 'download', 'history', 'concurrency', ('baostock_concurrency',)),
    CheckDefinition('baostock-qfq-shadow', 'download', 'history', 'qfq-shadow', ('baostock_qfq_shadow',)),
    CheckDefinition('tencent-download', 'download', 'history', 'download', ('tencent_download',)),
    CheckDefinition('tencent-history-window', 'download', 'history', 'window', ('tencent_history_window',)),
    CheckDefinition('v5-training', 'training', 'v5', 'training', ('v5_training',)),
    CheckDefinition('v5-samples', 'training', 'v5', 'samples', ('v5_samples',)),
    CheckDefinition('training-samples', 'training', 'samples', 'comparison', ('training_samples',)),
    CheckDefinition('v5-lite', 'training', 'v5', 'lite', ('v5_lite',)),
    CheckDefinition('training-alignment', 'training', 'models', 'alignment', ('training_alignment',)),
    CheckDefinition('training-coverage', 'training', 'samples', 'coverage', ('training_coverage',)),
    CheckDefinition('training-performance', 'training', 'resources', 'performance', ('training_alignment',)),
    CheckDefinition('training-resources', 'training', 'resources', 'cpu', ('training_resources',)),
    CheckDefinition('model-inputs', 'training', 'models', 'inputs', ('model_inputs',)),
    CheckDefinition('history-sqlite', 'download', 'history', 'sqlite', ('history_sqlite_performance',)),
    CheckDefinition('history-publication', 'download', 'history', 'publication', ('history_publication_audit',)),
    CheckDefinition('history-source-compare', 'download', 'history', 'compare', ('history_source_compare',)),
    CheckDefinition('price-repair-audit', 'download', 'history', 'repair', ('price_repair_audit',)),
    CheckDefinition('market-factor-compare', 'download', 'market', 'factors', ('market_factor_compare',)),
    CheckDefinition('research-source-compare', 'download', 'company', 'sources', ('research_source_compare',)),
    CheckDefinition('history-write', 'download', 'history', 'write', ('history_write_benchmark',)),
    CheckDefinition('download-performance', 'download', 'persistence', 'performance', ('download_performance',)),
    CheckDefinition('database-baseline', 'infra', 'database', 'baseline', ('database_baseline',)),
    CheckDefinition('company-resume', 'download', 'company', 'coverage', ('company_resume',)),
    CheckDefinition('company-ledger', 'download', 'company', 'browser', ('company_ledger_browser',)),
    CheckDefinition('history-space', 'download', 'history', 'space', ('history_space',)),
    CheckDefinition('history-industry', 'download', 'history', 'industry', ('history_industry',)),
    CheckDefinition('qfq-sqlite', 'download', 'history', 'qfq', ('qfq_sqlite_audit',)),
    CheckDefinition('download-failures', 'download', 'history', 'failures', ('download_failures',)),
    CheckDefinition('research', 'recommendation', 'pipeline', 'readiness', ('research_readiness',)),
    CheckDefinition('long-watchlist', 'recommendation', 'long', 'admission', ('long_watchlist_admission',)),
    CheckDefinition('browser', 'recommendation', 'web', 'browser', ('browser_refresh',)),
    CheckDefinition('browser-live', 'recommendation', 'web', 'browser-live', ('browser_live',)),
    CheckDefinition('performance', 'recommendation', 'pipeline', 'latency', ('production_performance',)),
    CheckDefinition('quality-debt', 'engineering', 'quality', 'debt', (), 'scripts.checks.engineering.quality.debt'),
    CheckDefinition('quality-transform', 'engineering', 'quality', 'transform', (), 'scripts.checks.engineering.quality.cli'),
    CheckDefinition('wheel-install', 'engineering', 'packaging', 'install', (), 'scripts.checks.engineering.packaging'),
    CheckDefinition('memory', 'training', 'resources', 'memory', (), 'scripts.checks.training.memory'),
    CheckDefinition('skill-evaluation', 'engineering', 'skills', 'evaluate', (), 'scripts.checks.engineering.skills.cli'),
    CheckDefinition('assets', 'engineering', 'assets', 'consistency', (), 'scripts.checks.engineering.assets'),
    CheckDefinition('migration', 'engineering', 'migration', 'verify', (), 'scripts.checks.engineering.migration'),
)

WORKERS = (('scripts.checks.recommendation.web_health', 'Main'), ('scripts.checks.training.v5_training', 'Main'), ('scripts.checks.training.v5_samples', 'Main'), ('scripts.checks.training.v5_lite', 'Main'), ('scripts.checks.download.market.tushare_daily', 'Main'), ('scripts.checks.training.training_samples', 'Main'), ('scripts.checks.training.training_resources', 'Main'), ('scripts.checks.training.training_read_comparison', 'Main'), ('scripts.checks.training.training_coverage', 'Main'), ('scripts.checks.training.training_alignment', 'Main'), ('scripts.checks.download.market.tencent_quotes', 'Main'), ('scripts.checks.download.history.tencent_download', 'Main'), ('scripts.checks.training.sample_comparison_worker', 'Main'), ('scripts.checks.download.company.research_source_compare', 'Main'), ('scripts.checks.download.history.qfq_sqlite', 'Main'), ('scripts.checks.download.history.price_repair_audit', 'Main'), ('scripts.checks.download.market.market_factor_compare', 'Main'), ('scripts.checks.recommendation.long_watchlist', 'Main'), ('scripts.checks.download.history.history_write_benchmark', 'Main'), ('scripts.checks.download.history.history_sqlite_performance', 'Main'), ('scripts.checks.download.history.history_sources', 'Main'), ('scripts.checks.download.history.history_source_compare', 'Main'), ('scripts.checks.download.history.history_publication_audit', 'Main'), ('scripts.checks.download.history.history_industry_preparation', 'Main'), ('scripts.checks.download.history.history_daily_capability', 'Main'), ('scripts.checks.download.market.exchange_security_master', 'Main'), ('scripts.checks.download.history.download_performance', 'Main'), ('scripts.checks.infra.disk_profile', 'Main'), ('scripts.checks.infra.database_layout_comparison', 'Main'), ('scripts.checks.infra.database_baseline', 'Main'), ('scripts.checks.download.company.company_resume', 'Main'), ('scripts.checks.download.company.company_ledger_browser', 'Main'), ('scripts.checks.recommendation.browser_refresh', 'Main'), ('scripts.checks.download.history.baostock_qfq_shadow', 'Main'), ('scripts.checks.download.history.baostock_concurrency', 'Main'), ('scripts.checks.engineering.quality.debt', 'Main'), ('scripts.checks.training.memory', 'Main'), ('scripts.checks.engineering.packaging', 'Main'), ('scripts.checks.engineering.quality.cli', 'Main'), ('scripts.checks.engineering.skills.cli', 'Main'), ('scripts.checks.engineering.assets', 'Main'), ('scripts.checks.engineering.migration', 'Main'), ('scripts.checks.engineering.quality.rename_worker', 'Main'))

def CheckByIdentity(identity: str) -> CheckDefinition:
    for definition in CHECKS:
        if definition.identity == identity:
            return definition
    raise ValueError("unknown diagnostic identity")

def SelectCheck(business: str, module: str, check: str) -> CheckDefinition:
    for definition in CHECKS:
        if (definition.business, definition.module, definition.check) == (business, module, check):
            return definition
    raise ValueError("unknown business/module/check selection; use --list")

def SelectionArguments(identity: str) -> tuple[str, ...]:
    definition = CheckByIdentity(identity)
    return ("--business", definition.business, "--module", definition.module, "--check", definition.check)

def WorkerArguments(argv: tuple[str, ...]) -> tuple[str, ...]:
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] in {name for name, _ in WORKERS}:
        return (argv[0], "-m", "scripts.diagnose_runtime", "--internal-worker", argv[2], "--", *argv[3:])
    return argv
