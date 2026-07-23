# 软件业务设计文档

版本：v1（由仓库既有需求、架构、运维、问题记录和实施计划归并）

状态：活动产品与工程契约

适用范围：本地 A 股研究看板

本文是产品范围、系统架构、运行时、时间线、数据服务、发布、API、界面、运维、
验收和交付路线的唯一权威。荐股算法、过滤、评分、DeepSeek、融合和选股规则以
[荐股策略文档](recommendation-strategy.md) 为唯一权威；依赖、构建和入口以根目录
`pyproject.toml` 为唯一权威；协作流程以根目录 `AGENTS.md` 为准。

## 1. 产品定位与范围

产品是在个人 PC 上运行的只读 A 股研究看板，面向单一使用者，不是多租户 SaaS，
不连接券商，也不提供真实下单。结果只用于研究，不构成投资建议，不承诺收益。

四类视图为：

- `today`：09:36-11:20 的盘中短线研究信号，面向 T+1。
- `tomorrow`：T 日尾盘形成，面向 T+1。
- `d25`：T 日尾盘形成，面向 T+2 至 T+5。
- `long`：固定长期观察池，只展示当前状态，不冻结、不生成动作、不写推荐历史。

系统允许返回 0 只推荐。产品不包含策略验证工作台、自动调参、机器学习训练、股票价格
预测和模拟交易页面；后台可以对正式冻结推荐进行只读收益与最大不利波动结算，作为发布
质量门禁和复盘数据，但不通过普通 Web 提供交易模拟或自动改变生产策略。

运行范围固定为 Python 3.10-3.14，以及当前稳定版 Chrome、Edge 或 Firefox 桌面版。
验收分辨率为 1280x720、1440x900 和 1920x1080。手机和平板不属于产品范围，不得为
移动端增加业务分支或运行依赖。默认仅监听 `127.0.0.1`，不提供远程身份认证。

## 2. 系统能力与业务流程

系统从公开行情和研究数据构建点时快照，经标准化、硬过滤、候选预选、本地评分、
可选 DeepSeek 结构化复核、风险合并和稳定 TopK 后发布到 Web；冻结策略按时间点保存
不可变历史锚点，当前报价只作为 overlay 展示。

```text
调度与五来源采集
        |
        v
不可变观测 -> 确定性统一行情 -> 过滤与特征
                                  |
                    +-------------+-------------+
                    v             v             v
                 today         tomorrow         d25
                    +-------------+-------------+
                                  v
                     DeepSeek结构化复核（可降级）
                                  v
                        融合、TopK、发布与冻结
                                  v
                        P6内存投影 -> Web/SSE
                                  |
                     检查点/正式冻结/收盘overlay
                                  |
                       后台结果结算（只写审计）
```

任何数据源或 DeepSeek 失败都不得阻塞本地推荐和只读 Web。系统保留最近有效发布，
明确标记 stale、degraded、not_ready 或冻结 fallback，不用空结果覆盖有效结果。

## 3. 架构与代码边界

活动产品代码只能位于 `src/trader`，固定依赖方向为：

```text
entrypoints / web / infra -> application -> domain
```

- `domain`：按 `market`、`recommendation`、`review`、`outcome` 四个业务能力包组织不可变
  值对象和纯函数。`market` 负责点时行情、因子、研究、新闻与尾盘信号；
  `recommendation` 负责过滤、板内评分、策略组合、融合、下行保护和稳定排名；`review`
  负责结构化复核值与本地风险映射；`outcome` 负责冻结推荐结果结算。领域包不得读取配置、
  时钟、网络、文件或数据库，不保留旧根级模块或动态兼容导出。
- `application`：端口按行情、候选特征、报价、研究、参考/历史、快照、事件、复核和结果
  读写能力拆分；流水线只接收不可变的依赖、选项和资源集合。跨线程事件使用有类型审计
  记录、状态枚举与深层不可变 JSON 载荷，状态转换、deadline、latest-wins、冻结 CAS 和
  停止顺序由应用层显式拥有；不得导入 Flask、`infra` 或旧包，也不得让
  `Mapping[str, object]` 或共享可变字典穿越新的应用公共边界。
- `infra`：配置、行情、交易日历、DeepSeek、缓存、SQLite、文件和外部适配器；编排门面只
  持有显式有类型组件，不通过 mixin、多继承、共享状态基类或 `Any` 属性取得能力。
- `web`：请求校验、序列化、SSE 和静态资源；只能调用应用层只读用例。
- `entrypoints`：参数、进程生命周期和退出码。
- `bootstrap.py`：唯一组合根，显式创建客户端并注入依赖；禁止全局服务定位器。

行情适配器固定采用组合：`MarketSourceCoordinator`、`QuoteStore`、
`HistoryStore`/`HistoryWarmup`、
`ResearchLoader`、`IntradayLoader` 和 `ReferenceLoader` 分别拥有自己的有类型状态、锁和
资源依赖；类之间不得通过 mixin、共享状态基类、`Any` 属性或隐式模板方法取得能力。
`bootstrap.py` 显式装配这些组件，最外层 `MarketFeatureService` 只协调与转发行情、
候选、报价、研究、参考、元数据和结果端口，不保存组件业务状态。DeepSeek 固定按 HTTP、
schema、预算批次、预算汇总、缓存、请求执行、状态和复核编排拆分；
`DeepSeekReviewer`、`DeepSeekBudgetStore` 只组合这些组件。快照仓库同样组合独立观测
持久化组件，不通过继承注入事件或健康状态能力。

`create_app()` 必须无线程、无网络、无数据库和无文件写入副作用。HTTP 请求不得抓取
行情、评分、调用 DeepSeek 或写盘。新代码不得导入 `stock_analyzer`。活动源码单文件
原则上不超过 800 行，超出必须按职责拆分并说明；禁止新增含义模糊的聚合模块。

公开入口固定为 `trader-server` 和 `trader-cli`。配置通过 `--config` 或
`TRADER_CONFIG` 传入绝对路径，不得按当前工作目录猜测。HTML、CSS、JavaScript 和
图标随 wheel 作为包资源发布。

## 4. 生命周期、并发与资源所有权

入口依次加载配置、创建适配器、创建运行时、启动流水线并启动 Web。退出时先关闭
事件接收门，排空已接收的冻结和风险事件，再停止来源、标准化、策略、DeepSeek 和
long 执行器，持久化单写线程最后退出；停止完成后不得遗留 worker、future、连接、
single-flight 或回调引用。

默认线程和有界资源为：调度 1、实时数据采集 6、历史下载 5、标准化/过滤 2、策略评分
3、DeepSeek 4、合并 1、持久化 1。实时数据池含五个普通来源 worker、五个普通待处理
槽位，以及腾讯候选和 TopK 专用的一个紧急 worker、一个紧急槽位；历史池拥有独立五个
worker 和候选池容量的有界等待槽，不得占用实时采集位。每个来源最多一个运行任务和一个
latest-wins 待处理请求；同源在途时只保留最新观察点，不补跑旧周期。

