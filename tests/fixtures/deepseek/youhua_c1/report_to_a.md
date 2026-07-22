# Codex C C1.x DeepSeek 盘点报告

codex_and_phase: `Codex C / C1.x`

base_commit: `45bd2fab992d36eb873b7c448fbd9739f0cad43c`

head_commit_or_patch: `patch only; no production code change; waiting for CONTRACT_BASE/G1`

owned_paths_changed:

- `tests/fixtures/deepseek/youhua_c1/report_to_a.md`

contract_assumptions:

- C 当前只提交阶段 1 盘点报告，不进入 C2 实现。
- C2 只修改 `src/trader/infra/deepseek/`、`src/trader/domain/review/`、新闻事实/本地映射相关文件及其测试。
- C 不修改 Polars、P6/Web、公共 ports/events、pipeline、publisher、bootstrap、配置接线或权威文档。
- `youhua_contract_base_v1`、`p4_p5_high_value_review_manifest_v1` 和 `deepseek_v4_review_facts_v1` 由 A 作为唯一公共 owner 冻结。
- long 的目标契约是复核集合永久为空、DeepSeek 物理 HTTP 请求永久为 0；当前代码仍存在 long review 入口，C2 需在 C 租约内实现隔离，并向 A 申请公共调用方配合。

schema_or_migration_changes:

- 本阶段无 schema 或 migration 变更。
- 当前主审 schema 常量为 `SCHEMA_VERSION = "deepseek_review_v3"`，prompt 常量为 `PROMPT_VERSION = "deepseek_review_prompt_v3"`。
- 当前挑战者 schema 常量为 `CHALLENGER_SCHEMA_VERSION = "deepseek_challenger_v1"`，prompt 常量为 `CHALLENGER_PROMPT_VERSION = "deepseek_challenger_prompt_v1"`。
- 当前预算 SQLite schema 为 `src/trader/infra/deepseek/budget.py` 中 `SCHEMA_VERSION = 3`。
- C2 预期实现新的 V4 facts 内部 schema，但公共事实版本必须复用 A 冻结的 `deepseek_v4_review_facts_v1`，不得自建第二套公共版本。

tests_run_and_results:

- `.venv/bin/python -m pytest tests/unit/test_v2_deepseek_base.py tests/component/test_v2_deepseek.py tests/component/test_v2_deepseek_v4.py -q`
  - result: pass
  - coverage count: 75 collected tests
  - warnings: 9 RuntimeWarning entries from fixture model name `model` being unknown; existing test fixture behavior, not C1 regression
- C1 request baseline script using existing `tests/component/test_v2_deepseek.py` fixture:
  - today/today_main: `applied`, `cache_hits=0`, `physical_attempts=1`
  - tomorrow/afternoon: `applied`, `cache_hits=0`, `physical_attempts=1`
  - d25/afternoon: `applied`, `cache_hits=1`, `physical_attempts=0`
  - physical calls: `2`, models `["deepseek-v4-flash", "deepseek-v4-flash"]`
  - budget used: `2`
  - budget by bucket: `{"today": 1, "tomorrow": 1}`
  - budget by stage: `{"today_main": 1, "tomorrow_afternoon": 1, "d25_afternoon": 0, "long_afternoon": 0, "shared_preheat": 0, "emergency": 0}`
- `git diff --check`
  - result: pass

performance_before_after:

- Not applicable for C1; no production implementation changed.
- Existing fixture request baseline above records physical calls and cache reuse only.
- C2 must report process peak RSS for maximum Flash batch, Pro batch, schema repair and response validation as required by `docs/plan_youhua.md`.

## C1.1 Review 盘点

现有 Review/domain 边界：

- `src/trader/domain/review/models.py` 定义 `ReviewOutcome`、`RiskFact`、`RiskRule`、`DimensionAssessment`、`DeepSeekReview`、`ReviewCandidateContext`。
- `src/trader/domain/review/rules.py` 执行本地风险事实派生、模型风险事实 evidence 校验、去重、封顶和 veto 映射。
- `src/trader/application/ports/reviews.py` 是当前公共 review port，C 不直接修改。

