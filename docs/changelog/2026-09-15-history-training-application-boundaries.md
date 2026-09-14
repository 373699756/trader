# History, training, and recommendation application boundaries

## User Request

完成历史下载/更新、V2/V3 训练和荐股三条链的模块化任务，确保维护、训练与荐股互不触发或污染彼此状态。

Regression-Key: `history-training-recommendation-application-boundaries`

## Cause

历史同步和训练核心虽然已有 infra 实现，但命令入口直接编排基础设施，缺少计划中明确的应用层用例和端口；
下载与增量维护的调用语义、训练运行器和 due 判断无法在不触碰外部 I/O 的情况下独立验证。

## Added

- 新增 `application/history`：下载、增量更新、只读状态用例及 Supplier/Archive/Calendar/Status 端口。
- 新增 `application/training`：按 profile 注入的训练用例、训练 due 用例及 runner/due 端口。
- 新增 `HistoryArchiveGateway`，统一复用原子历史同步 owner，并为无 active snapshot 的 update 返回稳定阻断状态。
- 增加应用边界契约测试和委托行为回归测试。

## Changed

- `download` entrypoint 通过 `DownloadHistoryUseCase` 调用归档端口；首次建立与已有归档增量维护仍由同一同步 owner
  决定，避免重复实现 snapshot 发布和回滚语义。
- `train-v2`、`train-v3` entrypoint 通过各自应用用例调用 profile-owned infra runner；训练不会触发历史下载、荐股刷新、
  自动 promotion、配置修改或热加载。
- 训练引擎改用 profile-owned `.training.lock`，与历史维护 `.maintenance.lock` 分离；训练仍通过 snapshot identity
  校验保证读取一致性。
- 工程设计和策略回溯文档补充 history/training/recommendation 的单向依赖边界。

## Fixed

- 修复历史更新在无 active snapshot 时可能误进入供应商流程的问题，保持旧归档不变。
- 修复 CLI 直接持有历史同步和训练实现导致的应用层耦合；模型、风险、冻结和推荐 Web 行为保持上一批已验证契约。

## Removed

- 未删除历史数据、训练工件或既有 `download` 命令；未新增 `download_history` 或 `train-tomorrow` 兼容入口。

## Verification

- 历史/训练应用边界契约、委托回归和既有 `download` CLI 契约测试通过。
- 受影响新增 Python 文件 Ruff 检查通过，`make type-check` 通过。
- `make lint`、`make type-check`、`make package` 和 `make performance-check` 通过；性能基线 `network_calls=0` 且增量重算等价。
- `git diff --check` 通过。
- `make format-check` 仍受本批之前的 4 个文件格式基线失败影响：`daily_history_cache.py`、
  `test_baostock_history_cli.py`、`test_entry_contract.py`、`test_v3_profile.py`。
- 最终全量 `make test` 完成；仅命中既有架构命名、旧模型 ID 和历史 Changelog 记录契约失败，
  本批新增 history/training 测试与入口回归均通过。
- 未执行真实 BaoStock、训练全量运行、DeepSeek 或浏览器验证；这些外部证据不属于本次应用边界单元测试。

## Residual Risks

- 真实供应商超时、磁盘不足和跨进程锁竞争仍需在可控运行环境执行 history 专项诊断。
- 全量测试中的既有命名/旧模型 ID/历史 Changelog 契约失败仍未处理，本批不扩大范围修改。