事件至少包含事件 ID、主体、交易日、阶段、策略、优先级、数据版本、配置版本、创建
时间、deadline、重试数和不可变载荷引用。幂等键为：

```text
trade_date + phase + strategy + event_type + subject_key + data_version
```

事件在跨线程前转换为深层不可变 JSON 形态。状态固定按
`pending -> running -> success/failed/expired` 进行 compare-and-set；相同幂等键只有
一个有效执行者。冻结、风险变化和 DeepSeek 补审高于普通行情，拥有独立保留容量；
队列满时只能合并普通行情，不能丢弃已持久化的冻结或风险事件。

TopK 展示报价高于普通周期任务；全市场、候选和评分使用同级 FIFO，并按计划器固定顺序
形成“全市场→候选→评分”，避免下一轮全市场越过上一轮未完成的筛选或评分。候选和评分
事件的排队过期时间分别包含上游 20 秒和 23 秒最坏等待，但实际开始执行后仍重新截断为
各自 3 秒和 15 秒 I/O/计算预算，排队预算不得被外部调用消费。

## 5. 交易日与时间线

所有业务时间使用带时区的 `Asia/Shanghai` 时钟，时钟必须可注入。窗口左闭右开；
交易日由 A 股交易日历确定，日历失败时仅可使用带版本、仍有效的本地缓存。

| 时间 | 行为 |
| --- | --- |
| 09:15-09:30 | 共享预热，构建候选、历史因子和证据 |
| 09:30-09:36 | today 观察，不产生可执行动作 |
| 09:36-10:30 | today 主执行 |
| 10:30-11:20 | today 降级执行，提高动作门槛 |
| 11:19:50 | today 冻结检查点 |
| 11:20 | today 正式冻结，之后只更新报价 overlay |
| 11:20-13:00 | 暂停策略计算，以 10 秒全市场行情、1 秒 TopK 报价维持展示 |
| 13:00-14:20 | tomorrow、d25 和 long 预计算 |
| 14:20-14:48 | 新入围、风险和证据变化补审 |
| 14:48 | 停止提交生产 DeepSeek 请求 |
| 14:49:50 | 绕过 fresh 缓存，刷新最终候选并生成 tomorrow/d25 检查点 |
| 14:50 | 正式冻结 tomorrow、d25；long 只发布当前观察 |
| 15:00 | 已有正式记录只保存收盘 overlay；缺失记录按 P6 或收盘本地补算创建一次 `close_fallback` |

错过冻结、DeepSeek 截止或收盘单点时，同一交易日首次后续 tick 可幂等补提交；
14:49:50 最终刷新只能在 14:50 前补交。deadline 后返回的数据只记脱敏审计，不能
更新已有正式记录或冻结 JSON。15:00 后仅当 today/tomorrow/d25 某策略同日正式记录
不存在时进入收盘恢复：本次进程已生成 P6 时保持股票、评分、动作和排名，只替换收盘
锚点；冷启动先读数据库，仍缺失才以一份完整同日收盘行情执行本地筛选、三板评分与 TopK，
不新增 DeepSeek HTTP。行情或三板批次不完整时不写半成品，按 3/5/10/20/30 秒退避重试。

## 6. 数据服务、刷新与降级

### 6.1 来源职责

| 来源 | 职责 |
| --- | --- |
| 东方财富 | 全市场、分钟和历史基础行情，实时字段主优先级 |
| 新浪 | 同周期全市场校验和字段回退 |
| 腾讯 | 候选和 TopK 定向实时报价；默认提供完整前复权日线历史主来源 |
| Tushare | 120 积分档只暴露 SDK `daily` 批量未复权能力和来源健康；更高积分 qfq 能力必须按配置显式启用 |
| AKShare | 行业、新闻、公告和候选级研究数据 |

Tushare SDK 是默认运行依赖，HTTP 协议固定向官方 API 根地址提交 `api_name=daily`，
Token 缺失或供应商明确返回无接口权限时按永久来源降级，不得把权限拒绝当作限流紧密重试；
transport timeout 固定 8 秒，
`runtime.json.market_data.tushare.points` 明确声明积分档。当前固定为 120：只调用基础积分可用的
A 股 `daily`，每批 30 只合并为一次请求；不得调用需要 2000 积分的 `stock_basic`、`trade_cal`、
`adj_factor`/`pro_bar(qfq)`、`daily_basic` 或财务指标接口。项目根目录受保护文件
`.token_key` 以 `DEEPSEEK_API_KEY=...`、`TUSHARE_TOKEN=...` 两个独立赋值保存凭据；
DeepSeek 和 Tushare 各自仍以同名环境变量优先，其次读取对应 `*_FILE`，最后读取
`.token_key`。POSIX 下拒绝 group/other 可读文件，未知键、重复键、空值和超大文件
拒绝启动。密钥、Token、完整请求或响应不得写入配置、日志、SQLite、快照或 API。

### 6.2 刷新频率

| 数据 | 09:15-09:30 | 09:30-10:30 | 10:30-11:20 | 13:00-14:20 | 14:20-14:48 | 14:48-15:00 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 全市场行情 | 10秒 | 5秒 | 5秒 | 5秒 | 3秒 | 14:49:50一次 |
| 120只候选报价 | 2秒 | 1秒 | 2秒 | 2秒 | 1秒 | 1秒 |
| TopK展示报价 | 1秒 | 1秒 | 1秒 | 1秒 | 1秒 | 1秒 |
| 本地评分 | 10秒 | 3秒 | 5秒 | 5秒 | 3秒 | 14:50冻结 |
| 行业热度 | 120秒 | 60秒 | 60秒 | 60秒 | 60秒 | 停止 |
| 市场新闻 | 120秒 | 60秒 | 60秒 | 60秒 | 60秒 | 仅展示 |
| 个股公告/风险 | 300秒 | 180秒 | 180秒 | 180秒 | 120秒 | 仅展示 |
| 财务/历史因子 | 盘前一次 | 缓存 | 缓存 | 缓存 | 缓存 | 收盘后更新 |

频率只能来自 `runtime.json.pipeline.cadence_seconds`。表中数字是最短计划间隔，不是强制
并发数：周期任务在途时跳过本周期，从当前时刻重算下一次，不排队补跑；每个来源 lane
只保留最新待处理观察点。因此实际刷新周期自动取“不小于配置下限且接口能够持续完成”的
最快速度，接口变慢或熔断时不得堆积请求，恢复后自动回到下限。

