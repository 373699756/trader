# V2 P1 来源能力探测基线（2026-07-30）

## 1. 基线元信息

- 仓库：`/home/c/linux/trader`
- BASE_SHA：`f4062de`
- 工程分支：`codex/v2-g1-e1`
- 上下文：按 `docs/implementation-plan.md` V2-E1 复核来源准入，并实现统一只读端口、
  交易日历持久化、epoch 覆盖门禁和最近有效数据保护；本批仍不新增外部来源或组合根接线。

## 2. 本批能力探测范围

- 只做源码、配置、运行契约的边界审查；当前未接入外部真实网络进行抓取探测。
- 以 `docs/implementation-plan.md` 的 V2-E1 为准，限定为“接入前能力清单 + 未验证来源准入
  结论”。
- 目标来源：交易所官方、巨潮资讯 CNInfo、东方财富、新浪、腾讯、通达信/mootdx、BaoStock、
  AKShare、Tushare。

## 3. SourceCapability 清单（截至 2026-07-30）

| Source | Evidence | 当前可调用范围 | 当前准入结论 | 门禁判定 |
| --- | --- | --- | --- | --- |
| 交易所官方（上交所/深交所/北交所） | `src/trader/infra/market_data/` 下无 `exchange.py` 或同名入口；运行时
  `MarketSourceCoordinator`/`MarketDataGateway` 无该 source | 未接入 | 拒绝 | 等待“字段/交易日历/停复牌/状态机”能力探测脚本 |
| 巨潮资讯 CNInfo | `src/trader/infra/market_data/cninfo.py` 与
  `service_research_data_plane.py` 已实现离线增量公告、稳定公告 ID、游标和风险组件恢复 |
  独立风险登记簿写入；不进入行情路由 | 有条件允许（研究风险数据平面） | 交易所交叉校验保持
  `pending`；禁止进入行情 source contract、评分来源替换或 HTTP 热路径 |
| 东方财富（Eastmoney） | `src/trader/infra/market_data/eastmoney.py`，运行路由见 `gateway.py`/`source_coordinator.py`
  | 全市场主线 + 部分板块/参考字段 | 维持现状 | 允许（既有） |
| 新浪（Sina） | `src/trader/infra/market_data/sina.py`，运行路由见 `gateway.py`/`source_coordinator.py` | 全市场对冲源
  | `schema-v17` 下继续作为失败 fallback | 允许（既有） |
| 腾讯（Tencent） | `src/trader/infra/market_data/tencent.py`，定向候选/TopK 来源 | 定向报价 | 允许（既有） |
| 通达信/mootdx | `src/trader/infra/market_data/` 下无 mootdx 适配器 | 未接入 | 拒绝 | 等待节点发现/断线/批量与偏差探测 |
| BaoStock | `src/trader/infra/market_data/` 下无 BaoStock 适配器 | 未接入 | 拒绝 | 等待复权/字段单位/停牌-除权探测 |
| AKShare | `src/trader/infra/market_data/akshare.py`、`akshare_parsing.py`、`akshare_news.py` |
  研究端口与公告/财务聚合 | 维持现状，不纳入交易所级准入 | 允许（既有） |
| Tushare | `src/trader/infra/market_data/tushare.py`、`service_tushare.py` |
  120 积分未复权 `daily` 能力审计，不进入活动历史因子或评分 | 维持现状，非交易所级生产准入 | 允许（既有） |

## 4. 已形成的可验证锚点

- `docs/implementation-plan.md` V2-E1 已补充“未接入来源不参与评分/冻结/生产组合根/配置”
  的硬约束。
- `DataPlaneReadPort` 固定为一次读取不可变一致 epoch 视图；`DailyFeaturePack -> MarketEpoch ->
  CandidateQuoteEpoch` 父子身份和 `ResearchEpoch` 同日配置约束保持内容寻址。
- 低频仓储覆盖证券主数据、交易日历、历史摘要、风险证据和来源游标；同身份旧观察不得覆盖
  更新记录，同观察时间不同内容按冲突拒绝，空事实不得覆盖最近有效内容。
- 发布门禁要求潜在可执行代码证券主数据覆盖 100%，候选核心历史覆盖不低于 99%；不合格
  epoch 保留最后一致视图并返回结构化拒绝原因。
- `tests/contract/test_v2_source_capability.py` 增加三类契约：
  1) P1 章节状态与输出要求可追溯；
  2) `SourceCapability` 基线报告完整性；
  3) 未接入来源未出现在运行入口路由。
- 本批不引入新的网络抓取 fixture：外部来源真实性和可用性仍按降级契约视为待真实环境复核。

## 5. 本批准入结论

- 仅保留已存在的五类契约来源（东财/新浪/腾讯/AKShare/Tushare）进入既定运行，且不更改其当前职责。
- 交易所官方、通达信/mootdx、BaoStock 维持 **未准入/拒绝**；CNInfo 只允许独立风险登记簿
  旁路写入，不获得行情或交易所级生产来源身份。上述来源进入相应生产职责前均须完成：

  - `正常 / 空页 / 半页 / 重复页 / 字段缺失 / 时间倒退 / 超时 / 限流` 的外部探测用例；
  - 字段映射、单位、时区、分页、增量游标与错误分类报告；
  - 生产配置切换与降级接入条件。

- 本批未通过 `runtime.json`、配置或 `bootstrap.py` 向生产路径新增任何候选外部源。

## 6. 剩余风险

- 本批未执行真实外部网络探测，供应商字段、限流、空页和时间倒退行为仍需独立证据。
- CNInfo 交易所公告交叉校验继续为 `pending`；不得把旁路登记簿描述为交易所级复核。
- 组合根和生产配置属于后续 Gate 独占范围；E1 端口与仓储尚未接管运行入口。
