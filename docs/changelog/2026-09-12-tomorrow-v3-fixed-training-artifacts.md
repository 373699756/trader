# Tomorrow V3 固定训练工件文件

## User Request

用户要求恢复 `data/train/tomorrow-v3/` 下固定的 `report.json`、`training-input.json`、`active-bundle.json`、
`model.json`，删除 hash 命名的训练目录，并让 `./run.sh --profile v3` 只从该固定目录读取。

## Current Evidence and Cause

- 用户看到根目录 `model.json` 时间未更新，而成功训练结果出现在
  `generations/07088e9e0f89f451fa10bbbcb7c9b2aaebb59e28e71812aa5d7081090a20343b/`。
- 根因已确认：此前发布器把三件工件原子移动到内容寻址目录，loader 再通过 `active-bundle.json` 的
  `generation` 字段定位模型；这使稳定路径上的旧文件与真实活动模型分离。
- 训练内部又发布 `completed` 阶段，命令层随后发布成功结果，因而终端连续打印两条完全相同的完成行。
- 旧训练合同 hash 包含整个源码 commit，且没有表达当前 2 线程/2048 MiB 资源合同，会让普通文档提交与真正训练
  实现变化无法正确区分。
- `Regression-Key: tomorrow-v3-fixed-artifact-root`。

## Added

- 新增四文件固定发布 repository：先校验隐藏 staging，再备份旧四文件并写入持久化发布日志，依次替换三件工件，
  最后替换 `active-bundle.json` 提交标记。
- 新增进程中断恢复：新组已经完整提交时清理隐藏回退证据，否则恢复上一完整四文件组；发布日志存在时 V3 loader
  失败关闭，不读取半组文件。
- `active-bundle.json` 收缩为 `model_hash`、`report_hash`、`training_input_document_hash`、`content_hash` 四个必要
  hash 字段，不保存 `schema_version`、业务身份、路径或机器信息；其余身份从三件已通过 codec 的工件读取。
- 训练结果 JSON 增加固定 `artifact_root`，直接说明四件工件的落盘目录。

## Changed

- V3 loader 只读取 `data/train/tomorrow-v3/` 下四个固定文件，不解析目录、不回退旧 generation，也不回退 V1/V2。
- 训练合同 hash 改为表达特征、输入、切分、标签、模型结构及 2 线程/2048 MiB 执行政策；`source_commit` 继续写入
  三件工件供审计，但不再因普通 commit 变化触发重训。
- 训练节奏新增 `training_contract_due`，使旧 3 线程/4096 MiB 合同或以后真正的训练实现变化明确进入待重建状态。
- 已将现有校验通过的模型 `4e47e0519fd08d63c6123d48c1402b69a56c13ad7c1c901e13d97cba13c573ff`
  原样迁移到固定四文件布局；模型内容、标签截止日和输入身份未改变。

## Fixed

- 修复成功训练后根目录模型仍旧、用户无法从稳定路径判断真实活动模型的问题。
- 修复 `./run.sh --profile v3` 的训练路径与运行读取路径不一致的问题。
- 修复成功训练终端重复输出两条 `Tomorrow训练 | 1/1 | 完成` 的问题。
- 修复发布替换中断可能留下新旧文件混合且缺少恢复依据的问题。

## Removed

- 删除 `bundle_store.py` 及其含义不清的命名，改用职责明确的 `training_bundle_repository.py`，不保留兼容别名。
- 删除 `generations/<hash>/` 发布和读取链；运行目录稳态不再保留 hash 目录。
- 删除 Git 对任意 `data/train/**/model.json`、`report.json` 的宽泛反向放行，改为只允许
  `data/train/tomorrow-v3/` 下四个固定生产文件进入提交。
- 删除训练内部多余的最终完成阶段，最终结果只由命令边界输出一次。

## Verification

- 定向回归覆盖固定四文件发布/重复发布、staging 拒绝、替换失败回退、持久化日志恢复、半组失败关闭、指针篡改、
  V3 profile 加载、训练节奏、资源合同、单次完成投影、命名、打包布局和文档契约，全部通过。
- `make format-check`、`make lint`、`make type-check`、`make package` 通过；Ruff 覆盖 691 个文件，
  mypy 覆盖 398 个源文件，重构债务检查为零。
- 真实迁移后固定目录只含四个文件；新 loader 读取 `profile_id=v3` 和上述模型 hash，记录合同与当前资源合同对比为
  `training_contract_due=true`，没有启动约 7 小时的重复训练。
- 使用隔离运行目录执行 `TRADER_PORT=5051 ./run.sh --profile v3` 启动新进程；`/api/status` 返回
  `profile_id=v3`、`active=true`、正确模型 hash。统一 `runtime` 诊断 3 次采样全部可达、无 error，整体为受控
  `degraded`，随后通过一次 Ctrl+C 正常停止。
- 荐股事故六检查点：宿主回环可达；周六非交易日没有计划行情刷新；交易日历明确投影 2026-09-12 非交易日；
  Today/Tomorrow/D25 均为无输入漏斗的 `not_ready`；冻结/收盘恢复矩阵在非交易日不适用；新隔离进程加载当前源码和
  固定目录 V3 模型。不能把周末零候选写成盘中推荐通过。
- 完整 `make test` 首次运行只暴露一项文档固定短语契约失败；修复后对应 18 项文档契约和上述定向
  回归通过。用户明确要求不再跑完整测试，因此修复后的第二次全量执行被停止，不记为全量通过。

## Residual Risks

- 当前模型是已完成的旧执行合同训练结果，虽可显式 V3 加载，但 2 线程/2048 MiB 的约 7 小时完整训练仍待 SQLite/
  训练优化实施后统一重建；本批没有再次消耗该时间。
- 2026-09-12 是非交易日，真实行情供应商、候选、逐股 history、完整评分和 Top6 无法形成盘中实证；本批只证明
  固定模型装载、服务启动、API 投影和受控非交易日降级。
- 修复文档契约后未再完整执行全量 pytest；已按用户要求以相关契约和定向回归代替。
- 已存在的 5000 服务不是本批启动的新进程，保持运行且未被停止；其内存中已加载的模型不证明新固定路径 loader。
