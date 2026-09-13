# V3 三头训练与运行整链

## User Request

用户要求不再逐章停顿：先一次性完成 `docs/train.md` 全部实现，再统一编写测试，最后执行统一测试、真实
`train-v3` 训练和显式 V3 整链验收。历史输入继续使用现有 BaoStock 日线活动归档，不下载分钟数据、不修改
历史 SQLite schema，也不在 `data/historyless/` 生成另一份训练数据库。

## Current Evidence and Cause

- 本批基线与上游均为 `46c38b6f885f991a2652873078b9080605052257`，分支为 `feature/tomorrow-v2`。
- 开始时工作树只有用户未跟踪的 `data/historyless/` 与 `docs/train.md`；`docs/train.md` 作为用户计划源保留且不
  纳入提交。SQLite repack 完成 `finalize` 后，按用户要求删除只剩状态与验收摘要的 `data/historyless/`。
- 前一批只建立了按策略键控的 profile 类型合同；训练样本、bundle codec、V3 loader、生产模型服务、状态投影和
  启动入口仍由 Tomorrow 单头实现拥有，无法生成或消费 Today/D25 独立模型。
- 旧训练结果还携带源码提交并使用 Tomorrow 专属 schema；这会让可移植工件和本机 Git 状态耦合，也无法通过
  三头固定目录表达各自的标签成熟度和模型身份。
- `Regression-Key: v3-three-head-training-runtime`。

## Added

- 增加 Today、Tomorrow、D25 三份不可变训练合同；Today/D25 使用 3/5 日收益与三项残差动量，Tomorrow 保持
  原六特征、拟合参数和数值训练数学。
- 增加一次扫描的 T+1 至 T+5 多目标样本管线和单一临时 SQLite；D25 只在四个未来 horizon 齐全时形成聚合目标。
- 增加 Today/D25 独立 predictor、统一 `train-v3` CLI/启动脚本入口，以及三策略共享特征计算但隔离预测结果的
  有界缓存。
- 增加按 head 的到期决策、固定四文件安全发布、发布中断恢复和 Today/D25 验证标签指标。

## Changed

- V3 loader 现在要求 `data/train/{today-v3,tomorrow-v3,d25-v3}/` 三组工件全部存在且分别通过 codec；缺少或
  错配任一组时整个显式 V3 启动失败关闭。V1/V2 仍只声明 Tomorrow 模型能力。
- Tomorrow 专用生产模型服务改为按策略实例化的服务；三个 head 分别拥有锁、deadline、预测缓存和统计，模型
  诊断与成本门覆盖 Today/Tomorrow/D25，但不覆盖统一 0–100 `base_score`。
- 状态由单一 `tomorrow_model` 切换为 `scoring_profile.profile_id` 和按策略键控的 `heads` 白名单；GET、SSE、
  冻结记录和 Web 继续消费通用决策模型身份与诊断字段。
- `active-bundle.json` 精确保留 `model_hash`、`report_hash`、`training_input_hash`、`content_hash`；其余三件工件
  不保存源码提交、本机路径或 generation 目录，整组可复制到依赖兼容的另一台 PC。
- Linux 公开训练入口继续使用 1792 MiB 回压、2048 MiB 硬限制和低 CPU/IO 权重；PowerShell 同步公开零参数
  `train-v3` 命令。

## Fixed

- 修正训练标签合同与实际实现不一致的问题：模型目标始终是扣成本前超额，完整往返成本只由在线执行门扣一次。
- 15:00 后 `close_fallback` 现在从候选资格开始就绕开模型历史与特征门，只走本地规则，避免模型缺数污染允许的
  盘后恢复。
- Today/D25 在线输入不再错误要求未进入各自 manifest 的 1 日收益。
- 新训练输入 codec 绑定并校验 split、日历、股票集合和内容 hash，避免训练成功后因自身字段合同缺失而无法加载。

## Removed

- 删除活动树中的 Tomorrow 专用生产模型服务和 V3 单头 bundle 类型，不保留双实现、旧状态 schema 或 loader
  fallback。
- 删除训练工件与 `source_commit` 的耦合；代码、特征、标签、参数和资源限制只通过明确训练合同 hash 表达。
- 不创建或读取 generation/hash 目录，不新增历史数据库或 `historyless` 转换步骤。
- SQLite repack 已安全 `finalize`，旧归档备份释放 24,961,556,480 字节且不可恢复；确认活动归档和训练工件后，
  删除 `data/historyless/` 中剩余的 80 KiB 状态及验收摘要。

## Verification

- `make format-check`、`make lint`、`make type-check` 和完整 `make test` 通过；`make package` 在允许访问本机依赖
  代理的环境中成功生成 sdist 和 wheel，仓库外 wheel 安装 smoke 通过。
- 真实 2 GiB scope 三头训练总耗时 1:56:33，一次历史扫描读取 8,058,342 行并形成 7,257,635 个有效样本；
  Today、Tomorrow、D25 均顺序完成 42 个行业模型。进程峰值 RSS 为 863,518,720 字节，临时样本 SQLite 峰值
  为 1,812,779,008 字节，均低于 2,147,483,648 字节上限。
- 三头训练输入均绑定活动 snapshot `4c23f681da75cf6b484787e4f508534801594de7afbac564eefcae42b3b59d1b`；
  重复 `./run.sh train-v3` 在约 2.4 秒内返回整体及三头 `already_current`，并公开各自非空 model/report hash。
- 实际 `./run.sh --profile v3` 启动成功；`/api/status` 与 Today、Tomorrow、D25 三个 current API 均返回 200，
  状态中的三个活动模型 hash 分别匹配各自固定目录。验收日为周日，三策略 current 为预期的 `not_ready`，不是
  模型加载失败。
- `scripts/repack_baostock_history_archive.py finalize` 验证活动 snapshot、Tomorrow bundle 和内存证据后成功；
  活动归档大小为 12,886,999,040 字节，旧备份已删除。重复 finalize 幂等返回 `finalized`。

## Residual Risks

- 日线训练有意不复现历史 11:20/14:50 时点；所有三头继续固定
  `point_in_time_parity=false`、`historical_data_insufficient`、`production_authority=false` 和
  `automatic_model_update=false`。
- 验收发生在非交易日，只证明真实服务可加载三头并通过状态/API 边界；交易时段的实际候选数量和收益效果仍需
  后续运行观察。默认 `scoring_profile=v1` 保持不变，不会因本次训练自动切换。