服务在交易日任意活动阶段首次启动都必须补一次证券主数据和交易日历初始化，不得只在
09:15-09:30 启动时执行。全市场任务只抓取、统一并原子发布实时行情及已缓存历史特征，
不得在 20 秒 deadline 内同步抓取整批历史。缺失历史按主板、创业板、科创板均衡的稳定
顺序交给独立历史池分批预热。120 积分 Tushare `daily` 明确标记为 `raw`/
`unadjusted_daily`，只可用于来源能力审计，不得进入收益、均线、波动、回撤、ATR 或其他
需要复权的历史特征；历史预热直接使用腾讯完整日 K qfq 主来源，东方财富 qfq 为第二
回退。只有 Tushare 明确支持 `pro_bar(qfq)` 时才可进入历史特征缓存。单只缺失不能回滚
同批成功结果，下一周期只续跑未覆盖代码；成交量和成交额分别按供应商原单位显式换算。每批完成
后立即链式提交下一批，失败代码进入负缓存冷却，均不得再次阻塞实时行情任务。
固定 360 个历史预热槽只分配给主板、创业板和科创板，按稳定轮询分别最多保留 120 个；
`unsupported` 证券不得占用历史槽或进入远端历史重试。历史预热必须先保证每个活动板块
具备至少 100 个可用横截面样本，不能把四类证券平均成每类 90 个而使板块可靠度永远低于
可执行门槛。
冷启动允许只读复用 `.runtime/market_data.sqlite3` 中每只不少于 20 条的最近有效前复权
日线作为历史种子；旧种子不写回、不伪装成当日刷新。远端或旧种子返回的合格 qfq 日线
必须写入 v17 独立的 `.runtime/v17/history_cache.sqlite3`，最多保留 360 只并由进程内
单写锁串行提交；后续重启先读该热缓存，避免非策略优化或服务重启把三板历史覆盖重新
归零。热缓存沿用 `daily_history.refresh_ttl_seconds`；过期时优先远端刷新，远端失败才
回退最近有效缓存。热缓存损坏、写入失败或缺失不得阻塞远端回退，也不得写回旧运行库。
若服务在 14:50 后首次启动且 P2 当日报价索引为空，首个后台 tick 必须单次恢复全市场
当日报价索引，供历史表“今日涨跌/锚点至今”和收盘展示读取。15:00 后收盘恢复协调器
先逐策略读取数据库；同日正式记录仍缺失时复用该 P2 收盘批次执行本地筛选、评分与冻结
写入，不调用 DeepSeek。若后续重试时进程内已有完整、同日、三板历史样本达标且未被
可靠度/样本错误标记的收盘全市场缓存，必须复用该缓存继续本地补算，不得反复同步抓取慢
全市场来源；`close_quotes` 有界执行预算必须覆盖一次慢收盘来源返回和本地冻结写入。失败时
保留 `null`/`not_ready` 并后台退避重试，HTTP 查询仍不得现场抓行情、评分或写盘。

### 6.3 实时性与失败策略

每条观测保存来源、主体、观察/来源/接收/生效时间、响应版本、字段、缺失原因、载荷
哈希、终态和脱敏错误码。未来、无时区、空版本、非法代码、非有限核心值和 deadline
后完成的观测不得进入有效缓存或发布。

目标为：TopK 关键阶段 P95 年龄不超过 2 秒、其他阶段 5 秒；候选主执行 5 秒、
其他阶段 10 秒；全市场主阶段 10 秒、其他阶段 15 秒；SSE 发布 2 秒；today 行情到
评分发布 15 秒。数据年龄超过 cadence 2 倍标记 stale，超过 3 倍标记 degraded；
today 行情超过 20 秒或尾盘冻结行情超过 30 秒只能观察。

全市场截止 20 秒、候选报价 3 秒、研究数据 8 秒、DeepSeek 默认 12 秒。用于 P3 候选
发现的周期性全市场任务必须等待本轮物理刷新完成，不能消费 stale-while-revalidate 返回的
上一轮快照；该缓存模式只允许用于展示或来源失败时的显式降级。全市场成功后
即形成可读 P2；P2 发现阶段以本轮 `received_at` 判断是否赶上当前周期，不能把数据源
最近成交时间误当网络接收时间而整批淘汰；进入候选复核和评分后仍按原始 `source_time`
执行 20/30 秒可执行性约束。历史预热属于独立 P1 进度，只有具备所需历史的代码进入 P3，不能因
尚在预热而把全市场事件记为 expired。来源连续失败
3 次熔断 60 秒，半开只允许一个探测。缓存支持 fresh、stale-while-revalidate、
degraded、负缓存和 single-flight；刷新失败保留最近有效值及原始时间，不得用失败条目
覆盖。全源失败也返回最近有效统一快照，并增加降级原因。

所有外部适配器共用结构化失败分类：`timeout`、`deadline`、`circuit_open`、
`negative_cache`、`cancelled`、`superseded`、`no_data`、`rate_limited`、
`schema_invalid` 和 `source_failed`；分类只保存供应商、操作、是否可重试和有界类别，禁止
持久化密钥或完整异常文本。行情以 deadline、熔断、负缓存和 latest-wins 取消共同控制；
DeepSeek 以单次 timeout、物理预算预留、提交 deadline 和 schema 终态控制，失败不阻塞
本地推荐。不同适配器不为追求表面一致而增加无配置依据的隐藏重试或后台线程。

进入 P1-P3 列式热路径的 provider adapter 固定分为三个显式步骤：
`transform_query` 只规范代码、市场、日期、分页和非敏感请求指纹；
`extract_data` 只执行带 timeout/deadline 的物理 I/O 并返回供应商元数据；
`transform_data` 负责严格 schema、单位、时区、缺失原因和字段血缘，不能计算策略分。
查询必须绑定数据集、来源、主体、请求字段、请求/deadline 时间和来源契约版本；原始载荷
保留接收时间、字段血缘和缺失原因；报价保留来源/接收时间与数据版本；列式批次再绑定
merge epoch、配置/schema、manifest 和内容哈希。无时区时间、非法代码、空版本和非有限
核心值在 adapter 边界拒绝；未来观测继续由统一观测边界拒绝。缺失使用 `null`，不得用
0、空字符串或 `NaN` 冒充观测。

候选实时报价事件只刷新腾讯报价并立即交还事件线程，不得同步等待整批尾盘分钟线；
tomorrow 评分的数据准备阶段按需加载/读取分钟线并受评分预算约束。这样 1 秒 TopK 与
1-2 秒候选计划不会因慢分钟历史长期保持 `inflight`，实际完成速度仍受供应商响应限制。
13:00-14:50 的周期评分以及最终复核/收盘评分在读取 tomorrow 特征缓存前，必须对当前候选
执行一次有界尾盘分钟刷新；刷新复用缓存并受 3 秒市场任务预算约束，失败不得阻塞本地评分。
尚未开始即被取消的分钟请求不得写入负缓存，下一轮评分必须能够继续推进；只有已经发出且
超时的物理请求才进入冷却。

### 6.4 确定性统一行情

