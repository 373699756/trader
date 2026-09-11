# Delivery Record: BaoStock QFQ Gap Repair

## User Request

定位转换后归档的 209 个可补充字段缺口，从网上取得或重建缺失记录并写入数据库，使当前训练读取面能够消费完整
日线输入；同时修改 `scripts/convert_baostock_history.py`，让已发布归档也能安全补缺。

## Cause

- 209 个缺口全部是 `daily_qfq`，覆盖 56 只代码，不是 raw 行、交易日或月分片缺失。
- 其中 131 条属于 `001872` 在 2018-12-26 更名前的历史区间。BaoStock 以旧代码 `000022` 返回这些日期，原请求
  使用当前代码，因而没有取得复权结果。
- 其余 78 条主要位于新股上市最初 1 至 4 个交易日；BaoStock 对前复权请求返回 raw adjustment 标志，现有 gap
  supplier 按合同记录为 `supplier_adjustment_unavailable`。
- 转换器此前在发现目标已经完成后直接返回，只在首次旁路转换期间支持补缺；初始 snapshot 还固定使用序号 1，
  因而不能暴露父归档之后同步序号为 2 的有效增量 revision。
- 首次真实补缺在发布前失败：供应商返回的停牌 qfq 行被组装器错误标记为 `complete`，与 raw/qfq 的
  `suspended` 状态冲突，整批被月归档 schema 拒绝；目标 snapshot 因原子发布边界保持未变。

## Added

- 新增已发布归档 qfq-only 修复：先用当前 BaoStock gap supplier 对全部 `(代码, 日期, 字段)` 身份逐项联网核验，
  直接采用可取得的 qfq；只有供应商明确返回 `supplier_adjustment_unavailable` 时才进入公式重建。
- 新增 `001872 <- 000022`、生效日 2018-12-26 的 qfq 历史代码窗口。
- 新增 BaoStock 官方涨跌幅复权算法实现：以随后首个同源有效 qfq 为锚点，用当日 raw `preclose` 与前日 raw
  `close` 按六位小数反向递推，只构造 qfq OHLC，量额保持同源 raw 语义。
- 新增已发布月份的旁路复制、revision 写入、分片封存、rollback 恢复和新 active snapshot 原子发布；故障注入
  回归覆盖月文件替换后、控制指针发布前的恢复。

## Changed

- 首次离线转换的 active snapshot 序号从固定 1 改为 2；首次转换期间确有在线补缺时使用序号 3，使 snapshot
  序号与父归档、增量及补缺 observation 序号一致。
- 修复发布采用新的 BaoStock 来源身份以及与其绑定的日历和总体身份；旧 revision、旧 snapshot 和源归档保持
  不变，未变化月份继续复用原引用。

## Fixed

- 修复转换器无法对已经完成且已发布的归档再次执行 209 条 qfq 补缺的问题。
- 修复增量 revision 已存在于月库但因 active snapshot 序号过低而对训练读取不可见的问题。
- 修复 `001872` 更名前历史 qfq 请求没有路由到供应商真实旧代码的问题。
- 修复停牌 qfq 修订必须保留 `supplier_marked_suspended` cell 状态的问题，避免合法停牌行阻断整批发布。

## Removed

- 无。没有删除源归档、历史 revision、旧 snapshot、训练工件或运行数据。

## Verification

- 真实归档联网核验完成 56/56 只股票；发布结果为 `state=supplemented`、`supplemented_rows=209`、
  `remaining_downloadable_gaps=0`，active snapshot sequence 从 1 更新为 3，snapshot hash 为
  `b6c90cb4e458a9b46a2aa29b2e717f75d1545656e00baf4d05ed324596651a67`。
- 发布后重复执行同一补缺入口返回 `state=already_current`、`remaining_downloadable_gaps=0`、相同 snapshot
  hash，且没有再次登录 BaoStock；26 个受影响月份的 pending、seal 和 rollback sidecar 均已清理。
- 定向单元与契约测试覆盖：初始/增量序号可见性、已发布归档补缺、官方因子递推、历史代码映射、停牌状态、
  幂等重跑、分片替换后发布失败恢复，以及权威文档合同；34 项通过，停牌修复用例随后单独复跑 1 项通过。
- 按用户明确要求，本批不运行 `make` 全量门禁；仅运行补数边界直接相关的 pytest、Ruff、mypy、文档契约和
  `git diff --check`：受影响文件 Ruff 通过，转换器与 gateway mypy 通过，`git diff --check` 通过。

## Residual Risks

- 历史行业、11:20/14:50 分钟、资格、硬过滤和风险事实仍缺可信点时来源；本批只修复已证明的日线 qfq 缺口，
  不改变 `point_in_time_parity=false` 或生产权限。
- 本批不切换 Tomorrow 评分生产档位，也不把 qfq 补缺等同于 V3 模型已经训练或获得生产授权。
- 本批只证明 209 个 qfq 缺口完成；历史行业与分钟数据资格仍未闭合，未运行 Tomorrow 训练。
- `Regression-Key: completed-baostock-qfq-gap-repair-and-snapshot-sequence`。