现有 DeepSeek infra 拆分：

- `schema.py`: 主审 prompt、schema parsing、repair prompt、manifest/cache key、策略分类。
- `challenger.py`: Pro 挑战者 prompt、schema parsing、保守合并。
- `reviewer.py`: review/preheat/emergency 编排、批次状态、缓存、预算。
- `reviewer_requests.py`: 主审/挑战者 HTTP 执行、schema repair、attempt 完成写回。
- `reviewer_support.py`: deadline、priority、高价值启发、emergency 原因、candidate 去重。
- `budget.py`、`budget_batch_store.py`、`budget_reporting.py`、`budget_support.py`: SQLite 原子预算、批次、恢复和状态汇总。
- `cache.py`: raw review 与 strategy-classified review 双层缓存。
- `client.py`、`base_client.py`、`model_capabilities.py`、`model_catalog.py`: HTTP transport、模型能力和模型校验。
- `evidence_router.py`: 点时 evidence 过滤、去重、分槽和 prompt 选择。

现有 model/prompt/schema：

- 主审默认模型配置为 `deepseek-v4-flash`。
- 挑战者默认模型配置为 `deepseek-v4-pro`。
- 主审 prompt 要求只输出五维 dimensions 和 risk_facts，不允许目标价、收益保证、排名或交易指令。
- 主审 schema 当前是五维分数/置信度模型，不是计划 C2 的 V4 facts schema；这是 C2 的主要 schema 缺口。
- 模型输出的 `rating` 会被 parse 为受控枚举，但仅审计使用，不直接进入生产分数。

现有缓存：

- raw cache key 包含 code、board、board policy、population、merge_epoch、parameter status、board reliability、结构化 features、prompt evidence、external risk facts、model、model_role、thinking_mode、generation、schema、prompt。
- fusion cache key 增加 strategy、strategy version、dimension weights、confidence coverage、known dimension threshold、challenger identity/status。
- raw cache 对价格变化超过 1% 或量比变化超过 0.3 失效；策略分类缓存按 strategy 隔离。

现有预算/重试/状态：

- `DeepSeekBudgetStore` 用 SQLite `BEGIN IMMEDIATE` 做原子预留。
- 每个物理 attempt 在调用前预留；失败、超时、HTTP retry 和 schema repair 均消耗预算。
- HTTP `maximum_attempts` 当前限制为 1 到 2；repair 使用额外一次 `maximum_attempts=1`。
- `recover_incomplete()` 把重启遗留 reserved/running 标记为 `abandoned` 并同步 audit。
- status 暴露 batch、candidate outcomes、cache hits、physical attempts、budget summary、HTTP 429、timeout、token 和 model role 计数。

## C1.2 业务盘点

long 调用入口：

- `src/trader/application/pipeline_stages.py` 当前在常规策略 review 之后单独提交 long review；这会允许 long 消耗 DeepSeek 主审预算。
- `src/trader/application/recommendation_finalization.py` 和 `src/trader/application/recommendations.py` 的通用 finalize 路径在传入 review port 且有 eligible 时调用 review；直接调用方若传 long 也会进入 DeepSeek。
- `src/trader/infra/deepseek/reviewer_requests.py` 已禁止 long challenger，但未禁止 long primary、preheat 或 emergency。
- `config/v2/runtime.json` 当前仍有 `strategy_limits.long = 18`、`stage_targets.long_afternoon = 10`、`stage_limits.long_afternoon = 18`，与 C2 long 零请求目标冲突；配置和公共契约由 A owner 处理。

新闻公告证据：