来源观测按股票一次索引，目标复杂度 O(S*N)。先按 `source_time`、`received_at` 选择
更新字段，同时点按来源优先级；`data_version` 只比较同来源同时点，最后仅用载荷哈希
消除输入顺序差异。实时字段同时点优先东方财富、再新浪；新鲜候选/TopK 字段优先
腾讯；慢数据不能覆盖更晚实时价格。

来源间价格偏差不超过 0.50% 可通过。超过时，腾讯定向价必须与东方财富或新浪至少
一个观测偏差不超过 0.50% 才算复核，不能用腾讯与自身比较。相同有效输入无论完成
顺序均产生相同字段来源、冲突、缺失、合并 epoch 和规范 JSON 哈希。

## 7. 六阶段内存、缓存与性能路线

### 7.1 固定目标

内存验收使用两个不能互相替代的指标：

| 指标 | 上限 | 统计范围 |
| --- | ---: | --- |
| P1-P6 逻辑缓存载荷 | 248 MiB | 规范序列化载荷、缓存 identity 和按池计费内容 |
| 迁移期进程峰值 RSS | 384 MiB（402,653,184 字节） | Python、Polars 原生缓冲、线程栈、队列、临时副本、新旧 epoch、scalar/columnar 双路径和 P1-P6 全部进程内存 |

P1-P6 逻辑缓存池固定为 248 MiB：

| 池 | 内容 | 上限 | 保留模式 |
| --- | --- | ---: | --- |
| P1 | 来源观测、分钟、历史、研究、主数据、日历、估值和财务 | 128 MiB | TTL |
| P2 | 统一行情、当前报价索引、候选定向报价 | 56 MiB | epoch |
| P3 | 硬过滤、候选特征、板内横截面、竞争组 | 24 MiB | epoch |
| P4 | 九组板块评分、板块批次、全局本地草稿 | 16 MiB | epoch |
| P5 | DeepSeek 主审、挑战者和策略分类结果 | 12 MiB | epoch |
| P6 | 当前 TopK、最近 20 日历史投影、日期索引和 overlay 身份 | 12 MiB | delivery |

P6 固定最多 72 个视图：当前四策略 4 个、最近 20 个交易日最多三策略 60 个、冷读取/
原子替换/回退 8 个；单视图规范 JSON 不超过 160 KiB。任一池满只影响本池新写入，不得
清空其他池；P6 始终优先当前视图和最近 20 日已提交策略投影。同一日期缺少其他策略时，
已有策略记录仍按各自 manifest 与 SHA-256 独立可读，日期接口只返回所查询策略真实存在
的日期，不得因三策略交集为空而隐藏已保存历史，也不得把缺失策略伪装为完整。

迁移期 384 MiB 是整个进程硬上限，不是缓存容量；不得把多出的 136 MiB 分给缓存、
扩大候选、延长 TTL 或保留更多历史。阶段 2-4 和切换验收必须同时记录
`cache_logical_bytes`、`process_rss_bytes`、`process_peak_rss_bytes`、可用时的
`process_uss_bytes`、`python_traced_bytes`、`polars_estimated_bytes` 和
`transient_peak_reason`。延迟测试关闭 `tracemalloc`，内存测试单独开启；任何验收不得
用 Python 分配或逻辑缓存估算代替 RSS 峰值。运行配置的 `performance_budgets.memory`
必须拆分为 `cache_logical_bytes` 与 `process_peak_rss_bytes`，旧 `cache_total_bytes`
单字段或把 384 MiB 当作缓存容量的配置必须启动前拒绝。

A5 终态复验继续使用上述固定上限，没有扩大任何缓存池。迁移压力场景同时保留旧/新
scalar/columnar epoch、六池约 70% 载荷、8 只股票 DeepSeek 批次、20 个 P6 日期、冷读预取、
原子替换和 32 个慢客户端，测得逻辑缓存 `205,468,511` 字节、进程峰值 RSS
`387,186,688` 字节、结束 USS `339,656,704` 字节和 Polars 估算 `1,282,816` 字节。
纯 columnar 100 tick 稳态测得逻辑缓存 `29,661,328` 字节、分配增长 `0.0%`、峰值 RSS
`273,195,008` 字节、结束 RSS `254,447,616` 字节、结束 USS `240,578,560` 字节和
Polars 估算 `1,282,816` 字节。以上是 Python 3.14.4、固定离线 fixture 和当前验收宿主的
实测证据，不代替 Python 3.10-3.13、真实供应商负载或其他宿主的独立复验。

是否收紧 384 MiB 必须作为后续独立决策：先收集更多真实进程、Python 3.10-3.14 和不同
宿主的峰值分布，再同时评估安全余量、降级路径和缓存命中收益。A5 不降低该硬上限，也
不得把当前实测余量转换成新增缓存、候选、TTL、历史或双跑容量。

缓存身份固定包含数据集、来源、主体、请求指纹、交易日、阶段、来源契约版本、配置
版本和 schema。请求指纹为排序后的非敏感参数规范 JSON SHA-256。TTL 条目必须有
TTL、动作年龄、负缓存和容量；epoch 条目只命中完全相同身份；delivery 条目声明驻留
交易日、冷槽、current pin 和单项字节上限。

### 7.2 v17 P1-P6 活动实现

五来源、v16 三板评分与 v17 P1-P6 已进入活动实现。cache schema v6、列式行情变更集合、
一体化发布池、Web 热索引、盘中零完整快照写盘、冻结检查点恢复、局部 SSE patch 和固定
性能 CLI 属于同一运行契约，不允许回退成三组缓存、仓储直读 Web 或盘中完整快照持久化。

该章节固定包含：

- cache schema v6 严格配置，拒绝旧三组布局、未知键和 248 MiB 逻辑缓存漂移；
- P1-P6 分池迁移，跨 epoch、板块、策略、policy、schema 和版本不得误命中；
- Polars 只作为 `infra` 内 P1-P3 的实现细节；`ColumnarQuoteBatch`、
  `ColumnarResearchBatch`、`ColumnarFeatureBatch` 和 `MarketChangeSet` 均为不可变包装，
  只暴露只读投影、身份、内容哈希和字节统计，不把 DataFrame 泄漏到应用层或领域层；
- 列式 schema 使用六位字符串代码、受控板块/阶段枚举、有限 `Float64`、显式整数、
  带时区时间和三态布尔；禁止 `Object` 列和热路径 Python UDF/lambda。默认使用固定 eager
  表达式；lazy 只能由固定 fixture 证明有净收益后逐点启用；
- P3 在最多 360 只候选进入 P4 前只物化一次既有不可变 `FeatureSnapshot`，P4-P6 继续
  使用唯一领域评分、融合、动作、排序、冻结和哈希实现，不复制列式评分分支；
