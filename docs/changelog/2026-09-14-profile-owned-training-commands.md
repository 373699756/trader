# Profile-owned V2/V3 training commands

Regression-Key: `profile-owned-v2-v3-training-commands`

## User request

用户指出 `./run.sh train-v2` 尚不存在，并明确要求 V2/V3 不仅数据目录分开，代码也必须按档位区分；能够共用的
历史扫描、样本构造、拟合与发布机制继续共用，差异部分由独立 V2/V3 命令和代码合同处理。

## Root cause and decision

原训练实现、样本库和拟合器全部位于 `profiles/v3`，同时三头合同与输出目录硬编码为 `*-v3`。这使新增 V2 命令
只能复制整套训练器或继续借用 V3 身份，两者都会混淆档位所有权。目标结构因此选择“公共机制下沉、档位合同上移”：
公共引擎不知道 V2/V3，档位模块分别声明窗口、特征、模型 ID、codec 身份、输出根和命令 adapter。

## Added

- 新增零参数 `./run.sh train-v2`，一次扫描当前 active BaoStock snapshot，顺序训练 V2 Today、Tomorrow、D25。
- 新增 V2 独立 251 日训练合同，纳入 120/250 日 skip-5 残差动量和同源市场状态特征。
- 历史读取面支持 61 至 251 日显式有界窗口；临时样本 SQLite 按档位特征宽度建表。
- 增加 V2/V3 合同隔离、命令路由、独立输出路径及 251 日真实分片读取回归测试。

## Changed

- 训练公共机制迁入 `infra/scoring/training`；`profiles/v2` 与 `profiles/v3` 分别拥有自己的 contracts 和 training
  adapter，且互不导入。
- `train-v2` 写 `data/train/v2/{today,tomorrow,d25}`；`train-v3` 写
  `data/train/v3/{today,tomorrow,d25}`；`train-tomorrow` 明确写 V3 的 `tomorrow`。
- bundle codec、原子发布和 due 判断显式接收档位合同，禁止用目录名猜测模型身份。
- 251 日单股窗口改为只保留日期、复权收盘和复权成交额的紧凑历史点，代码、行业、板块等当前元数据只保留一份。
- revision 影响范围由档位窗口派生，V2 失效后续 250 个交易日，V3 保持 60 个。
- 同步更新 README、评分逻辑、工程设计、工程实施、策略回溯和 V1/V2 合并计划。

## Fixed

- 修复公开脚本没有 `train-v2`、V2 无法执行独立训练的问题。
- 修复共享实现被错误命名并放置在 V3 包内、导致 V2/V3 代码职责混杂的问题。

## Removed

- 移除公共训练实现对 V3 特征、V3 输出目录和固定六列样本的硬编码。
- 未删除旧扁平活动模型；它们仍是本批运行 loader 的只读稳定 release。

## Verification

- 定向 pytest、Ruff、mypy：通过。
- 真实 `./run.sh train-v2`：通过；扫描 8,058,342 行，产生 6,282,177 个有效样本，三头各得到 34 个有效行业模型，训练输入 hash 为 `4c23f681da75cf6b484787e4f508534801594de7afbac564eefcae42b3b59d1b`。
- 真实训练软件 RSS 峰值 507,764,736 字节，低于 2048 MiB 门；临时样本 SQLite 峰值 1,815,040,000 字节。
- 同输入立即重跑 `./run.sh train-v2`：三头均返回 `already_current`，hash 不变。
- 完整高风险门禁：`make format-check`、`make lint`、`make type-check`、`make test`、
  `make package` 全部通过；严格重构与全仓命名基线为零诊断，mypy 检查 404 个源文件无问题。
- `git diff --check`：通过。

## Residual Risks

- 本批不实施运行 loader cutover；新 V2 工件即使训练成功，也必须先通过完整性和成本后收益/风险门，才可替换默认
  运行时当前读取的旧扁平 release。
- 当前历史是 15:00 收盘代理，不等同于真实历史 11:20/14:50 点时输入；工件继续固定
  `point_in_time_parity=false` 和 `production_authority=false`。
- 当前主机没有可用的 systemd user scope，因此未得到 cgroup 硬限证据；训练内置的跨平台 RSS 采样门已实证通过。