- `src/trader/infra/deepseek/evidence_router.py` 当前支持 `announcement` 和 `news`，按 72 小时 TTL、来源质量、类型/发布时间/title hash 去重。
- `src/trader/infra/market_data/akshare_news.py` 从东财搜索结果规范化 `news` Evidence。
- `src/trader/domain/market/news.py` 当前以关键词派生新闻情绪和 freshness；C2 需要增加更严格的本地点时过滤、来源分级、事件级去重和“官方或两个独立可信来源支持正向事实才可加分”的规则。

融合与风险处罚：

- `src/trader/domain/recommendation/fusion.py` 已按 `local_score * 0.68 + deepseek_score * 0.32 - deepseek_risk_penalty` 计算，不重复扣 `local_risk_penalty`。
- 固定融合验收向量由 `tests/unit/domain/test_fusion.py` 覆盖，结果为 `83.40`。
- DeepSeek raw risk fact 初始 `penalty=0`、`veto=False`；只有经 `map_deepseek_risk_facts()` 通过本地 `RiskRule`、证据 ID、证据类型、TTL 和置信度校验后才形成 `deepseek_risk_penalty` 或 veto。

旧冻结回放：

- `src/trader/application/recommendation_replay.py` 使用 `_RecordedReviewPort` 注入旧 review，不调用真实 DeepSeek。
- `src/trader/infra/persistence/snapshot_replay.py` 负责从持久化记录还原 review mapping。
- C2 不应改变旧冻结按自身版本回放的契约。

失败降级：

- DeepSeek disabled/api key missing/deadline reached 会生成 `REJECTED` 或 `LATE` 终态，并完成 batch 状态。
- HTTP retryable 状态、timeout、RequestException、JSON/schema 失败和 finish_reason length 均进入 bounded retry/repair 或 rejected/late。
- Recommendation 融合层在 review 未 applied 或保护集合未完整时回退本地分和 `local_degraded` 模式。

## C1.3 请求/预算基线

当前配置向量：

- model: `deepseek-v4-flash`
- challenger_model: `deepseek-v4-pro`
- timeout_seconds: `12`
- batch_size: `8`
- max_tokens: `1800`
- daily_hard_limit: `188`
- strategy_limits: `today=70`、`tomorrow=45`、`d25=35`、`long=18`、`shared_preheat=15`、`emergency=5`
- challenger_limits: `today=6`、`tomorrow=6`、`d25=5`、`long=0`
- stage_targets: `shared_preheat=15`、`today_observe=14`、`today_main=42`、`today_late=12`、`tomorrow_afternoon=21`、`tomorrow_final=14`、`d25_afternoon=19`、`d25_final=11`、`long_afternoon=10`、`emergency=0`
- stage_limits: `shared_preheat=15`、`today_observe=15`、`today_main=42`、`today_late=13`、`tomorrow_afternoon=25`、`tomorrow_final=20`、`d25_afternoon=22`、`d25_final=13`、`long_afternoon=18`、`emergency=5`

现有测试覆盖：

- schema 验证、证据引用限制、未知维度、rating 审计、模型供应商字段不被信任。
- HTTP retry、timeout、non-retryable HTTP、finish_reason length、schema repair、repair 最终失败。
- SQLite 原子预算、stage limit、emergency 原因、重启恢复、batch/candidate terminal。
- Flash/Pro 模型能力、挑战者审计字段、挑战者 repair 和 strategy-scoped challenger cache。
- raw cache 对 quote-only data version 变化复用、对价格/量比变化失效、strategy-independent raw review 可复用。
- long 当前存在“复用 d25 raw review”的测试基线；C2 必须按新 contract 改为 long 零请求/零复核集合，并同步更新相关测试。

## C1.4 接口申请

requested_interface_changes:

