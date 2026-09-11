# Delivery Record: Semantic Research Artifact Identities

## User Request

用户发现活动代码中存在 `CodexA/B/C/D` 类型、函数、owner 和工件路径，要求重新优化为可理解、可维护的业务命名。

## Cause

- 根因已确认：早期研究开发曾按 A/B/C/D 代理工作线分工，临时执行者名称被直接写入应用类型、公共函数、JSON 字段和持久化文件名。
- 权威文档已使用 H1 能力、历史确认和终端留出等职责概念，但活动代码未完成同步重命名。

## Added

- 新增活动源码和脚本的命名契约，禁止再引入自动化代理分工名称。
- 新增工件线形状的持久化边界回归，确认 JSON 不再含有无业务意义的 `owner`。

## Changed

- Tomorrow 标签就绪前置改为 `TomorrowLabelReadinessPrerequisite`，H1 收口改为 `H1ResearchCompletion`。
- 历史确认批次、策略终态、持久化 store/index/error 及执行函数统一使用 `HistoricalConfirmation...` 命名。
- 跨策略终端留出脚本改为 `point_in_time_terminal_holdout.py`；H1 和历史确认终态文件分别改为 `h1_research_terminal.json` 和 `historical_confirmation_terminal.json`。
- 研究工件图只使用 `stage`、`artifact_id` 和 `artifact_kind` 表达职责，类型、codec 和报告投影统一删除 `owner`。

## Fixed

- 修复临时执行者名称泄漏到长期业务 API、错误信息、工件身份和测试名的命名污染。

## Removed

- 删除 `TomorrowResearchOwner`、工件 `owner` 字段及 owner-to-artifact 映射。
- 删除所有活动代码、脚本和测试中的旧 A/B/C/D 代理类名与函数别名，不保留双实现。

## Verification

- 127 项定向 unit/contract 回归通过，覆盖 Tomorrow 工件图与 codec、H1 完成链、历史确认链、
  点时终端留出脚本、研究入口、架构及策略文档契约。
- 受影响 Python 文件 Ruff 通过；`make format-check` 通过；`make type-check` 通过（394 个源码文件）；
  `make package` 通过并成功生成 sdist 与 wheel。
- `make test` 完整收集并执行 1845 项：本批相关及其余 1844 项通过，唯一失败为本批基线已存在的
  `test_linux_user_units_pass_systemd_native_verification_when_available`。沙箱外定向复跑仍失败，原因为
  2026-09-11 已提交的 systemd 模板给 `WorkingDirectory=` 写入带引号路径，而本机校验器拒绝该格式；
  测试与模板均不在本批 diff，未混入本次命名修复。
- `make lint` 的常规 Ruff 检查通过，严格重构质量计数因本批基线已存在且未修改的
  `src/trader/infra/scoring/profiles/v3/training.py` 两项失败：`C901=1`、`PLR0913=1`。
- 只读 `research` 诊断按预期以历史数据和人工生产授权前置不足失败关闭，未出现重命名后的工件冲突，
  且 `production_authority=false`。

## Residual Risks

- 旧研究目录中若存在带代号字段或文件名的旧工件，新 codec 将失败关闭；本批不自动迁移、不删除运行数据，需从已验证父证据显式重新封存。
- `docs/reports/` 中不可变的既往交付报告仍保留当时的执行者字段；它们不属于活动代码、运行工件或新命名契约，未改写历史审计事实。
- 本批不改变评分、候选、冻结、生产授权或模型更新语义。
- `Regression-Key: semantic-research-artifact-identities`。
