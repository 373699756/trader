# V2 P1 来源能力探测基线（2026-07-30）

## 1. 基线元信息

- 仓库：`/home/c/linux/trader`
- Git HEAD：`e81e10d`
- 分支：`feature/tomorrow-v2`
- `@{upstream}`：`origin/feature/tomorrow-v2`
- 上下文：本批仅完成 `docs/V2_plan.md` 的 P1 可验证能力与准入边界固化，不改造运行代码。

## 2. 本批能力探测范围

- 只做源码、配置、运行契约的边界审查；当前未接入外部真实网络进行抓取探测。
- 以 `docs/V2_plan.md` 的 P1 为准，限定为“接入前能力清单 + 未验证来源准入结论”。
- 目标来源：交易所官方、巨潮资讯 CNInfo、东方财富、新浪、腾讯、通达信/mootdx、BaoStock、
  AKShare、Tushare。

## 3. SourceCapability 清单（截至 2026-07-30）

| Source | Evidence | 当前可调用范围 | 当前准入结论 | 门禁判定 |
| --- | --- | --- | --- | --- |
| 交易所官方（上交所/深交所/北交所） | `src/trader/infra/market_data/` 下无 `exchange.py` 或同名入口；运行时
  `MarketSourceCoordinator`/`MarketDataGateway` 无该 source | 未接入 | 拒绝 | 等待“字段/交易日历/停复牌/状态机”能力探测脚本 |
| 巨潮资讯 CNInfo | `src/trader/infra/market_data/` 下无 CNInfo/巨潮抓取实现；`service_research.py`
  仅沿用 AKShare 现有公告聚合链 | 未接入 | 拒绝 | 等待公告唯一标识/增量游标/重复页行为探测 |
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
  历史特征与研究补强能力（120 分） | 维持现状，非交易所级生产准入 | 允许（既有） |

## 4. 已形成的可验证锚点

- `docs/V2_plan.md` P1 已补充“未接入来源不参与评分/冻结/生产组合根/配置”的硬约束。
- `tests/contract/test_v2_source_capability.py` 增加三类契约：
  1) P1 章节状态与输出要求可追溯；
  2) `SourceCapability` 基线报告完整性；
  3) 未接入来源未出现在运行入口路由。
- 本批不引入新的网络抓取 fixture：运行环境当前按降级契约继续使用历史离线证据。

## 5. 本批准入结论

- 仅保留已存在的五类契约来源（东财/新浪/腾讯/AKShare/Tushare）进入既定运行，且不更改其当前职责。
- 交易所官方、巨潮资讯 CNInfo、通达信/mootdx、BaoStock 维持 **未准入/拒绝**，
  直到完成：

  - `正常 / 空页 / 半页 / 重复页 / 字段缺失 / 时间倒退 / 超时 / 限流` 的外部探测用例；
  - 字段映射、单位、时区、分页、增量游标与错误分类报告；
  - 生产配置切换与降级接入条件。

- 本批未通过 `runtime.json`、配置或 `bootstrap.py` 向生产路径新增任何候选外部源。

## 6. 后续状态附注

- P6 批次已新增 CNInfo 离线增量登记簿模块，用于公告唯一键、游标和风险证据持久化；该模块
  仍未进入行情路由、生产 source contract 或 HTTP 只读请求路径，交易所公告交叉校验继续为
  `pending`。
