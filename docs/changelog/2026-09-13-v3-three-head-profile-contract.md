# V3 三头评分档位契约

## User Request

用户把已确认的纯日线三头训练与运行计划写入 `docs/train.md`，要求继续实施。按计划章节与仓库“继续”边界，
本批完成第 1 节：四份核心文档、全局评分档位配置、多头类型契约及直接回归；不提前实现共享样本管线。

## Current Evidence and Cause

- 初始基线提交和上游均为 `65d1ae04f307f8d6bea417331bb1106b445374d1`；实施期间用户先独立提交并推送
  `ad1c0ca49657451cda4745f30d741f857d67a4d2`，本批最终 Review 改以该提交为基线。
- 开始时只有用户的 `data/historyless/` 与 `docs/train.md` 未跟踪，没有暂存内容；本批保持二者原样且不纳入提交。
- 根因已确认：配置字段 `tomorrow_scoring_profile` 把用户选择的整个评分档位错误限定为 Tomorrow；
  `LoadedScoringProfile.identity` 同时保存单一 model ID/hash，`heads` 又只是 tuple，无法无歧义表达 V3 的
  Today/Tomorrow/D25 独立身份、证据和组合器。
- 当前 V3 loader 与生产服务仍只具备 Tomorrow head；三头工件、训练入口、运行评分和外部状态属于后续章节，
  本批未把目标文档描述成已经完成的运行能力。
- `Regression-Key: v3-three-head-profile-contract`。

## Added

- `LoadedScoringProfile` 增加按 `Strategy` 键控、只读的 head 集合；每个 `HeadRuntime` 明确拥有 predictor、
  combiner 和独立 `ProfileEvidence`。
- 类型合同允许 Today 的 `runtime_anchor=11:20`；Tomorrow/D25 继续使用 14:50。
- 增加配置回归，证明旧 Tomorrow 专用字段不被兼容或静默接受。

## Changed

- `config/strategy.json`、settings、bootstrap、server、CLI、performance 与基线身份审计统一使用
  `scoring_profile`；`--profile` 仍只覆盖当前进程且不写回配置。
- V1/V2/V3 当前 profile builder 全部使用新的策略键控 head 合同；现有 Tomorrow 推理数学、模型 hash、
  成本门、风险、融合、动作、排名和冻结语义均未改变。
- `docs/01_评分逻辑.md`、`docs/02_工程设计.md`、`docs/03_工程实施.md`、`docs/04_策略回溯.md` 明确三头的
  特征、标签、锚点、固定目录、失败关闭和分章节交付边界；`docs/v1v2.md` 同步采用新的配置字段名。

## Fixed

- 消除了“全 profile 只有一个 model ID/hash”的错误表示，为后续三个独立模型身份留下唯一类型所有权。
- 消除了全局档位配置与 Tomorrow 单策略命名不一致的问题。

## Removed

- 删除 `ProfileIdentity` 的单模型双重表示；档位只保留 `profile_id`，模型 ID/hash 由对应 head predictor 拥有。
- 删除活动配置与调用链中的 `tomorrow_scoring_profile`；不保留双字段、别名参数或隐藏 fallback。

## Verification

- 配置、profile factory、V3 codec/profile、Tomorrow 模型评分、router、bootstrap、CLI 入口和基线身份的统一
  定向 pytest 通过。
- 架构、功能包、特征合同、版本命名、四份核心文档一致性和固定训练工件的统一契约 pytest 通过。
- 受影响 Python 文件 Ruff format/check 通过；`mypy src/trader` 覆盖 401 个源文件并通过；`git diff --check`
  通过。
- 绝对配置路径执行 `trader-cli validate-config` 与显式 `--profile v2` 均成功，分别投影
  `scoring_profile=v1` 和 `scoring_profile=v2`，且覆盖不写回配置。
- 本批是九章高风险大任务的第 1 章；完整 `make format-check`、`make lint`、`make type-check`、`make test`、
  `make package` 及真实训练/运行专项门禁待第 9 章统一执行，未在本批记为通过。

## Residual Risks

- 共享 T+1 至 T+5 样本、Today/D25 工件、`train-v3`、三头 router/service、公开状态和冻结身份尚未实施；
  现有显式 V3 仍只加载 Tomorrow，不能宣称完整三头已启用。
- 当前日线方案有意不补历史 11:20/14:50 分钟锚点；未来三个 head 即使工程可用，也必须保持
  `point_in_time_parity=false`、`historical_data_insufficient`、`production_authority=false` 和
  `automatic_model_update=false`，直到独立点时证据与授权闭合。
- 本批未运行真实训练、真实服务或浏览器；这些行为尚未改变，相关证据留到对应实施与最终验收章节。
