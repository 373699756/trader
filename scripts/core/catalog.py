"""Immutable public actions and private worker allowlisting."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scripts.core.paths import PROJECT_ROOT


@dataclass(frozen=True)
class ToolDefinition:
    identity: str
    business: str
    module: str
    action: str
    worker: str | None = None
    handler_package: str | None = None
    execution: Literal["subprocess", "foreground"] = "subprocess"
    foreground_handler: str | None = None
    reporter: str | None = None
    local_readiness: bool = False
    stdout_report_format: Literal["json", "text"] = "json"
    supports_text: bool = False
    worker_output_kind: Literal["json", "text", "tool"] = "json"
    artifact_output_arguments: tuple[str, ...] = ()
    artifact_directory_arguments: tuple[str, ...] = ()
    effects: tuple[str, ...] = ("read_only",)

    def projection(self) -> dict[str, str]:
        return {"business": self.business, "module": self.module, "action": self.action}


TOOLS = (
    ToolDefinition('disk', 'infra', 'disk', 'profile', None, 'scripts.infra.disk', 'subprocess', None, reporter='scripts.infra.disk.reporting'),
    ToolDefinition('snapshot', 'recommendation', 'pipeline', 'snapshot', None, 'scripts.recommendation.pipeline', 'subprocess', None, reporter='scripts.recommendation.pipeline.reporting', local_readiness=True, effects=('read_current_data', 'isolated_replay')),
    ToolDefinition('web', 'recommendation', 'web', 'health', None, 'scripts.recommendation.web', 'subprocess', None, reporter='scripts.recommendation.web.reporting', effects=('local_http_requests',)),
    ToolDefinition('history', 'download', 'history', 'sources', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('network',)),
    ToolDefinition('security-master', 'download', 'market', 'security-master', None, 'scripts.download.market', 'subprocess', None, reporter='scripts.download.market.reporting', effects=('network',)),
    ToolDefinition('tencent', 'download', 'market', 'quotes', None, 'scripts.download.market', 'subprocess', None, reporter='scripts.download.market.reporting', effects=('network',)),
    ToolDefinition('tencent-transport', 'download', 'market', 'transport', None, 'scripts.download.market', 'subprocess', None, reporter='scripts.download.market.reporting', effects=('network',)),
    ToolDefinition('tushare', 'download', 'market', 'daily', None, 'scripts.download.market', 'subprocess', None, reporter='scripts.download.market.reporting', effects=('network',)),
    ToolDefinition('history-daily-capability', 'download', 'history', 'daily-capability', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('network', 'temporary_databases')),
    ToolDefinition('baostock-concurrency', 'download', 'history', 'concurrency', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('network',)),
    ToolDefinition('baostock-qfq-shadow', 'download', 'history', 'qfq-shadow', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('read_published_history',)),
    ToolDefinition('tencent-download', 'download', 'history', 'download', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('network', 'temporary_databases')),
    ToolDefinition('tencent-history-window', 'download', 'history', 'window', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('network', 'temporary_databases')),
    ToolDefinition('v5-training', 'training', 'v5', 'training', None, 'scripts.training', 'subprocess', None, reporter='scripts.training.reporting', effects=('read_published_history', 'isolated_model_fit'), artifact_directory_arguments=('--v5-probe-directory',)),
    ToolDefinition('v5-samples', 'training', 'v5', 'samples', None, 'scripts.training', 'subprocess', None, reporter='scripts.training.reporting', effects=('isolated_samples',), artifact_directory_arguments=('--training-probe-directory',)),
    ToolDefinition('training-samples', 'training', 'samples', 'comparison', None, 'scripts.training', 'subprocess', None, reporter='scripts.training.reporting', effects=('read_explicit_checkout', 'isolated_samples'), artifact_directory_arguments=('--training-probe-directory',)),
    ToolDefinition('v5-lite', 'training', 'v5', 'lite', None, 'scripts.training', 'subprocess', None, reporter='scripts.training.reporting', effects=('read_sealed_samples', 'isolated_model_fit'), artifact_directory_arguments=('--training-probe-directory',)),
    ToolDefinition('training-alignment', 'training', 'models', 'alignment', None, 'scripts.training', 'subprocess', None, reporter='scripts.training.reporting', effects=('read_published_history', 'isolated_model_fit'), artifact_directory_arguments=('--training-probe-directory',)),
    ToolDefinition('training-coverage', 'training', 'samples', 'coverage', None, 'scripts.training', 'subprocess', None, reporter='scripts.training.reporting'),
    ToolDefinition('training-performance', 'training', 'resources', 'performance', None, 'scripts.training', 'subprocess', None, reporter='scripts.training.reporting', effects=('read_explicit_checkout', 'isolated_model_fit'), artifact_directory_arguments=('--training-probe-directory',)),
    ToolDefinition('training-resources', 'training', 'resources', 'cpu', None, 'scripts.training', 'subprocess', None, reporter='scripts.training.reporting'),
    ToolDefinition('model-inputs', 'training', 'models', 'inputs', None, 'scripts.training', 'subprocess', None, reporter='scripts.training.reporting'),
    ToolDefinition('history-sqlite', 'download', 'history', 'sqlite', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('read_published_history', 'temporary_databases')),
    ToolDefinition('history-publication', 'download', 'history', 'publication', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting'),
    ToolDefinition('history-source-compare', 'download', 'history', 'compare', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('network', 'read_published_history')),
    ToolDefinition('price-repair-audit', 'download', 'history', 'repair', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('network', 'isolated_repair'), artifact_directory_arguments=('--database-probe-directory',)),
    ToolDefinition('market-factor-compare', 'download', 'market', 'factors', None, 'scripts.download.market', 'subprocess', None, reporter='scripts.download.market.reporting', stdout_report_format="text", supports_text=True, effects=('network', 'optional_external_profiles')),
    ToolDefinition('research-source-compare', 'download', 'company', 'sources', None, 'scripts.download.company', 'subprocess', None, reporter='scripts.download.company.reporting', effects=('network',)),
    ToolDefinition('history-write', 'download', 'history', 'write', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('isolated_databases', 'optional_published_history_copy')),
    ToolDefinition('download-performance', 'download', 'persistence', 'performance', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('isolated_databases',)),
    ToolDefinition('database-baseline', 'infra', 'database', 'baseline', None, 'scripts.infra.database', 'subprocess', None, reporter='scripts.infra.database.reporting', effects=('isolated_databases',), artifact_directory_arguments=('--database-probe-directory',)),
    ToolDefinition('company-resume', 'download', 'company', 'coverage', None, 'scripts.download.company', 'subprocess', None, reporter='scripts.download.company.reporting', effects=('read_company_data', 'explicit_network_audit_or_isolated_replay'), artifact_directory_arguments=('--company-replay-directory',)),
    ToolDefinition('company-ledger', 'recommendation', 'web', 'company-ledger', None, 'scripts.recommendation.web', 'subprocess', None, reporter='scripts.recommendation.web.reporting', effects=('isolated_browser',)),
    ToolDefinition('history-space', 'download', 'history', 'space', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting'),
    ToolDefinition('history-industry', 'download', 'history', 'industry', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting', effects=('network', 'read_published_history')),
    ToolDefinition('qfq-sqlite', 'download', 'history', 'qfq', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting'),
    ToolDefinition('download-failures', 'download', 'history', 'failures', None, 'scripts.download.history', 'subprocess', None, reporter='scripts.download.history.reporting'),
    ToolDefinition('research', 'recommendation', 'pipeline', 'readiness', None, 'scripts.recommendation.pipeline', 'subprocess', None, reporter='scripts.recommendation.pipeline.reporting'),
    ToolDefinition('long-watchlist', 'recommendation', 'long', 'admission', None, 'scripts.recommendation.long', 'subprocess', None, reporter='scripts.recommendation.long.reporting', effects=('read_company_data', 'optional_external_evidence'), artifact_output_arguments=('--long-evidence-output',)),
    ToolDefinition('browser', 'recommendation', 'web', 'browser', None, 'scripts.recommendation.web', 'subprocess', None, reporter='scripts.recommendation.web.reporting', effects=('isolated_browser',)),
    ToolDefinition('browser-live', 'recommendation', 'web', 'browser-live', None, 'scripts.recommendation.web', 'subprocess', None, reporter='scripts.recommendation.web.reporting', effects=('browser', 'local_http_requests')),
    ToolDefinition('performance', 'recommendation', 'pipeline', 'latency', None, 'scripts.recommendation.pipeline', 'subprocess', None, reporter='scripts.recommendation.pipeline.reporting', effects=('isolated_replay',)),
    ToolDefinition('quality-debt', 'engineering', 'quality', 'debt', 'scripts.engineering.quality.debt', None, 'subprocess', None, reporter=None, worker_output_kind="text", effects=('read_sources', 'ruff_lint')),
    ToolDefinition('quality-transform', 'engineering', 'quality', 'transform', 'scripts.engineering.quality.cli', None, 'subprocess', None, reporter=None, worker_output_kind="tool", effects=('explicit_source_transform',)),
    ToolDefinition('wheel-install', 'engineering', 'packaging', 'install', 'scripts.engineering.packaging.cli', None, 'subprocess', None, reporter='scripts.engineering.packaging.reporting', effects=('isolated_environment_install',)),
    ToolDefinition('memory', 'training', 'resources', 'memory', 'scripts.training.resources.memory', None, 'subprocess', None, reporter='scripts.training.reporting', artifact_output_arguments=("--output",), effects=('isolated_model_fit', 'external_results')),
    ToolDefinition('skill-evaluation', 'engineering', 'skills', 'evaluate', 'scripts.engineering.skills.cli', None, 'subprocess', None, reporter=None, worker_output_kind="tool", artifact_directory_arguments=("--output-dir",), effects=('paid_model_requests_on_run', 'external_outputs')),
    ToolDefinition('assets', 'recommendation', 'assets', 'consistency', None, None, 'foreground', 'scripts.recommendation.assets.cli', reporter=None),
    ToolDefinition('migration', 'engineering', 'migration', 'verify', None, None, 'foreground', 'scripts.engineering.migration.cli', reporter=None, artifact_directory_arguments=('--source',)),
    ToolDefinition('assets-generate', 'recommendation', 'assets', 'generate', None, None, 'foreground', 'scripts.recommendation.assets.cli', reporter=None, effects=('writes_packaged_asset',)),
    ToolDefinition('migration-build', 'engineering', 'migration', 'build', None, None, 'foreground', 'scripts.engineering.migration.cli', reporter=None, effects=('writes_external_candidate',), artifact_directory_arguments=('--source', '--target')),
    ToolDefinition('migration-rollback', 'engineering', 'migration', 'rollback', None, None, 'foreground', 'scripts.engineering.migration.cli', reporter=None, effects=('replaces_external_candidate',), artifact_directory_arguments=('--target', '--backup')),
    ToolDefinition('history-repack-build', 'download', 'history', 'repack-build', None, None, 'foreground', 'scripts.download.history.repack', reporter=None, effects=('writes_repack_candidate',), artifact_directory_arguments=('--source', '--target')),
    ToolDefinition('history-repack-activate', 'download', 'history', 'repack-activate', None, None, 'foreground', 'scripts.download.history.repack', reporter=None, effects=('switches_history_roots',), artifact_directory_arguments=('--source', '--target')),
    ToolDefinition('history-repack-rollback', 'download', 'history', 'repack-rollback', None, None, 'foreground', 'scripts.download.history.repack', reporter=None, effects=('switches_history_roots',), artifact_directory_arguments=('--source', '--target')),
    ToolDefinition('history-repack-finalize', 'download', 'history', 'repack-finalize', None, None, 'foreground', 'scripts.download.history.repack', reporter=None, effects=('deletes_verified_backup',), artifact_directory_arguments=('--source', '--target')),
    ToolDefinition('company-live-sync', 'download', 'company', 'live-sync', None, None, 'foreground', 'scripts.download.company.live_sync', artifact_output_arguments=('--measurement-output',), effects=('network', 'writes_current_company_data')),
)

WORKERS = (('scripts.recommendation.web.web_health', 'Main'), ('scripts.training.models.v5_training', 'Main'), ('scripts.training.samples.v5_samples', 'Main'), ('scripts.training.models.v5_lite', 'Main'), ('scripts.download.market.tushare_daily', 'Main'), ('scripts.training.samples.training_samples', 'Main'), ('scripts.training.resources.training_resources', 'Main'), ('scripts.training.samples.training_read_comparison', 'Main'), ('scripts.training.samples.training_coverage', 'Main'), ('scripts.training.jobs.training_alignment', 'Main'), ('scripts.download.market.tencent_quotes', 'Main'), ('scripts.download.history.tencent_download', 'Main'), ('scripts.training.samples.sample_comparison_worker', 'Main'), ('scripts.download.company.research_source_compare', 'Main'), ('scripts.download.history.qfq_sqlite', 'Main'), ('scripts.download.history.price_repair_audit', 'Main'), ('scripts.download.market.market_factor_compare', 'Main'), ('scripts.recommendation.long.long_watchlist', 'Main'), ('scripts.download.history.history_write_benchmark', 'Main'), ('scripts.download.history.history_sqlite_performance', 'Main'), ('scripts.download.history.history_sources', 'Main'), ('scripts.download.history.history_source_compare', 'Main'), ('scripts.download.history.history_publication_audit', 'Main'), ('scripts.download.history.history_industry_preparation', 'Main'), ('scripts.download.history.history_daily_capability', 'Main'), ('scripts.download.market.exchange_security_master', 'Main'), ('scripts.download.history.download_performance', 'Main'), ('scripts.infra.disk.disk_profile', 'Main'), ('scripts.infra.database.database_layout_comparison', 'Main'), ('scripts.infra.database.database_baseline', 'Main'), ('scripts.download.company.company_resume', 'Main'), ('scripts.recommendation.web.company_ledger_browser', 'Main'), ('scripts.recommendation.web.browser_refresh', 'Main'), ('scripts.download.history.baostock_qfq_shadow', 'Main'), ('scripts.download.history.baostock_concurrency', 'Main'), ('scripts.engineering.quality.debt', 'Main'), ('scripts.training.resources.memory', 'Main'), ('scripts.engineering.packaging.cli', 'Main'), ('scripts.engineering.quality.cli', 'Main'), ('scripts.engineering.skills.cli', 'Main'), ('scripts.engineering.quality.rename_worker', 'Main'))


def ToolByIdentity(identity: str) -> ToolDefinition:
    for definition in TOOLS:
        if definition.identity == identity:
            return definition
    raise ValueError("unknown tool identity")


def SelectTool(business: str, module: str, action: str) -> ToolDefinition:
    for definition in TOOLS:
        if (definition.business, definition.module, definition.action) == (business, module, action):
            return definition
    raise ValueError("unknown business/module/action selection; use --list")


def SelectionArguments(identity: str) -> tuple[str, ...]:
    definition = ToolByIdentity(identity)
    return ("--business", definition.business, "--module", definition.module, "--action", definition.action)


def WorkerArguments(argv: tuple[str, ...]) -> tuple[str, ...]:
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] in {name for name, _ in WORKERS}:
        return (argv[0], str(PROJECT_ROOT / "scripts/tools.py"), "--internal-worker", argv[2], "--", *argv[3:])
    return argv