- `ReviewCache` 归入 P5，保持请求键、TTL、预算和失效阈值不变；
- 内存式 `PublishedSnapshotIndex`，当前及 20 日驻留读取不访问 SQLite/文件；
- 普通 local/hybrid 发布只更新 P6 和 SSE，不写完整推荐 SQLite/JSON；
- 普通草稿、重算和重启恢复必须先由 P6 接纳，再更新 RuntimeState、session、检查点和 SSE；
  P6 拒绝时保留上一有效运行态并显式降级。正式冻结仍先完成不可变持久化，但通过 P6 前不得
  标记运行态冻结、消费检查点或广播新身份；
- 11:19:50 与 14:49:50 检查点，正式冻结和 15:00 overlay 才持久化；
- 合格检查点可在边界后恢复，跨日、过旧、迟到、损坏或已 consumed 的检查点拒绝；
- 最近 20 日按策略独立接纳 committed、manifest 与 SHA-256 合格的投影，同日缺失策略
  不得隐藏已有策略历史；
- 更老日期按日期级 single-flight 冷读并预取该日已有策略，部分缺失不伪装为完整；
- SSE 客户端使用独立有界缓冲，慢客户端丢弃并要求 resync；
- 新增无外部网络的 `perf-check` 固定 fixture、身份报告、绝对预算与基线比较；
- 业务投影、动作、排名、风险、版本和冻结哈希必须与迁移前一致。

增量重算按规范值比较，不使用近似桶跳过真实变化；相同输入 manifest 可复用 P2/P3，
配置、策略、schema 或来源契约版本变化必须全量失效。固定路由为：

| 变化 | 最小重算范围 | 发布 |
| --- | --- | --- |
| TopK 定向报价 | `overlay_only_codes` | 只更新 overlay 和对应行 |
| 候选价格、成交、量比 | 股票及受影响板内横截面 | 局部评分后重新执行全局选择 |
| 全市场行情 | 实际变化代码及受影响板块 | 更新必要候选和板块 |
| 行业热度 | 受影响行业候选及其板块 | 局部评分后重新执行全局选择 |
| 新闻、公告、财务、风险 | 受影响股票 | 更新证据与局部评分，必要时进入复核 |
| 配置、策略、schema、来源契约 | 全部 | 完整失效并生成新发布身份 |

dirty 身份不完整或依赖范围无法证明时必须扩大到板块或全量重算；旧版本可以完成审计，
但 compare-and-set 不得覆盖更新的 P6 或冻结结果。

性能 CLI 目标接口：

```text
trader-cli --config <absolute-runtime-json> perf-check
  --fixture <absolute-fixture-directory>
  --suite market-data|board-scoring|api-sse|end-to-end|all
  --output <absolute-json-path>
  [--baseline <absolute-json-path>]
```

固定 P95 上限：5500 行标准化 250ms、两源合并 600ms、统一快照可读 900ms、360 行
定向报价提交 100ms、单板 120 候选预选 250ms、单板单策略评分 250ms、三板三策略墙钟
1000ms、360 只稳定选择 100ms、本地草稿发布 500ms、DeepSeek 结果重发布 1s、
P6 -> SSE 内部入队 100ms、SSE 接收到浏览器下一帧绘制 100ms、权威 SSE 发布年龄 2s、
当前/驻留历史 API 200ms、ETag 304 50ms、日期列表和状态 API 100ms。
相对同身份基线关键路径不得退化
超过 5%，100 tick 项目分配增长不得超过 20%。

正式 `perf-check` 必须执行活动标准化、融合、列式投影、板内评分、全局选择、推荐准备/
终态化、P6/SSE 和 Web 路由，不得用 DataFrame self-join、排序或 JSON 序列化占位冒充
生产阶段。真实浏览器 patch-to-paint 由独立 Firefox/geckodriver runner 验收，其预算读取
同一运行配置；测试进程启动等待不属于 patch-to-paint 预算。

### 7.3 P3-P6 公共接缝

`youhua_contract_base_v1` 是已发布的公共接缝版本，不再表示阶段施工或代理分工。schema、
版本、公共 port/event、配置、publisher 和组合根只能在各自活动模块中保留一套定义；
P1-P3、DeepSeek 和 P6/Web 的内部实现不得分叉公共身份或泄漏基础设施类型。

| 接缝 | 版本 | Producer | Consumer | 最小载荷 |
| --- | --- | --- | --- | --- |
| P3 -> P4 | `p3_p4_feature_snapshot_market_change_set_v1` | B | A/P4 | `FeatureSnapshot` 集合、`MarketChangeSet`、`trade_date`、`phase`、`merge_epoch`、`data_version`、`config_version`、`feature_schema_version`、`content_hash` |
| P4 -> P5 | `p4_p5_high_value_review_manifest_v1` | A/P4 | C | 高价值复核集合、证据 manifest、`price_reaction_bucket`、`owner_strategy`、预算桶、deadline、schema/model 版本 |
| P4/P5 -> P6 | `p4p5_p6_projection_event_v1`、`p6_overlay_event_v1` | A/P4/P5 | D/P6/Web | 完整业务 projection event、overlay event、CAS/version、resync 原因、不可变快照身份 |

`MarketChangeSet` 至少包含 inserted/updated/removed/dirty codes、dirty boards、dirty
industries、dirty field families、evidence manifest/hash、overlay-only 标记和 full
invalidation reason；不确定时扩大为板块或全量，禁止缩小为不完整局部重算。P4 -> P5
的 long 复核集合永久为空且物理 HTTP 请求为 0。P6 projection event 必须保持业务 JSON、
动作、排名、冻结哈希和 Web envelope 兼容；overlay event 只能更新当前报价、收盘锚点和
显示状态，不得改变策略身份。

历史分阶段交接、基线和 G1-G5 门禁只保存在 `docs/reports/` 作为审计证据，不再作为活动
施工手册。后续接缝变化必须更新本文、公共契约测试、生产者与消费者，并在同一独立交付
批次中证明版本唯一和向后兼容。

## 8. 发布、冻结与持久化

发布采用单一合并器和单写持久化边界。普通草稿是可替换的内存版本；检查点、正式
冻结和收盘 overlay 是不同用途，计数和存储必须分开。

| 产物 | P6/SSE | SQLite/JSON | 可修改策略身份 |
| --- | --- | --- | --- |
| 普通 local/hybrid 草稿 | 是 | 否（v17目标） | 可被更新版本替换 |
| 冻结检查点 | 否 | 是 | 仅供边界恢复 |
| 正式冻结 | 是 | 是 | 否 |
| 缺失记录的 `close_fallback` | 是 | 是 | 创建后否 |
| 盘中报价 overlay | 是 | 否 | 否 |
| 15:00 收盘 overlay | 是 | 是 | 否 |
| long 当前观察 | 是 | 否 | 不形成历史身份 |
| 推荐结果结算 | 否 | SQLite | 不修改冻结身份 |