1. 高价值复核输入 `HighValueReviewInput`
   - owner: A public schema，C consumer
   - minimum fields:
     - `contract_version`
     - `strategy`
     - `trade_date`
     - `phase`
     - `deadline`
     - `owner_strategy`
     - `candidate_code`
     - `feature_snapshot_identity`
     - `local_score`
     - `local_rank`
     - `action_threshold`
     - `in_protection_set`
     - `near_action_threshold`
     - `near_global_boundary`
     - `topk_boundary`
     - `has_new_high_risk`
     - `has_new_catalyst`
     - `direction_conflict`
     - `evidence_conflict`
     - `was_reviewed`
     - `evidence_manifest_hash`
     - `price_reaction_bucket`
     - `budget_bucket`
   - compatibility: can be adapted from existing `ReviewCandidateContext` plus `FeatureSnapshot` without exposing Polars or Web types.
   - failure tests requested from A: long input collection is empty; invalid/missing identity rejects; Top18/Top10 and action-threshold boundary candidates are selected deterministically.

2. V4 facts output `DeepSeekV4Facts`
   - owner: A public schema，C implementation
   - minimum fields:
     - `contract_version = deepseek_v4_review_facts_v1`
     - `code`
     - `abstain`
     - `catalyst.direction`
     - `catalyst.importance`
     - `catalyst.confirmation`
     - `catalyst.cycle`
     - `catalyst.evidence_ids`
     - `price_reaction.bucket`
     - `price_reaction.evidence_ids`
     - `fundamental.direction`
     - `industry_policy.direction`
     - `risks.regulatory`
     - `risks.shareholder_reduction`
     - `risks.unlock`
     - `risks.pledge`
     - `risks.litigation`
     - `risks.earnings`
     - `conflicts`
     - `coverage`
   - compatibility: C can map facts into current `DeepSeekReview`/`DimensionAssessment` internally during migration, but public schema must not expose old five-dimension scoring as the contract.
   - failure tests requested from A: unknown fields reject; model-supplied penalty/veto/action/rank reject; evidence outside manifest rejects; all unsupported facts produce `abstain`.

3. Evidence manifest and price reaction bucket
   - owner: A public schema，C computes/consumes
   - minimum fields:
     - `manifest_hash`
     - `evidence_id`
     - `evidence_type`
     - `source_tier`
     - `source`
     - `published_at`
     - `received_at`
     - `data_version`
     - `event_key`
     - `supports_positive_fact`
     - `counter_evidence`
     - `price_reaction_bucket`
   - compatibility: existing `Evidence` can supply IDs, type, source, published/received time and version; C2 needs event/source classification additions in C-owned evidence routing.
   - failure tests requested from A: future/naive/expired evidence excluded; single soft-news positive fact does not add score; official or two independent trusted sources are required for positive fact uplift.

4. Owner strategy and cache/budget identity
   - owner: A public schema，C consumer
   - minimum fields:
     - `owner_strategy`
     - `consumer_strategy`
     - `generation`
     - `budget_bucket`
     - `model_role`
     - `model`
     - `thinking_mode`
     - `prompt_version`
     - `schema_version`
     - `config_version`
   - compatibility: raw facts may be strategy-independent only when action explanation is not cached with them; strategy-classified result remains per-strategy.
   - failure tests requested from A: cache hit does not consume budget; strategy-specific projection is isolated; prompt/schema/model/price bucket/volume bucket/manifest changes invalidate cache.

known_failures_and_risks:

- G1 is not published; C must not start C2 until A records this report and D1 report and publishes `CONTRACT_BASE`.
- Current code still allows long primary review in at least `pipeline_stages.py`; C2 must remove C-owned long budget/reviewer acceptance, but public caller changes require A owner coordination.
- Current runtime config still allocates long DeepSeek bucket and long stage targets/limits; A owns config changes.
- Current `deepseek_review_v3` is dimensions-oriented; planned `deepseek_v4_review_facts_v1` needs schema migration and compatibility mapping.
- Current news evidence routing limits and TTL exist, but source-tier positive-fact confirmation and event-level dedupe are incomplete.
- Full repo quality gates are known not globally green on A1 base per `docs/reports/youhua-a1-baseline.md`; C1 single-domain tests pass.

ready_for_gate: `yes; C1 report is available; waiting for A CONTRACT_BASE/G1`
