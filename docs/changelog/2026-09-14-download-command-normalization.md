# Download Command Normalization

## User Request

将历史维护入口从 `./run.sh download_history` 统一改为 `./run.sh download`。

## Cause

公开 Shell、PowerShell、CLI、测试和活动文档仍使用历史命令名，导致命令契约不一致。旧命令不再承担兼容职责。

## Added

- 增加统一的 `download` CLI、Shell 和 PowerShell 入口名称。
- 增加旧 `download_history` 命令必须解析失败的回归契约。

## Changed

- 历史维护行为保持不变，仍使用现有 BaoStock 增量、缺口、最近五日复读和原子 snapshot 发布链。
- 活动 README、工程设计、策略回溯、V1/V2 文档和入口契约统一改用 `download`。

## Fixed

- 修复公开命令名与历史维护实现名称不一致的问题。

## Removed

- 删除公开 `download_history` 命令，不保留隐藏兼容别名。

## Verification

- CLI、Shell 参数拒绝、旧命令拒绝、活动文档契约定向测试通过。
- 受影响 Python 文件 Ruff、mypy、`git diff --check` 待本批收尾统一执行。

## Residual Risks

- 本批未修改 `train-tomorrow`、自动化安装/卸载命令及历史归档内部实现；这些属于后续独立批次。
- 已安装的外部旧任务若仍调用 `download_history`，需要后续单独迁移或人工更新。

`Regression-Key: standalone-download-command-normalization`