today 在 11:20 冻结，tomorrow/d25 在 14:50 冻结。已有同日正式记录后，迟到行情、
风险、模型结果、补审或重算均不得改变股票集合、分数、动作、排名、版本或 JSON 哈希。
若同日记录缺失，连续运行优先固化本次进程 P6 并换入收盘锚点；冷启动先读库，仍缺失才
用完整同日收盘行情本地重建。冷启动重建除收盘价和三板行情完整外，还必须等待主板、
创业板、科创板分别具备至少 100 只含有效 20 日流动性历史的横截面；历史预热未完成时
保持 `not_ready` 并按 3/5/10/20/30 秒退避重试，不得把
`board_population_insufficient`、`board_data_reliability_below_threshold` 或整批
`history_warming` 的半成品固化为当日正式记录。`close_fallback` 进入正常历史、API
返回 `ready`，来源字段只用于审计；它及其 closing overlay 一经提交同样不可修改。
收盘冷启动重建按缺失策略独立校验和提交，单个策略因专属字段缺失或可靠度不足降级时，
不得阻止同一交易日其它已满足候选、行情、历史和三板可靠度契约的策略创建 `close_fallback`。

SQLite 使用单写线程、WAL、busy timeout、短事务和迁移注册表。JSON 使用临时文件、
flush、fsync、原子替换、manifest 与 SHA-256。冻结提交失败时保留上一个已提交快照并
显式降级；不得呈现半提交版本。v17 运行数据写入 `.runtime/v17`，不得进入 Git；旧
`.runtime/v2` 只允许由 `migrate-v17` 以只读方式导入已提交冻结，禁止新运行写回。

结果结算表以 `snapshot_id + stock_code + horizon` 唯一，保存冻结价、ATR20、未来最低价、
未来收盘价、原始/基准/净超额收益、MAE、质量状态和结算版本。结算只能由后台调度调用，
幂等补齐已到期窗口，不能改写冻结快照、推荐锚点、动作、排名或 JSON；来源失败保留待结算
状态并在后续收盘周期重试。

## 9. Web API 与 SSE

固定接口：

- `GET /api/status`
- `GET /api/recommendations/<today|tomorrow|d25|long>?date=YYYY-MM-DD&top_n=10&view=current|official|live`
- `GET /api/recommendation-dates?strategy=<today|tomorrow|d25>`
- `GET /api/events?cursor=0&limit=100`
- `GET /api/events/stream`

当前快照支持 ETag。SSE 使用单调事件 ID 和 `Last-Event-ID` 恢复；未提供
`Last-Event-ID`/`cursor` 的新连接从 publisher 当前序列开始，避免在已读取最新 ETag
快照后重放旧 projection，只有显式游标才执行历史恢复。游标超前、过旧或不连续时返回
`resync_required`，并在事件体中携带 `patch_schema_version=2` 与
`reason=cursor_ahead|cursor_expired|cursor_gap|slow_subscriber|base_mismatch|schema_mismatch|identity_mismatch`。
客户端缓冲有界，慢客户端不能阻塞 publisher；SSE 连接有效时停止持续轮询，断线后才
低频恢复。

推荐 SSE patch 固定使用 schema v2，事件体必须同时保留兼容字段 `schema_version=2` 和
显式字段 `patch_schema_version=2`。推荐 patch 包含 `base_projection_version`、
`projection_version`、`etag`、`view`、`upserts` 和 `removed_codes`；客户端只有在 base、
schema、身份、TopK 或 ETag 不匹配时才触发完整 GET。overlay patch 只允许携带当前报价、
涨跌、来源、来源时间、报价版本和 `data_age_seconds`，且 `projection_version` 必须匹配
当前 P6 projection；不匹配时以 ETag GET resync，不得局部套用到错误快照。

推荐、日期列表与事件响应的 Web envelope schema 提升为 v3；推荐响应只提供页面实际消费的
快照身份、策略、日期/视图、发布与冻结/降级状态，以及逐股身份、核心行情、锚点行情、
本地/模型/模型风险/最终分、动作、结构化理由、精简风险和模型复核终态。核心字段获取不到
时返回 `null`，禁止伪造 0；
原始特征、权重、分位、板块计算、证据、逐字段来源、完整缺失清单和模型技术审计继续
保存在领域快照与冻结存储中，不通过推荐 Web 响应传输。历史日期只允许
today/tomorrow/d25；long 仅当前日期可读。历史日期列表和显式日期查询均按策略独立读取
已提交记录，同日缺少其他策略不影响当前策略的历史可见性。

推荐逐股对象允许加法返回 `setup_type` 与 `downside`，后者仅包含保护状态、结构化原因、
ATR20 和可展示的历史风险输入，不包含未来结果。`items` 保持单数组兼容并按正式推荐、
观察项顺序合并；普通 Web 的 today、tomorrow、d25 在盘中当前视图只渲染正式推荐，忽略
观察项；`close_fallback` 和历史视图必须渲染 API 已返回的全部 TopK 项，避免收盘补算只有
观察项时被前端过滤成空表；long 则在主表渲染当前固定名单。

Web 当前交易日不 ready 时返回空 `not_ready`，不得用上一交易日冻结推荐冒充当前结果；
盘后后台收盘恢复成功后，同一接口直接返回 `phase=close_fallback`、`frozen=true` 的
`ready` 结果，页面标记“收盘补算”；
today/tomorrow/d25 收盘补算只把收盘报价、20 日历史样本和板块人口不足作为创建阻断；
结构化研究字段或板块可靠度不足时必须创建带降级原因的 `close_fallback`，逐股动作按
`observe` 展示，不得把整个策略清空。`close_fallback` 先按正常动作与观察门槛选择；
若本地候选非空但正式/观察 TopK 均为空，当前视图和历史视图可只读重放冻结输入，按原
集中度规则发布最多 8 个无 veto 的本地候选为 `observe`，并追加
`close_fallback_observation_floor_relaxed`，不得改写已冻结记录或提升为可执行。15:00 后
冷启动若存在长期 watchlist，long 必须用同日收盘价生成当前非冻结快照，只发布当前视图，
不写冻结历史。
历史冻结只允许通过显式 `date` 查询。不 ready 和历史查询都不触发后台刷新、落盘或
计算。普通 Web 固定请求 `view=current` 执行自动当前视图：只读取 P6 中最后一版同交易日
快照，未冻结时响应解析为 `view=live` 并标记“实时数据，结果可能变化”，正式冻结后
响应解析为 `view=official` 并标记“已冻结”；冻结时点已过但正式记录仍缺失时，继续
展示最后一版同日草稿及已有降级原因，不得清空为上一交易日结果。省略 `view` 或显式
使用 `view=official` 继续执行严格正式结果契约，冻结时点后没有同日正式记录即返回 `not_ready`；
`view=live` 是显式只读的同交易日草稿诊断视图。`official`/`live` 均保留 API 兼容，
不得在普通 Web 中作为两个并列操作入口；任一当前视图都不得读取上一交易日。
页面切换策略/日期时必须取消或隔离旧请求，迟到响应不能覆盖当前选择。历史日期页面
必须按后台 P2 刷新节奏重新读取实时列，重新读取本身不得触发网络采集或计算。
today、tomorrow、d25 在“当前”模式间切换时继续请求目标策略的 `view=current`；显式
选择历史日期后，在三种短线策略间切换必须保留该日期。目标策略没有同日归档时页面显示
该策略、该日期的正常空状态，不得自动回到当前日期，也不得用其他日期快照代替。切入 long
时日期固定清空为“当前”并禁用日期选择；从 long 切回短线策略同样从“当前”开始。日期
列表和推荐响应都必须绑定当前策略与日期身份，列表暂时失败时允许直接只读显式日期，
`snapshot_not_found` 仍解析为空状态而非请求失败。

