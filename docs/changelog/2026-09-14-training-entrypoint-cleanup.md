# Training Entrypoint Cleanup

## User Request

移除 Tomorrow 专用训练命令，训练统一使用 `train-v2` 和 `train-v3`，保持下载、训练和荐股职责独立。

## Cause

`train-tomorrow` 仍作为公开兼容入口存在，与当前按 profile 管理三头训练工件的结构重复，容易形成第二套训练入口和资源边界。

## Added

- 增加旧 `train-tomorrow` CLI 解析失败的契约覆盖。
- 训练入口文档统一说明 V2/V3 三头训练命令。

## Changed

- CLI、Shell、PowerShell 只公开 `train-v2` 和 `train-v3` 训练命令。
- `research_commands` 不再从公开命令分派 Tomorrow 专用训练流程；现有 V2/V3 训练实现和模型工件不变。
- 文档明确训练不会隐式调用历史下载，也不会修改荐股运行状态。

## Fixed

- 消除 Tomorrow 专用命令与 profile-owned 三头训练架构之间的入口歧义。

## Removed

- 删除公开 `train-tomorrow` parser、Shell/PowerShell 转发分支和活动文档入口。
- 删除 CLI 中仅服务于该公开入口的 Tomorrow 训练编排函数。

## Verification

- CLI、Shell、PowerShell 和活动文档契约定向测试通过。
- 受影响 Python 文件 Ruff、mypy、`git diff --check` 通过。
- 全量门禁待后续大任务收尾统一执行。

## Residual Risks

- `infra/scoring/profiles/v3/training.py` 中的底层 `run_tomorrow_training` 仍被内部单元测试覆盖，尚未在本批删除；它不再是公开命令入口。
- 自动化安装/卸载命令和历史模块拆分属于后续独立批次。

`Regression-Key: training-entrypoint-cleanup`
