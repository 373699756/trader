# Detail the Refactor Blueprint Implementation Plan

## User Request

根据当前代码情况，在 `docs/项目重构详细.md` 第 10 章补全按蓝图实施重构的详细计划。未来实施应从已确认基线创建 `branch/Afuture`，但本批只修订计划，不实际创建或切换分支。

Regression-Key: `refactor-blueprint-single-architecture-truth`

## Cause

原第 10 章只有五条摘要，没有记录当前技术分层目录、Today/V1、`web/api/`、复数 `profiles/`、四个根级 bootstrap 辅助模块、当前架构契约测试和数据布局等真实迁移阻断点，也没有定义阶段边界、提交纪律、数据切换、最终门禁与整套 release 回退方式。实施者仍需自行决定迁移顺序，无法直接执行。

## Added

- 增加当前分支、基线 hash、工作树、目标分支不存在状态和现有代码所有者清单。
- 增加当前所有者到 `download/`、`training/`、`recommendation/`、`http_api/`、`web/` 目标所有者的迁移表。
- 增加阶段 0–12 的顺序计划，覆盖公共基础、下载、训练、研究/评价、推荐领域、14 层链路、推荐适配器、HTTP/Web、组合根、数据布局、旧树删除和最终验收。
- 增加数据一次性迁移、失败关闭、完整旧 release 回退和禁止自动合并的边界。
- 增加文档契约测试，固定阶段顺序、当前路径映射、关键门禁和回退要求。

## Changed

- `branch/Afuture` 改为未来阶段 0 从“包含本计划且已推送”的确认提交创建；盘点前的 hash 只作证据，本批明确不创建、不切换该分支。
- 每个未来阶段固定为一次完整交付批次：契约先行、唯一所有者迁移、旧路径同批删除、定向验证、Review、单提交、推送和上游核对。
- 两份权威文档只在对应行为实际切换的阶段同步更新，不提前把尚未实现的目标写成当前活动事实。
- 最终全量门禁固定覆盖完整合并 diff，不能用各阶段的定向结果替代。

## Fixed

- 消除“先建全部新目录再慢慢迁移”可能造成的双实现、转发层和隐藏 fallback 空间。
- 补齐 Today/V1 退出、单数 profile、泛化持久化命名退出、历史数据路径迁移、Web 主页不变和监控模态框只读等容易遗漏的实施步骤。
- 明确当前基线已有架构命名与 Changelog 标题大小写红灯必须在阶段 0 独立闭合，不能归因给后续目录迁移。

## Removed

- 移除不能直接执行和验收的五条概括性切换步骤。
- 不修改活动源码、配置、数据、API、Web 或运行行为，不创建 `branch/Afuture`。

## Verification

- 新增 `tests/contract/test_refactor_blueprint_implementation_plan.py`，先证明旧五步摘要缺少阶段与发布/回退门禁，再在文档补全后通过 2 个测试。
- 17 个定向契约测试通过：新实施计划、根 Changelog 大小与链接、权威文档一致性、当前产品合同和版本命名边界。
- Ruff `check --no-cache` 与 `format --check --no-cache` 通过；蓝图仍保持 20 个成对 Markdown 围栏、14 层链路、13 个有序实施阶段和 121 个对齐到第 61 列的 dataclass 字段注释；`git diff --check` 通过。
- 完整 Changelog 契约仍只有既有 `2026-09-15-data-readiness-filter-separation.md` 的 `## Residual risks` 大小写失败；架构命名定向测试仍只有既有 `docs/项目重构大纲.md` 的旧英文命名失败。本批新增/修改文件均未命中该旧英文命名，两个基线问题明确列入未来阶段 0。
- 本批只改变计划和其契约测试；运行时、供应商、DeepSeek、浏览器和 wheel 安装实证不适用。

## Residual Risks

- 详细计划尚未实施，活动系统仍以 `docs/01_评分逻辑.md`、`docs/02_工程设计.md`、`pyproject.toml` 和当前源码为准。
- `branch/Afuture` 尚不存在；真正开始时必须重新确认包含本计划的获批提交，如果源分支前移则重新盘点并更新计划，不能直接使用盘点前的旧 hash。
- 当前基线的两个已知契约红灯仍需按阶段 0 独立修复，本批不越界改写对应历史文件。