冻结时点没有可用 pre-cutoff 草稿只记录计数，不覆盖最近错误；15:00 后由收盘恢复继续
创建缺失的 `close_fallback`。后台结算读取行情时被 latest-wins source lane 取代属于本轮
结算跳过，只记录 `outcome_settlement_superseded` 计数，不作为最近错误展示。

## 10. 桌面界面

首页固定包含：Header 状态、结果摘要卡、日期和策略切换、当前策略说明、单一荐股表、
选中股票详情、系统状态/DeepSeek 预算与失败原因。当前快照状态与最近错误固定放在页面
顶部 Header 的独立双栏信息带中；三档桌面分辨率下两栏高度固定，长文本只能在各自区域
换行并纵向滚动，不得改变下方摘要、策略或表格的纵向位置。股票列表摘要固定在四策略
按钮行上方，随后直接进入主表，三者无额外间隔相邻；主表不显示重复的“正式推荐”标题行。
普通页面不提供“当前推荐”、
`official/live` 或“收盘补算”操作入口；当前日期始终隐式使用 `view=current`。today、
tomorrow、d25 的单一荐股表只展示正式推荐，不展示独立观察池；long 仍在同一主表展示
当前固定观察名单。摘要显示推荐数、可执行数、过滤数、行情源、最高评分、模型复核数和
数据状态；每行可见关键分数、动作、
数据年龄、锚点价、当前价和风险；详情固定收敛为推荐结论、核心行情、评分与实际风险，
空值不生成占位区块。模型未复核和核心行情不完整只显示一条可读状态，完整技术审计不在
普通详情中展示。

状态使用文字、图标和颜色共同表达，不能仅依赖颜色。加载、空结果、降级、失败和
not_ready 必须可区分。页面保留“仅供研究，不构成投资建议”。三档桌面分辨率不得
白屏、重叠、出现页面级横向溢出或明显布局跳动；长快照状态和错误文本在各自独立区域
换行、滚动，不互相挤压且不挤压核心状态。long 的 `not_ready` 明确显示
“长期策略当前尚无可用数据”，不复用短线
“流水线尚未发布”的提示。

正式推荐为空时主区显示“当前无通过下行保护的正式推荐”，不追加观察表。历史视图保持
单表，不重新按当前动作解释旧版本冻结结果。未冻结的当前快照统一标记为“实时数据”，
并同时说明结果可能变化；`close_fallback` 只作为“已冻结 · 收盘补算”快照状态显示一次，
不得表现为按钮。

## 11. 可观测性与安全

状态至少暴露：线程、队列、latest-wins 合并、拒绝、重放、来源延迟、数据年龄、熔断、
缓存命中/淘汰/字节、single-flight、历史预热计划/完成/在途/覆盖数、策略耗时、DeepSeek
物理调用和预算、冻结状态、SQLite/JSON 写入分类、P6 发布版本、SSE 客户端和慢客户端
丢弃数。

P1-P6 诊断还必须能关联 request started、source received、normalized、merged、
change set、features ready、score ready、DeepSeek ready、P6 published 和 SSE enqueued
时间，汇总各阶段 P50/P95、dirty code/board/industry、局部/全量重算、分池与列式字节、
RSS 峰值和峰值原因。DeepSeek 状态至少汇总平均批次大小、跨策略 facts 复用、缓存命中、
主审/挑战者/emergency 原因、部分成功/修复、输入输出 token 和软桶使用率；SSE 状态至少
汇总 patch 数、upsert/删除数、传输字节、正常更新完整 GET 数和 resync 原因。浏览器
`browser_applied_at` 只用于桌面验收和本地诊断，不成为服务端业务事实。

活动运行时使用组合根注入的单个 `LatencyWaterfall`。关联身份和未完成 trace 最多保留
512 个，阶段名最多保留 64 个，每阶段样本最多保留 512 个；来源 lane 明确记录 queue
wait，物理供应商耗时与标准化、融合、canonical/targeted 提交等本地耗时分开。
`/api/status` 只能返回容量、活动 trace 数、
planned/completed/failed/timeout/superseded/dropped 计数，以及阶段 sample count、
P50、P95 和 max，禁止返回关联身份、代码集合或外部载荷。浏览器只在本地诊断对象中
保留最多 256 个从 SSE 收到到下一次 `requestAnimationFrame` 的成功 patch 样本及丢弃
计数。

日志使用脱敏结构化摘要，不记录密钥、Token、完整模型载荷、完整外部响应或个人敏感
路径。所有外部 I/O 有 timeout、容量和失败策略。DeepSeek 与 Tushare 凭据优先读取
各自环境变量，也可从权限安全的 `.token_key` 分字段读取；绝不进入配置快照、推荐历史
或 API。

默认仅本机访问，不承担公网部署的认证、授权和 TLS。若未来扩大网络边界，必须另立
完整章节处理安全模型，不能沿用本机假设。

## 12. 安装、运行与运维

一键启动使用 `run.sh`、`run.ps1` 或 `run.bat`。手动流程为创建虚拟环境、从
`pyproject.toml` 安装、用绝对配置路径执行 `trader-cli validate-config`，再启动
`trader-server`。任何环境都不得依赖仓库当前工作目录才能读取资源。

日常检查顺序：

1. 校验配置并确认交易日历、时区和运行目录可写。
2. 查看 `/api/status` 的来源、队列、缓存、预算、冻结和最近错误。
3. 对行情 stale/degraded，先区分供应商延迟、熔断与内部 lane 排队。
4. 对 DeepSeek 失败，区分密钥缺失、禁用、预算、deadline、HTTP 和 schema；本地推荐
   应继续可用。
5. 对冻结异常，核对检查点时间、manifest、SHA-256、配置和策略版本；不得手工改写
   冻结文件。
6. 对 Web 历史异常，验证 P6 身份与归档哈希，不允许 HTTP 请求现场重建推荐。

备份只需要配置、版本化冻结、manifest 和必要 SQLite；缓存、构建物和临时文件不备份。
回退必须切换完整旧 release 和对应配置，禁止只替换单个模块。v2 数据不得写回旧运行
库；新增兼容表和文件可由旧版忽略，非独立清理任务不得破坏性删除冻结历史。

## 13. 测试与发布验收

每次发布必须运行：

```bash
make format-check
make lint
make type-check
make test
make package
```

还必须验证架构 AST、`create_app()` 无副作用、固定融合向量 83.40、预算并发上限、
single-flight、latest-wins、来源乱序、SSE 游标恢复和慢客户端、冻结恢复与哈希一致性。
从仓库外安装 wheel 后验证 `trader` 导入、`trader-cli`、`validate-config`、模板、CSS、
JavaScript、图标和 `pip check`。桌面三档需要实际渲染证据；若宿主图形栈阻断，必须把
错误和未完成门禁列为剩余外部风险，不能宣称通过。

全工程重构期间，`make lint` 还必须执行严格复杂度与命名债务的单调收敛门禁：活动树中
`C901`、`PLR0911/0912/0913/0915` 和 `N` 系列问题数量不得高于已登记基线；每个重构批次
必须同步下调已经消除的问题额度，禁止新增同类债务。最终目录切换时这些临时额度必须
全部归零。活动源码单文件仍以第 3 节规定的 800 行为上限，不另设任意的 500 行限制。

性能 fixture 必须固定数据、策略和配置哈希，并记录提交、工作树、源码树 SHA-256、
Python、系统、内核、架构和 CPU。runner 禁止外网，DeepSeek 用固定响应，SQLite 使用
临时库。延迟轮次关闭 tracemalloc，内存轮次单独开启；内存报告必须同时覆盖
`cache_logical_bytes <= 260046848` 和 `process_peak_rss_bytes <= 402653184`。
固定性能配置使用 `performance_budgets.schema_version=2`，并分别执行生产函数性能 CLI
与真实浏览器 runner；任一绝对预算红项必须保留在报告中并阻断发布，不能用其他阶段的
余量抵消。

## 14. 迁移状态、历史决策与路线图

### 14.1 研究参考与采用边界

以下项目只提供工程或研究机制参考，Star 为旧文档记录的 2026-07-19 快照，不代表策略
收益；除 `pyproject.toml` 已声明的 AKShare 外均不是运行依赖。采用第三方源码前必须
固定 commit、核实许可证并记录归属。

| 项目 | 参考边界 |
| --- | --- |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | provider、研究角色和结构化决策；不采用交易结论 |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | 数据 provider 和标准化结果；不引入平台或 AGPL 源码 |
| [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) | DeepSeek、多源 A 股研究和历史验证机制 |
| [Qlib](https://github.com/microsoft/qlib) | 因子组织、点时数据和时间切分 |
| [vn.py](https://github.com/vnpy/vnpy) | 事件调度与任务生命周期 |
| [TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN) | DeepSeek 与 A 股研究流程；采用前复核混合许可证 |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | 确定性事件时间、领域模型和失败恢复；不引入实盘能力 |
| [AKShare](https://github.com/akfamily/akshare) | 交易日历与候选研究数据接口 |
| [Backtrader](https://github.com/mementum/backtrader) | 成本、滑点、持有期和分析器口径；不接回产品运行链 |
| [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) | 金融情绪、RAG、证据增强和任务评测；不引入训练或本地推理 |
| [LEAN](https://github.com/QuantConnect/Lean) | 事件驱动和数据归一化；不引入券商连接或下单 |

公开参数必须重新通过 A 股点时数据、停牌、涨跌停、滑点和样本外检查。社区热度和外部
回测都不能替代本项目的策略验收。

### 14.2 迁移与历史决策

v1 已退出活动树，活动实现只在 `src/trader`。迁移阶段完成了包边界、只读 Web、事件
流水线、冻结持久化、五来源并行、统一观测、v16 三板评分、结构化 DeepSeek 与 wheel
资源验证。旧 release 只用于完整回退，不得被活动代码导入。

历史文档中的有效结论已归并如下：

- 2026-07-17 的 16 项契约审计覆盖冻结、动作、过滤、缺失值、时间线、DeepSeek、
  持久化、Web/SSE、并发和验收；已实施项以当前代码和测试为证，真实交易日、外部
  服务和图形栈证据仍按剩余风险单独记录，具体运行快照 ID 不作为长期契约。
- 2026-07-20 的外部项目比较仅吸收 provider 边界、结构化证据、受控路由、有界并发、
  schema 和迁移注册机制；不复制第三方交易结论，不引入多智能体生产编排、券商连接
  或新的运行依赖。
- 历史问题与逐次落地日志由 `CHANGELOG.md` 保存，不再在活动文档重复形成第二套状态。
- v15 五来源、v16 三板评分、v17 P1-P6，以及组合根、Web、CLI 和最终清理均已完成；
  原全工程重构第 2.1-2.6 节全部闭合。收益验证、调参或新策略仍属于独立后续路线，
  不因工程等价重构完成而自动启用。

## 15. 交付与文档治理

### 15.1 独立交付批次

用户每发送一次“继续”或语义等价指令，都形成新的独立交付批次，只处理计划或本文中
下一个完整未完成章节，以同级标题为边界，并完成章节内全部明确子项。不得只完成首个
子项后停止，不得顺带处理相邻章节。开始前记录 `HEAD`、上游、既有工作树变更和任务
文件范围；此前批次未闭合 Review、提交或推送时先闭合此前批次。

每批先更新契约与失败测试，再实现；完成后以已推送基线审查完整 diff，修复所有已知
发现，运行适用质量、测试、构建、仓库外 wheel 和桌面门禁。必须在 `CHANGELOG.md`
归纳用户提出的问题或诉求、原因或现状判断、修改说明和行为变化、验证证据、剩余风险
及后续项。提交使用一个准确的 Conventional Commit，推送当前跟踪分支并确认
`HEAD == @{upstream}`；成功后停止，等待下一条指令。

### 15.2 两份权威文档与非权威执行计划的更新边界

产品范围、架构、生命周期、时间线、数据服务、发布/冻结、API、UI、运维、性能和验收
变化更新本文；候选、过滤、因子、评分、风险、DeepSeek、融合、动作和 TopK 变化更新
荐股策略文档。跨边界变更同时更新两份。依赖和入口只更新 `pyproject.toml`；执行记录
更新 `CHANGELOG.md`，阶段性基线和交接报告可放入 `docs/reports/` 且必须标记门禁状态。
`docs/strage.md` 只记录尚未获准实施的收益优化批次；`docs/times.md` 按 T1-T5 记录实时
性能优化的完成状态、测量基线、红线和退出条件。两份计划必须显式标记为非权威并链接
本节规定的两份权威文档；发现冲突时以权威文档为准，实施引起契约变化时先更新权威文档。
已经完成的全工程重构、P1-P6、DeepSeek V4 和 P6/Web 分阶段方案不再保留活动计划，施工
证据只归档在 `docs/reports/` 和 `CHANGELOG.md`。除此之外不得在 `docs/` 新建并行需求、
计划、问题单、运行手册或归档文件。
