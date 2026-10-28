# 数据库存储、字段命名与全量重建方案

本文合并公司存储轻量化、历史启动和全库优化方案，区分已落地代码与待实施目标。第0节仅摘录固定基线的进度，执行状态、实际改动、验证证据和未完成项仍只维护在[工程实施](3_工程实施.md)，不在本文另建活动台账。

评分、选席、风险及收益资格以[评分逻辑](1_评分逻辑.md)为准；架构、运行及冻结以[工程设计](2_工程设计.md)为准；下载、历史、样本和模型以[下载训练](4_下载训练.md)为准。公司正式库、采集库及发布合同已整合至[公司存储协议](#company-protocol)、[正式库字段](#company-schema)、[采集库字段](#company-raw)和[公司发布与读取](#company-publication)。对应行为实际切换时更新权威文档，不能用本文提前解释旧数据。

## 0. 已有进度与后续起点

下表摘自2026-10-28已推送提交`17923683`及[工程实施阶段281](3_工程实施.md#阶段281公司存储体积与历史启动超时)，是本次合并的基线摘要，不是当前机器的新实测。后续实施只更新原台账，阅读本文时先核对台账的新证据。

| 任务 | 基线进度 | 尚缺内容及归属 |
| --- | --- | --- |
| 公司正式库与采集拆分 | 代码已实现：12张正式关系表、24列主表、固定枚举、日期/时刻codec和二进制摘要 | 阶段281：活动旧库尚未清空重建，不能由新reader直接读取 |
| raw与正文滚动清理 | 代码已实现：20页/50篇批次、PDF有界驻留内存、压缩正文、逐股回执后清理 | 阶段281：全市场占用峰值、真实失败恢复及提取准确性待验收 |
| 永久排除及消费者切换 | 代码已实现：独立eligibility活动owner删除；非ST排除归company_exclusion，ST仍归既有参考 | 阶段281：重建后验证字段覆盖、人口和持久化；storage配置仍需核对新路径 |
| 公司与历史定向验证 | 阶段281记录310项定向通过，覆盖隔离SQLite、恢复及序列隔离；非全量或真实整链验收 | 新改动另跑对应门禁，不将旧测试结果算作本批通过 |
| 历史预加载优化 | 部分实现：年度读取合并、月证明查询及带DB/WAL身份的缓存 | 阶段281：真实V1/V2/V3仍在3914/4401股处触发300秒history_preload_deadline |
| 公司体积与完整链路 | 未完成：4402股合成测量超过360秒，未取得有效体积/耗时结论 | 阶段281：4401股规模测量、活动公司重建、字段覆盖、三档真实启动 |
| 全库及字段命名 | 本文重新规划；已实现公司字段仍保留在字典的“当前字段”列 | A包完善清单；B/C/E/F按责任边界原子切换，目标名称不能标为已实现 |
| 参考合并、冻结库、DeepSeek文件化 | 待实施 | 分别由B/C/D包接续；不因公司代码已落地而标记完成 |
| 样本/列式、全链演练与全量重建 | 待实施 | F/G/H包；性能、真实数据、模型、收益及发布资格分别验收 |

公司阶段281的剩余工作继续沿用原编号和原负责人，不拆到新阶段；合并本文不关闭281或接管213。后续实施记录从[阶段282](3_工程实施.md#阶段282数据库方案a包基线与字段映射)接续，不能将上表固定快照当作最新状态。

## 1. 已确定的范围与目标

### 1.1 用户已确定的选择

| 项目 | 本轮选择 |
| --- | --- |
| 优化范围 | 下载、训练、荐股及其全部活动数据库；全面复审库、表、字段和存储类型；合理名称保留，含糊或冗长名称按第3节调整 |
| 数据处理 | 旧业务数据不迁移，清空后重新下载，V1/V2/V3/V5重新训练；新推荐和研究从重建后开始 |
| 业务规则 | 评分公式、模型算法及参数、特征含义、六席规则、风险、时效、冻结和收益准入保持不变 |
| 运行资源 | 按当前4个逻辑CPU、内存可增加至10 GiB规划；执行前核验实际分配，不能把计划容量当成已分配 |
| 磁盘容量 | 不作为方案取舍或性能通过条件；仍记录物理I/O，不取消写入失败处理 |
| SQLite | 保留本地事务事实、断点和冻结存储，优化访问模式，不强行合成一个大库 |
| 公共参考合并 | inputs.json与historical_st.sqlite3的职责合并到market/reference/company_st.sqlite3；证券、ST、日历及参考发布同库管理 |
| 列式处理 | 使用已有Polars对照具名列Parquet；按第6节的固定门槛决定是否接入，不新增DuckDB或PyArrow |
| 公司风险 | 沿用阶段281已实现的采集、正式关系表和临时正文设计；E包按用户指定独立切换字段，原阶段的真实验收保持原归属 |
| 冻结库 | 正式冻结统一使用freeze.sqlite3，活动代码不保留旧文件名别名或双读 |
| DeepSeek固定参数 | 继续使用config/deepseek.json，不建立DeepSeek数据库 |
| DeepSeek运行状态 | 内存维护，使用runtime_dir/deepseek/state.json临时文件保存预算和必要恢复状态；默认.runtime/trader/deepseek/state.json |
| 执行前提 | 用户已自行清空data；按A→各实现包→G/H推进。清空目录不等于新schema、下载、模型或运行已验收 |

全面改名用于可读性和可维护性；批量读取、减少重复扫描、减少对象转换和短事务用于提速。两类验收分开，不能把改名或SQL条数减少直接写成性能收益。

### 1.2 必须保留的合同

1. Tomorrow/D25是评分策略，Long只展示当前快照，不排名、不冻结、不写推荐历史。
2. V1使用模型排序分选席，本地分决定动作；V2/V3/V5沿用各自既有本地评分和模型门合同，V5权限不因工程改造升级。
3. DeepSeek只提供经证据核验的结构化风险veto，不数值扣分；每天跨档最多168次物理HTTP尝试，失败、重试和修复尝试同样计数，预热和emergency独立限额不变。
4. 15:00 first-wins；已有正式记录不能被迟到结果覆盖，缺记录时只能创建合法close_fallback。
5. 所有业务日期使用Asia/Shanghai；deadline使用可注入单调时钟。日期、来源年龄、输入及发布身份不能为提速而放宽。
6. 下载拥有离线事实，training拥有样本和模型，recommendation拥有决策、冻结、在线结算、追踪和DeepSeek状态。HTTP/Web仅调用推荐应用层只读用例。
7. 组合根仍是bootstrap.py；domain不做I/O，application不依赖本业务infra。JSON和SQL停留在adapter/codec边界，内部传递有真实类型的不可变对象。
8. 一项职责只有一个活动owner；不保留兼容转发、双实现、隐式fallback和重复状态源。

## 2. 目标文件与数据库组织

### 2.1 目标目录

下面是完成后的活动布局。`{profile}`只允许v1、v2、v3、v5；取消V1在推荐和研究目录中的learned_rank路径别名。目录按职责及生命周期保留，库数量本身不是性能指标。

```text
config/
  deepseek.json                         # 人工维护的固定参数，不保存预算计数

.runtime/trader/                        # 默认runtime_dir，运行生成，不提交Git
  deepseek/
    state.json                         # 预算、在途请求及有界熔断状态临时文件

data/
  .deepseek.lock                       # 同一data_dir的DeepSeek运行锁，不保存预算
  market/
    reference/
      company_st.sqlite3               # 证券、ST、日历及不可变参考发布
      industry.sqlite3                 # 保留现名；历史锚点与当前行业分表
    history/
      catalog.sqlite3                  # 保留现名；历史控制与发布指针
      prices_YYYY.sqlite3               # 保留现名；年度raw/qfq修订及观察
    daily_window.sqlite3                # 保留现名；独立raw/qfq短窗及绑定
    analytics/                         # 仅列式对照通过后接入
      daily_prices/<月内容摘要>.parquet # 不可变的具名类型列投影
      manifests/<快照摘要>.json         # 精确绑定原始快照与月份文件
  company/
    company_facts.sqlite3              # 正式事实；已实现结构见第12节
    raw/
      company_collection.sqlite3       # 公告定位、结构化输入、分页与重试
      text/<分片>/<内容摘要>.txt.gz     # 临时压缩正文；PDF只驻留内存
  training/
    samples/{profile}/samples.sqlite3  # 各档临时样本及合法续传缓存
    models/{profile}/{tomorrow,d25}/   # model.json、metadata.json整档发布
  recommendation/{profile}/
    freeze.sqlite3                     # 正式冻结决策
    publication_state.sqlite3          # 持久发布序号，不复制离线参考
  research/{profile}/
    outcomes.sqlite3                   # recommendation写结算，training只读研究
    traces/YYYY-MM-DD.sqlite3          # recommendation写决策追踪
    experiments/<研究身份>/            # 隔离研究样本、残差及报告
```

runtime_dir由现有配置解析，不在adapter内硬编码`.runtime/trader`。DeepSeek运行状态不放进config、不放进data；data根只放一个不承载预算的进程锁，防止更换runtime_dir后同时启用两份复核。临时状态不能当作可跨PC分发的配置。所有路径由现有DataPaths及组合根显式提供，不增加第二套路径推断。

### 2.2 文件名复审与旧路径去向

| 数据组 | 改造前路径或结构 | 目标与处理 |
| --- | --- | --- |
| ST | market/reference/historical_st.sqlite3 | 职责并入company_st.sqlite3，版本化证据和当前采集指针分表；取消独立ST库 |
| 公共参考 | market/reference/inputs.json | 职责并入同一company_st.sqlite3，以不可变发布、版本成员及当前发布指针替代整包JSON |
| 行业 | market/reference/industry.sqlite3 | 保留industry.sqlite3，父目录已明确reference；当前行业与历史锚点不互相替代 |
| 历史控制 | market/history/catalog.sqlite3 | 保留catalog.sqlite3，父目录已明确history；结构化控制表，不引入通用键值事实表 |
| 年度价格 | market/history/prices_YYYY.sqlite3 | 保留prices_YYYY.sqlite3；保留年度分区和月逻辑身份 |
| 短窗 | market/daily_window.sqlite3 | 保留daily_window.sqlite3；raw/qfq配对及双档发布原子性保持 |
| 公司正式库 | company/company_facts.sqlite3 | 保留路径及已实现的12张正式表；目标字段映射见第12节 |
| 公司采集 | 旧事实库内的公告、分页、正文、重试 | 代码已拆入raw采集库及临时压缩正文；不重做采集，字段复审见第13节 |
| 公司资格 | company/eligibility.sqlite3 | 独立活动owner已删除，活动旧文件仍待清理；ST暂归既有参考、B包后归company_st.sqlite3，非ST排除归正式公司事实 |
| 旧公司证据 | company/evidence.sqlite3 | 清理清单明确列出；不作为失败回退或旧数据导入来源 |
| 正式冻结 | recommendation下的旧正式记录库 | 统一为freeze.sqlite3；新库不导入旧记录 |
| 推荐快照 | recommendation/*/snapshots.sqlite3 | 删除没有活动读写消费者的参考表体系；发布序号进入publication_state.sqlite3 |
| DeepSeek | recommendation/shared/deepseek_budget.sqlite3 | 删除全部活动SQLite读写及建表；固定参数保留config，预算及健康改为内存加临时文件 |
| V1目录 | recommendation/learned_rank、research/learned_rank | 统一为v1；不改变模型排序尺度标识和评分语义 |
| 样本及研究 | 各档样本、收益、追踪、研究SQLite | 按现有生命周期保留分库，统一表名、字段及类型，重建时不迁入旧内容 |

旧表是否删除必须搜索全部活动消费者后确认。源码中相似名称的研究接口不等于空置推荐data-plane的消费者，不能误删独立研究能力。旧格式明确拒绝，不在运行时现场改表或尝试旧文件。

### 2.3 数据与投影的所有权

| 数据 | 唯一事实/写入owner | 消费者与读法 |
| --- | --- | --- |
| 证券、ST、日历、参考发布 | download，统一reference_index adapter | 下载共用准备；训练和推荐按不可变发布读取，不读取采集中的当前指针 |
| 行业 | download，既有行业owner | 独立行业库；公共参考固定其发布身份，历史锚点与当前行业分开读取 |
| 控制库、历史修订、短窗 | download | 推荐按快照取有界窗口；训练与研究按固定身份扫描 |
| Parquet价格投影 | download的历史发布adapter | 由SQLite发布快照派生，可重建；不能独立修订事实 |
| 公司raw及正式事实 | download，沿用阶段281已实现owner | 推荐只预读正式轻量事实；证据详情由只读用例显式查询 |
| 样本及模型 | training | 同档双头共享合格样本；各档模型、样本及发布身份独立 |
| freeze及发布序号 | recommendation | 冻结、恢复及只读历史查询；不授予V5正式写入资格 |
| DeepSeek配置 | 现有配置owner | 启动解析为类型对象，不由预算adapter回写 |
| DeepSeek临时状态 | recommendation，组合根注入共享实例 | 跨档原子预留；状态接口读取内存，不每次读文件 |
| 在线收益和追踪 | recommendation | 结算编排、端口及存储写入统一归recommendation，training只读；切换全部消费者后删除training下旧活动实现 |

### 2.4 company_st.sqlite3合并方案

合并范围是`inputs.json`中的证券总体、日历、ST引用及发布身份，加上`historical_st.sqlite3`的持续核验与永久ever-ST事实。目标固定为`data/market/reference/company_st.sqlite3`。名称中的company表示证券主体参考，不接收公告、财务、质押、解禁、正文或非ST排除；这些仍归第12、13节的公司正式库及采集库。行业库也不在此次合并范围内。

原设计会把代码、ST状态和日期信息同时写入ST库和整包JSON。新设计只保存版本化事实，发布成员通过ID引用事实版本，取消整包JSON副本。需要保留的是采集断点与完整发布两个生命周期，不是保留两个可独立修改的当前ST来源。

#### 表划分与字段责任

下表确定实体、关键字段和关系；所有日期、时刻、摘要、空值和类型遵守第3节。完整逐字段字典随对应实现切片交付，不把本表作为运行数据或第二份进度记录。

| 表 | 关键字段及关系 | 职责 |
| --- | --- | --- |
| schema_contract | schema_version、schema_id | 库格式校验；拒绝旧格式，不现场改表 |
| security | security_id主键，code、name、board、listed_date、delisted_date、source_version、content_hash | 保存不可变证券资料版本；名称或上市区间改变生成新版本，不改旧发布引用的资料 |
| security_st_evidence | st_id主键，code、checked_date、st_status、content_hash | 保存不可变ST证据版本，状态为ever_st、clear、unknown；相同内容复用，实际核验日期不被本轮准备日覆盖 |
| security_st_current | code主键，st_id外键 | 仅供ST采集续传和合并查找最新可用证据；已确认ever_st不能被clear/unknown替换 |
| calendar_version | calendar_id主键，content_hash、first_trade_date、last_trade_date、session_count | 保存可靠开市日集合的版本和完整性信息；同内容日历复用版本 |
| trading_calendar | calendar_id外键、trade_date，联合主键 | 仅保存来源已确认的开市日，不把未出现日期自行标为休市；顺序和覆盖按原日历合同校验 |
| reference_publication | publication_id主键，content_hash唯一，checked_at、calendar_id外键、industry_version、security_count | 保存完整发布的不可变身份；industry_version允许按原降级合同为空，不能因此伪造行业合格 |
| reference_member | publication_id外键、code，联合主键；security_id、st_id外键 | 将一次发布绑定到具体证券版本及ST证据版本；两项引用必须属于同一code，禁止串股 |
| reference_current | singleton主键固定为1，publication_id外键 | 唯一当前发布指针；仅在整组校验通过的同库事务中推进 |

`security_id`、`st_id`和`calendar_id`使用明确的内部版本键，不能只用code关联可变当前行。证据内容身份包含实际核验日期；当天clear证据可以复用，跨日需依原规则重新核验，永久ever_st可复用既有确认。本轮资格准备日期由reference_publication.checked_at表达，不伪造新的供应商核验日期。转换到现有PreparedReference端口时，ST条目的checked_on按原合同表示本轮准备日，原始核验日期继续保留在不可变证据中，不能回写覆盖。

主键和外键保证发布、证券、ST与日历关系；证券和ST版本的内容摘要去重，成员和采集指针通过包含code及版本键的组合外键防止串股。security_st_current按证券主键批量读取，reference_member按发布及证券主键批量读取，日历按版本及日期主键范围查询；按版本键关联不可变事实，不每股单独开连接。已发布版本及其成员禁止UPDATE覆盖，本轮不回收已发布参考版本，避免删除历史、训练和研究仍绑定的事实。

#### 采集、发布和恢复顺序

1. 公共准备读取security_st_current恢复ST核验，网络期间不持写事务。已确认ever_st永久排除；当天clear复用；缺失、过期或unknown按现有来源规则核验。每批插入不可变证据并更新采集指针，中断后保留合法进度。
2. 获取并校验证券总体及可靠日历；当前行业沿原owner准备，失败仍遵守原价格降级和训练待补规则。证券、日历内容复用已有版本，不重复写同一事实。
3. 公共准备完成后固定本次准确的证券、ST、日历及行业身份，检查每个总体成员有明确ST证据、证券区间正确、日历有序唯一且覆盖合格。该条件是参考完整，不意味着unknown证券获得下载或荐股资格。
4. 在同一个短事务写入reference_publication及全部reference_member，并推进reference_current；任一步失败全部回滚，消费者继续读取上一完整发布。事务前不提前更新当前发布指针，事务中不执行HTTP。
5. 推荐启动在同一个只读事务捕获publication_id，批量读取该版本的证券、证据及日历，转换为原应用端口的不可变类型对象；不得将新security_st_current与旧reference_member混用。按绑定行业身份校验外部行业投影，冲突时保留原合法视图并标记待补，不假称跨库原子。
6. 训练、研究和历史按准确publication_id及content_hash读取，不能每次跟随reference_current。当前资料更新不会改变旧发布成员或旧资格结论；正常重启才加载新慢资料，不新增服务轮询。
7. 历史catalog的绑定参考改为引用不可变publication_id及摘要，不再复制完整ST数组、证券总体或日历JSON。原历史行业区间证据继续按既有点时绑定合同保存，不能用当前行业替代。先持久化参考发布，再提交catalog绑定及历史发布指针；两库之间不宣称原子事务，中断可以留下未被使用的参考版本，不能留下已发布但缺失的引用。
8. 同一上海日期、同逻辑内容的重复发布必须幂等，复用publication_id并保留首次发布时间；内容身份沿用现有参考合同的逻辑值，不因重复准备的墙钟变化制造新版本。同publication_id异内容拒绝。损坏、缺失版本、外键不匹配、摘要错误或覆盖不足时明确不可用，不退回inputs.json或独立ST库。

#### 消费者切换和合并验收

工作包B内一次性切换ST采集/合并、公共PrepareReference发布、同日日历复用、history/market/company共用准备、训练及研究绑定、推荐PublishedReferences、资格过滤、公司同步的参考输入、CLI、状态、存储探测和全部诊断fixture。DataPaths及bootstrap显式注入新库和端口；application/domain不因合并直接导入SQLite。原ReferencePublication和ST codec/merge的职责转入新adapter，旧活动实现删除，不留转发模块。

旧inputs.json、historical_st.sqlite3及其锁/WAL/SHM路径进入活动重建清单；撤销之前拟议的security_st.sqlite3目标，不创建该中间库，不执行导入旧事实。第14节按已切换的company_st.sqlite3参考接线说明消费者，不接管原公司阶段的真实验收。

定向回归必须覆盖：批次核验取消后续传、ever_st不降级、clear跨日失效、unknown保持未知、同日重复准备、发布中途失败、当前指针原子性、证券名称变化、新旧发布隔离、跨证券错误引用、日历版本不完整、行业身份冲突、catalog引用缺失及发布后采集指针变化。比较原PreparedReference逻辑值及资格结果一致，单独记录新库批量读取和发布事务耗时；合并是去重与一致性改造，不预先宣称大幅提速。

## 3. 全库命名和存储类型

### 3.1 字段协议

命名要求是简短、通俗易懂、专业且含义准确。先用表名确定业务对象，再用最短且不歧义的字段名表达事实；不把数据库、业务和表名前缀层层复制到每列。已有清楚的名称保留，只有含糊、误导、单位不明或不必要冗长的名称才改。

新增表用明确业务实体的snake_case；不对已有清楚的复数表名机械改单数。Python符号遵守[仓库命名规范](../.agents/skills/python-engineering-guide/references/coding-standard.md#3-命名格式与可读性)，本节只规定数据库存储名称；不借SQL改名扩大为全仓Python风格整理。

| 类别 | 目标名称示例 | 简短与专业的边界 |
| --- | --- | --- |
| 证券 | code、name、board | 表内明确是A股证券时用code，六位ASCII数字TEXT；不反复加stock/security/company前缀 |
| 业务日期 | trade_date、report_date、unlock_date | 明确日期用途；不统一成含糊的day/date |
| 覆盖范围 | start_date、end_date | 字典注明闭区间或截止语义；同表多种覆盖保留announcement等必要限定 |
| 时刻 | disclosed_at、checked_at、published_at、committed_at | 保留真实业务动作与_at，不能用time混淆；正式事实用UTC微秒INTEGER |
| 价格 | raw_open、raw_close、qfq_open、qfq_close | raw/qfq是已有行情口径，表已表达price；不重复unadjusted和_price |
| 数量与金额 | volume、amount、parent_profit、contract_amount | 单位在逐字段字典中固定为股、人民币元；多币种或多单位同列族时才在名称加_cny等限定 |
| 百分数与收益 | pledge_pct、roe_pct、return_ratio | _pct表示1.5=1.5%，_ratio表示0.015=1.5%；不能为缩短而删除比例口径 |
| 财务 | eps、bps、revenue_growth_pct、profit_growth_pct、core_profit_growth_pct | 只采用公认EPS/BPS/ROE缩写；保留归母与扣非的区别，不创造晦涩首字母缩写 |
| 状态与布尔 | status、finance_state、is_complete | 表内生命周期用status，完整性用state，布尔用明确谓词；不丢掉同表多个组件的区分 |
| 关系与身份 | id、risk_id、evidence_id、publication_id、revision_id、sync_sequence | 表自己的主键可用id；外键必须说明所指对象；修订、版本、发布和序号不可混成一个version |
| 摘要 | content_hash、body_hash、record_hash、freeze_hash | 字典明确SHA-256及32字节BLOB；不在每列重复_sha256；普通ID不能因外观相似就转摘要 |
| 规范字节 | record_bytes、event_bytes、sequence_bytes | 指明字节的对象，保留真正需要的BLOB；不改成无含义data |
| 失败 | failure_code；raw可保留failure | 正式库是稳定类别，raw是来源细节；不能将详情误改名为code或写进正式数值事实 |


每个SQLite列必须声明真实类型、可空性及必要约束。REAL写入和读取拒绝NaN/Infinity；主外键、唯一约束、日期配对和枚举CHECK共同保证关系。普通价格、收益和金额不得继续以BLOB声明后混存数值。采集库已有ISO日期/时刻TEXT作为明确的raw边界例外保留，见第13节；不为了字段改名额外迁移采集时间格式。

行情价格和成交量继续使用REAL并保留来源精度，不转换为整数分或未经验证的整数股数；成交额和利润等金额以人民币元计量。名称缩短不改变raw/qfq口径、单位、缺失值或精度。

年度价格及短窗增加number_flags数值标记，按raw、qfq各9个数值列顺序，用低18位保留原整数类型、高18位保留浮点负零。SQLite REAL亲和性会将整数读成浮点、将负零读成正零；解码时使用标记还原原数值表示，使既有规范摘要与逻辑发布身份保持不变。不能由REAL精确表达的整数拒绝写入，不以舍入方式生成另一事实。该列只是数值表示元数据，不保存第二套价格。

NULL用于未知、缺失或不适用，0只表示已确认的零；空字符串不得代替缺失日期、摘要和数值。数值改变单位必须有显式codec和往返测试，不能靠名称变化隐式乘除100。

不为本次改名提高最低SQLite版本；在受支持环境中使用明确类型、CHECK与codec落实约束。正式冻结、事件和序列块保留确有用途的BLOB/JSON边界。用户已追加授权全仓及公开协议统一freeze命名：新规范字段参与新摘要，不保留旧字段别名；旧记录留在隔离副本，不原地改字节。SQL列类型改名本身仍不改变业务值。

### 3.2 必须覆盖的表族

| 表族 | 结构调整与命名要求 |
| --- | --- |
| 证券/ST/公共参考 | 按第2.4节归company_st.sqlite3，事实版本、采集指针、发布成员与发布指针分表；不再维护inputs.json副本 |
| 行业 | 独立industry.sqlite3；历史行业查询、当前行业查询分别具名，来源、检查日、锚点日和结果身份不能混列 |
| 历史控制 | 下载批次、任务、缺口、不可用qfq、绑定参考、不可变快照、当前指针及schema_contract各有职责 |
| 年度价格 | daily_price_revision保存完整raw/qfq修订；daily_price_observation关联日期、证券、同步序号和修订 |
| 独立短窗 | 价格单元、档位绑定、日历、完成状态及发布记录分表；不按V2/V3复制同一价格事实 |
| 公司正式/采集 | 保留第12、13节已实现的关系和code；按“原字段→现字段”映射调整冗长及含糊字段，不另建同义事实表 |
| 训练 | 样本、日期分区、统计、诊断和序列块分别具名；f0/f1替换为真实特征语义列，禁止通用EAV特征表 |
| 冻结 | freeze_record保存正式记录；新库不创建已经退役的定时检查点表，旧检查点读取代码随无迁移切换删除 |
| 发布序号 | publication_sequence记录预留高水位；schema_contract说明库合同，不承载证券/日历副本 |
| 收益和追踪 | 明确关联原决策身份、观察期限、成熟状态、原始事件字节和隔离失败类别 |
| 研究样本/残差 | 按真实研究身份命名；spec、总体、日期、预测和结算边界保持，不能并入生产决策 |

完整字段映射写入下载训练权威文档：每个旧字段必须列出保留、改名、派生或删除去向、目标类型/单位、写入者及消费者。删除要有无消费者或可推导证据，禁止按字段名猜测。最终当前结构字典按第10节格式逐字段交付。

模型内部特征ID、特征排列和供应商固定字段不随数据库统一改名。API、事件和规范字节按用户追加授权统一使用freeze字段，前后端及诊断同批切换。存储adapter使用明确的类型化映射，不创建供旧消费者继续使用的兼容别名。V5序列块维度、dtype、字节序及通道顺序单独写入存储合同。

### 3.3 跨库命名清单与消费者

下表复审当前源码字段及原方案候选名，明确保留或改名；第12、13节另按每个公司字段逐行映射，第15节补齐其余持久表和动态特征。旧候选不是已实现名称。data已清空，当前清单来自源码建表与codec，不能冒充已删除实库的探测结果；实现包按清单切换全部消费者，不允许运行时按两种列名猜测。

| 范围 | 当前字段或旧候选 | 目标字段 | 决定与理由 | 工作包；owner及消费者 |
| --- | --- | --- | --- | --- |
| 证券及关联键 | code；原方案security_code、security_name | code、name | 保留现有code，撤销重复前缀；六位证券代码语义不变 | B/E/F；download、training、recommendation及其只读投影 |
| 参考版本键 | 原方案security_version_id、st_evidence_id、calendar_version_id | security_id、st_id、calendar_id | 缩短关系键；仍引用不可变版本，不改成可变当前行 | B；参考采集、发布、历史绑定、训练及推荐 |
| 参考摘要及合同 | 原方案content_sha256、schema_identity | content_hash、schema_id | 算法与格式由合同说明，不在每列重复长名称 | B；参考、历史、短窗adapter及诊断 |
| 年度/短窗价格 | raw_open/high/low/close、qfq_open/high/low/close、raw_volume、qfq_volume、raw_amount、qfq_amount | 保留现名 | 已经简短并区分raw/qfq；不采用unadjusted_open_price等长候选 | B；下载写入、历史读取、训练扫描及结算 |
| 年度/短窗百分数 | raw_pct_change、qfq_pct_change；raw_turnover、qfq_turnover | raw_change_pct、qfq_change_pct；raw_turnover_pct、qfq_turnover_pct | 统一百分数后缀，数值不乘除100；前提是逐列核验现有百分数口径 | B；价格codec、索引及全部窗口消费者 |
| 价格完整性/交易状态 | status、raw_present、qfq_present、raw_trading_status、qfq_trading_status | 保留现名 | 单元状态、侧存在性及交易状态各有职责，不合并为一个status | B；下载、配对校验、训练及推荐 |
| 历史批次/指针 | download_batch.sequence、published_snapshot.sequence；download_batch.cutoff、download_qfq_unusable.checked_cutoff | sync_sequence；两表各自的cutoff_date | 明确同步序号及已核验的任务截止日，不能把截止日改成执行核查日 | B；规划、续传、发布、固定快照及诊断 |
| 历史身份 | revision_id、sync_sequence、first_seen_sequence、content_hash | 保留现名 | 修订ID、观察序号、首次见到序号及内容摘要不能混用 | B；年库、catalog、证明、研究读取 |
| 公司主事实 | financial_report_date、basic_eps、book_value_per_share、pledge_ratio_pct等 | report_date、eps、bps、pledge_pct等 | 利用表上下文缩短；保留财务、质押、解禁和公告完整性区别 | E；公司writer/reader、评分输入、证据详情、排除、CLI及诊断；逐行见第12节 |
| 公司采集 | start、end、day、digest、identity、policy | start_date、end_date、as_of_date、record_hash及具名策略ID | 消除范围、统计日和身份歧义；不把raw原文误改成正式失败码 | E；采集、分页、重试、恢复、编译与清理；逐行见第13节 |
| 冻结身份 | strategy、version、payload_hash、payload_sha256、payload、error | strategy、freeze_id、freeze_hash、record_hash、record_bytes、failure_code | 区分原决策身份、决策摘要和整条规范字节校验；保留trade_date、committed_at、commit_kind、status | C；冻结、恢复、历史查询、结算、研究、CLI及只读投影 |
| 发布序号 | publication_sequence.reserved_until | 保留现名 | 直接说明持久预留高水位，不简写为seq | C；序号预留、发布与SSE恢复 |
| 训练样本 | V1的day、各档code、industry、average_amount_20d | trade_date、code、industry、avg_amount_20d | 对齐真实交易日；avg为公认统计词，成交额单位在字典固定 | F；样本生成、续传、拟合、资格及诊断 |
| 样本标签 | target_t1～target_t5、target_d25、target_d25_aggregate | 保留现名 | 当前标签有各档计算合同，不能凭名称改成普通收益或统一成一个target | F；标签、校准、验证及模型报告 |
| 样本特征 | f0/f1等位置列、V1 features字节 | 按本档固定特征合同使用具名列 | A包从真实特征ID生成逐项映射；保留顺序、dtype、缺失和模型身份，不臆造简写 | F；样本codec、矩阵构建、V1/V2/V3/V5双头拟合 |
| V5序列块 | block、payload | block_id、sequence_bytes | 明确块键及序列字节；保留sequence_start及重叠编码 | F；序列样本写入、读取、矩阵和释放 |
| 收益 | stock_code、recommend_date、strategy、snapshot_id、horizon | code、freeze_date、strategy、snapshot_id、horizon | 去掉多余股票前缀、明确推荐决策日期；不把snapshot_id猜成freeze_id | C；在线结算、训练研究及只读查询 |
| 追踪 | 原冻结版本、payload、payload_hash | freeze_id、event_bytes、event_hash | 保存新freeze事件字节及身份，旧记录不导入；schema_version、freeze_hash保留 | C；追踪写入、隔离、研究及诊断 |

新建参考表用第2.4节的短字段；冻结表用第4节字段，不能继续沿用旧候选长名称。表自身主键id、表内明确的status、reason、source可保留；跨表引用才需要risk_id、positive_id、evidence_id等限定。金额的人民币元、成交量的股数及正负号必须逐字段写清，不能仅因名称缩短而改变单位。

### 3.4 原子切换与验收

每个改名清单必须关联当前owner、目标owner、全部读写消费者、公开投影及必删路径。实现时同批修改建表、SQL、行解码、类型化映射、索引、外键、测试、诊断和配置；删除旧定义、旧列名和兼容分支，不保留双列、双写或隐藏fallback。已发布旧记录保留原身份，按第9节隔离旧数据而非原地改库。

每次字段改名同时核验类型、NULL、单位、精度、日期、枚举及摘要；固定同一输入和模型验证特征、预测、评分、动作和排名不变。命名验收检查“简短、易懂、专业、同义唯一”，性能验收独立计量，改名本身不算提速。

## 4. freeze.sqlite3的边界与切换

### 4.1 保存什么

每档只有一个`recommendation/{profile}/freeze.sqlite3`。`freeze_record`至少明确以下身份和校验信息，内部类型以实际领域对象为准：

| 字段 | 含义及约束 |
| --- | --- |
| strategy | 只允许tomorrow、d25；与trade_date组成正式记录唯一键 |
| trade_date | INTEGER业务日期；不能用提交时刻推断交易日 |
| freeze_id | 原领域决策身份，唯一，不按新库路径重算其业务意义 |
| freeze_hash | 正式冻结记录的逻辑内容摘要，与包含版本/摘要字段的整条规范字节校验区分 |
| record_hash | record_bytes的SHA-256，32字节BLOB |
| record_bytes | 正式决策的规范字节及既有审计，不拆成第二份可修改的评分事实 |
| committed_at | UTC微秒，表示持久提交时刻 |
| commit_kind | 既有正式封口或合法close_fallback类别，不新增业务类别 |
| status | committed/quarantined等既有状态的明确编码 |
| failure_code | 校验或恢复失败类别，正常记录为NULL |

库级合同为freeze_record/2，通过`schema_contract`和PRAGMA user_version=2校验。公开协议、事件及规范字段统一freeze，不保留旧字段别名；新字段生成新摘要，旧格式拒绝读取和导入。正式记录的大小上限、摘要校验、幂等和损坏隔离规则保持；同日重复同内容可幂等，异内容必须拒绝。

### 4.2 全部消费者一次切换

同时切换DataPaths、组合根、冻结端口和adapter、收盘恢复、历史查询、结算、追踪、研究只读、CLI、迁移/诊断及对应测试。搜索旧文件名和旧SQL后，删除旧活动路径；仅使用新`freeze.sqlite3`，不保留旧库读取、软链接、自动rename或别名。

发布序号独立保留在`publication_state.sqlite3`，沿用现有预留块语义：先持久化预留，再发放内存坐标；崩溃可以跳号，不能复用。此次不新增跨库原子提交承诺，也不把未使用的data-plane参考表继续带入新库。

新旧数据不混读。无旧记录迁入不意味着允许改写或重新解释旧正式记录；重建后正式推荐从第9节限定的新交易日起开始，Long和V5权限保持原合同。

## 5. DeepSeek改为配置加运行临时文件

### 5.1 文件分工

| 位置 | 保存内容 | 生命周期 |
| --- | --- | --- |
| config/deepseek.json | enabled、模型/超时/重试、预算上限、桶和阶段限制、熔断策略等现有固定参数 | 人工配置，正常重启加载；不保存used_count，不回写运行计数 |
| 共享类型化内存对象 | 当前预算、在途预留、桶/阶段用量、熔断及只读状态快照 | 同一服务进程四档共用一个实例；status读取这里 |
| runtime_dir/deepseek/state.json | 当天请求预留和结算状态、必要批次摘要及有界熔断恢复窗口 | 本地临时文件，正常停止/重启不删除；只保留恢复所需的小规模状态 |
| data_dir/.deepseek.lock | 同一数据根的DeepSeek运行所有权锁 | 复用现有ProcessLock；锁文件不存预算，不替换锁文件本身；与runtime_dir中的服务锁职责分开 |

不再创建DeepSeek SQLite、表、索引、SQLite专用schema迁移或连接。原调用审计按必要内容进入既有正式决策/追踪边界，不为了保留旧数据库全部列重做文件归档系统。完整prompt、响应、正文、凭据和无限增长历史不得进入state.json。

### 5.2 状态字段与容量

JSON是持久化边界，加载后必须转换为类型对象；业务内部不能直接操作任意dict。临时文件包含以下具名信息：

| 字段或集合 | 用途 |
| --- | --- |
| schema_version | 严格识别临时文件格式；不支持时停用外部调用，不猜旧格式 |
| budget_date | Asia/Shanghai自然日；明确它是计费日，不拿市场交易日替代 |
| budget_policy_hash | 当前限额/桶/阶段策略身份；当天调整配置不清空已消耗用量 |
| revision | 单写owner的状态修订号，用于拒绝迟到完成覆盖新状态 |
| requests | 当天最多168条成功预留；每条有request_id、档位/策略、桶、阶段、预留/完成时刻及状态 |
| review_batches | 关联已发生物理请求的有界批次和应用计数；无HTTP的重复skipped不无限追加 |
| health_state | 既有熔断模式、截止时刻、半开probe身份及有限恢复窗口 |

桶和阶段计数由请求集合计算或在同一内存事务更新后验证一致，不分别写多个文件形成双状态。请求记录不因超时、失败或进程退出而从当天用量删除；内存纯命中不消耗物理请求预算。磁盘状态序列化上限4 MiB，超限视为不可写，不裁剪当天已计费请求来腾额度；跨日清理上一日不再需要的明细，保留迟到请求关联直至收尾。

### 5.3 预留、HTTP与崩溃顺序

1. 组合根创建一套共享状态服务。按解析后的data_dir定位进程锁，覆盖owner生命周期，线程锁串行化四档预留与完成；不同runtime_dir共用数据根也不能同时启用两份DeepSeek。获取不到锁时只停用该服务的外部复核，本地荐股继续。取得锁后仍需核验临时文件，换目录不能被当成新日空预算。
2. 每次物理HTTP尝试前，检查全局168、桶、阶段和熔断门。预留先更新候选状态，再使用既有atomic_files写同目录临时文件、同步并原子替换state.json；成功后才允许发送请求。在原子文件primitive中补足POSIX替换后的目录同步和定向回归，Windows核验既有替换/同步能力，不把原子可见性等同于断电耐久性。不能证明预留已可靠写入时不发送HTTP。
3. HTTP执行期间不持线程互斥锁；并发结果按request_id更新，不能以旧快照覆盖新预留。
4. 每次重试或schema修复前重新预留；完成时只追加结果类别及必要统计，不减少已使用额度。预留成功后尚未发出就崩溃，也保守计入当天用量，不通过重启退款。
5. 正常重启读取state.json，未完成预留标记为abandoned但仍计入原预算日；批次与熔断恢复沿用既有语义。
6. 23:59发出的请求完成时更新原budget_date；跨日时旧在途请求与新日预算按身份区分，不归零后错记，也不允许迟到结果影响已冻结名单。
7. 临时文件缺失、损坏、版本不支持或不可写，且不能证明当天未调用时，当日暂停新DeepSeek HTTP；本地评分、冻结及只读Web继续。首次合法空状态由显式初始化建立；丢文件本身不是“额度为0”。无法核验的当天状态只在下一上海自然日按合法新日初始化恢复，不人工清零补额度。
8. DeepSeek关闭时不加载其凭据，不创建数据库、不消耗物理请求预算；必要运行状态可以保持未初始化。状态读取失败不能触发网络修复。

临时文件不是永久数据库，也不是要提交Git的配置。它可以随运行目录清理，但清理会丢失当天计数证据，因此不能约定每次启动/退出都自动删除。全量重建时的日界与初始化流程见第9节。

### 5.4 必须删除和验证的旧实现

切换预算、批次、健康门、结果统计、状态投影和组合根所有消费者；删除DeepSeek专用SQL、连接回调、建表、预算库路径、存储介质检测项及诊断对旧数据库的依赖。保留既有review事实缓存和single-flight的唯一owner，不把缓存再复制成一套文件存储。

为新文件adapter补最小充分回归：168并发上限、桶/阶段隔离、失败和重试计数、跨档共享、同数据根不同runtime_dir的进程互斥、正常重启、预留后崩溃、丢失/损坏/写入失败、跨日迟到完成、熔断半开及DeepSeek关闭。测试使用临时目录和固定时钟，不消耗真实模型额度。

## 6. 读写和计算的性能改造

### 6.1 下载与写入

- 保留有界下载worker和唯一写入协调者；网络、PDF解析不持SQLite写事务。
- 恢复、存在性和缺口规划使用批量查询，避免逐证券逐日探测；同日已合格数据不重复请求和更新。
- 批量写入校验事实，持久业务库继续FULL；训练可重建临时样本才使用原合同允许的低同步模式。
- 价格落库成功后再确认catalog断点。跨库失败可留下不可见的未发布事实，不能提前确认进度。
- 内容不变不更新发布时间；参考单独改变可以复用价格，不触发全段重下。
- 短窗raw/qfq及档位绑定、完成状态保持同库原子发布，不拆成先完成一侧再对外可见。
- 同一输入比较批量大小与关闭/checkpoint总成本，不把延后写盘移出计时取得虚假收益。

### 6.2 历史读取、索引与校验

| 工作负载 | 读取范围 | 优化方向 |
| --- | --- | --- |
| 下载恢复 | 请求证券、日期及序号 | 批量请求集合关联；证券/日期/序号索引，避免已有整股无数据时逐日探测 |
| 荐股启动 | 合格总体最大必要窗口及少量合法向前扩展 | 证券/日期窗口索引；合并扩展请求，按年复用连接 |
| 训练/研究 | 固定快照的日期范围及实际使用列 | 日期/证券顺序扫描；条件下推，不先读全表再Python过滤 |

价格修订及观察关系保持。点查、窗口和大扫描分别写SQL，通过EXPLAIN QUERY PLAN及实际计时比较；避免全库ROW_NUMBER排序，防止相关子查询在未命中索引时逐行退化。只保留服务于真实访问模式的索引，不为每列建索引。

请求集合使用有界批次或临时表，兼容SQLite参数上限。连接由单线程持有、取消可中断、任务结束关闭；缓存是按进程聚合预算，不能每个年库连接都无限增加页缓存。

同一进程的同文件和同逻辑身份复用校验；文件、WAL或发布身份改变后失效。合并校验与取数的重复读取，不跳过内容、修订序号、覆盖、摘要和损坏检查。当前整年文件顺序预读必须单独对照，有总收益才保留；不默认所有启动都读取全部历史。

### 6.3 训练样本与数组

保留已有一次公共历史扫描、每档独立训练和有限队列，不另建共享扫描owner。分别记录读取、对齐、特征、样本写入、样本读取和模型拟合耗时。

公共日历对齐及相同基础量只处理一次，不同档位的窗口、标签、行业合同及模型保持独立。样本具名列按真实使用方式建立日期分区和行业索引；批量写入、按需要列批量读取，避免反复把整批SQLite行转换为Python对象再复制为数组。

矩阵按已知尺寸分配，实际大矩阵分配服从共享预约，超限使用受控文件映射；结束、取消及单档失败必须释放。V5保留既有重叠序列块编码及浮点精度，不为“字段清楚”复制360份重复序列值。失败/部分样本不能冒充完整缓存，日期和特征身份不符明确拒绝复用。

不改变浮点归约顺序、模型精度、训练样本范围或拟合参数取得表面提速；净效用零点、动作阈值及ROUND_HALF_UP附近必须检查一致性。

### 6.4 Parquet/Polars采用规则

1. 从已经发布的SQLite快照生成真正的具名类型列；不把整行JSON塞成单字符串列。只导出快照允许的可见修订。
2. 按逻辑月份生成不可变文件，按trade_date、code排序，初始Zstd低等级压缩，row group约65,536行。文件包含保持原口径的raw/qfq字段、日期和必要状态，复用第3节的单位和空值合同。
3. 每个月文件绑定逻辑内容、格式版本、行数和摘要；manifest绑定来源快照及全部月引用。未变化月份复用文件，不每次复制2000日。
4. 在临时路径写入、同步并验证后原子发布manifest；失败保留旧发布，不在下载事务内双写两份事实。SQLite是唯一可修订事实，列式文件只由它重建。
5. Polars按日期和列裁剪读取，有界批次输出到现有训练对齐和特征owner；顺序、dtype和缺失处理与SQLite对照完全一致。
6. 分别测已有文件的重复准备、首次生成加准备，以及一次增量导出加准备，不只测最有利的读取片段。
7. 五组交替对照中，重复训练准备中位耗时至少降低15%、至少4组获胜；首次生成和增量更新加准备的耗时各自回退不超过5%，并通过输入一致性及训练内存门，才接入离线批量读取。
8. 通过后，训练及对应离线大扫描只接入一个列式adapter，不设置损坏后静默读SQLite的fallback；列式缺失或无效明确待重建。在线有界历史查询仍使用原SQLite事实owner，是不同访问用例。
9. 未通过则删除候选活动接线，继续使用优化后的SQLite，不维护两个生产读取分支。对照实验代码只保留在既有诊断职责内，不作为第二事实owner。

Polars已在pyproject.toml依赖中；本轮无需新增分析引擎。技术能力参考[Polars Parquet文档](https://docs.pola.rs/user-guide/io/parquet/)和[SQLite查询规划](https://sqlite.org/queryplanner.html)，具体收益由本机同负载对照证明。

### 6.5 荐股启动与连续运行

公共参考和公司正式事实各批量读取一次，所选档位共用同一个启动投影owner和预读线程。按最大必要窗口扫描一次，同长度不可变摘要共享；每档模型输入、质量和冻结身份独立。

停牌或估值所需的向前扩展按证券合并，避免每加一段窗口都从头读取。启动后常规评分和报价展示不重新打开离线参考、公司或历史库；显式证据详情、历史查询和结算属于单独计量用例。

仅实际依赖变化重算基础量，保持现有缓存的来源年龄、人口、交易日、配置及证据失效规则。相同事实的接收时间变化不必重复算术计算，但仍需重新判断时效。不得通过共享结果绕过本档成本门或风险门。

冻结与发布保持短事务。正式记录保存一份规范字节及校验信息，不再保存一份可独立写入的完整分数副本。删除空置data-plane的初始化、恢复和SQL，发布序号不能随简化被降为仅内存值。

### 6.6 阶段281尚未闭合的历史启动

通过scripts/diagnose_runtime.py及其既有子模块测量完整历史预加载，将读取、分区证明、行解码、共享派生计算及缓存提交耗时分开。

待验证的优化包括：使用匹配代码/日期的索引避免临时排序；同一有效发布序列的年度分区流式读取；月度证明不重复读取相同年度内容；发布目录缓存必须绑定数据库及WAL变化身份，更新后立即失效。不能跳过内容证明、混用不同发布序列或泄漏未发布修订。

以实际新进程完整加载4401股及V1/V2/V3所需窗口为验收对象，确认在既有300秒预算内完成，且没有history_preload_deadline反复刷新。若失败，继续定位未解决耗时；HTTP 200、Long可显示或少量股票fixture不能算启动通过。

## 7. 资源预算和验证门

### 7.1 10 GiB内存的使用

| 范围 | 默认安排 |
| --- | --- |
| 推荐 | 维持共享512 MiB逻辑缓存、2 GiB进程RSS验收线，多个档位不能各领一份 |
| 训练 | 保留既有约5 GiB整体scope及共享拟合预约；按实际可用内存保留至少1.5 GiB余量 |
| 执行器 | 按所选档位数创建，不新增固定只准两档的限制 |
| 计算线程 | V1/V2/V3沿用每档2线程，V5沿用已配置线程；用四档实测核对原生库线程叠加，不无界加线程 |
| SQLite缓存 | 按全任务连接数聚合计量，不能每股、每连接盲目扩大cache_size或mmap |
| 基准测量 | 下载、训练、荐股重型对照分别运行，避免互相争用；物理I/O测试不放tmpfs |
| DeepSeek临时状态 | 一个共享内存实例、小规模原子JSON，不扩展为通用文件数据库 |

增加内存优先减少争用、给操作系统缓存留余量，不自动提高推荐验收线。若实际资源不足10 GiB，在报告中标明；不能用名义10 GiB证明通过。相同数据量下的持续增长仍受20%门限制。

### 7.2 正确性场景

| 类别 | 必须覆盖 |
| --- | --- |
| 历史 | 首建、次日、隔周、同日重跑、首次未发布续传、参考变化、上市/退市及日历边界 |
| 参考合并 | 第2.4节的ST续传、永久排除、版本去重、原子发布、新旧版本隔离及catalog绑定；旧参考文件不再被访问 |
| 价格 | raw/qfq单侧缺失、非正qfq、停牌推定、有效修订、冲突、未发布行隔离 |
| 恢复 | 取消、提交失败、年库已写但catalog未确认、资源退出、重启和旧格式拒绝 |
| 字段 | 日期/微秒时刻往返、pct/ratio换算、浮点精度、枚举固定编号、NULL与0区分、摘要编码 |
| 公司 | 第11–14节要求的完整覆盖、证据、事件状态、非ST排除、待核实和正式库轻量预读 |
| 模型 | 同固定输入及同模型下特征、预测、分数、动作、排名、净效用门及双头身份一致 |
| 冻结 | 15:00、first-wins、同内容幂等、异内容拒绝、迟到、正式命中、合法close_fallback、损坏隔离 |
| 序号/SSE | 重启不复用坐标，丢失旧运行历史后旧游标触发resync，不接着发送伪连续补丁 |
| DeepSeek | 第5节并发、预算、重试、崩溃、跨日和临时文件失败矩阵；不触发真实API |
| 边界 | Long只展示，V5权限不变；HTTP/Web无业务写入；CreateApp无I/O/线程副作用 |

重新下载可能得到供应商修订，重新拟合也会产生新工件；新旧真实数据/模型不同不用于证明代码等价。等价测试必须固定同一输入与同一模型，真实重建另验覆盖和身份。

### 7.3 性能通过标准

| 目标 | 通过条件 |
| --- | --- |
| 数据库性能改造 | 目标工作负载5组新旧交替，中位墙钟至少减少10%、至少4组获胜；其他既有负载回退不超过5% |
| 仅命名的切换 | 正确性完整通过，既有负载回退不超过5%；不要求凭改名产生10%收益，也不将其记成提速 |
| 历史启动 | 完整合格总体、所选档位保持原300秒deadline并成功加载；281先验收V1/V2/V3，G包补所选四档，不能靠提高deadline通过 |
| 连续荐股 | 同输入不新增离线输入库连接/读取SQL；真实评分仍须满足既有P95、来源年龄和发布预算 |
| 训练 | 公共主扫描一次；按阶段及整轮报告时间，合法重复运行正确复用；拟合资格不因提速放宽 |
| 列式接入 | 严格按第6.4节的15%、4/5、首次及增量总成本门决定 |
| 内存 | 推荐峰值RSS不超过2 GiB，100 tick增长不超过20%；训练遵守既有scope及预约 |
| DeepSeek文件 | 跨档并发及恢复语义通过，状态接口零额外文件读取；记录预留/写文件耗时，不承诺换文件天然更快 |
| 磁盘体积 | 记录但不作为本轮性能通过条件；现有诊断的空间增长门需同步移除本轮依赖，不能仍暗中决定accepted |

新增数据库性能门在现有config/performance.json及其类型化解析中维护，诊断读取同一来源；不能在多个脚本重复硬编码。现有其他延迟、内存和回归门不随本轮改写放宽。

每轮记录源码、依赖、环境、输入、模型、配置和发布身份；记录连接、SQL、事务、批量行数、物理读写、WAL、RSS、可用内存、换页及中位数。新进程不等于物理冷缓存；新进程、预热缓存、真实网络分别说明，不在活动机全局清页缓存。SQL累计时间不能相加当墙钟，HTTP 200不能当链路通过。

### 7.4 公司专项验收

#### 结构与语义

- 正式库只有约定的12张业务表；无原始公告、正文、title、url、summary、JSON事件数组和续传表。
- 逐表校验列类型、NULL、枚举范围、外键、唯一键、证据顺序、字段单位、时间精度及摘要长度；未知编号拒绝。
- 风险解除、撤销、减持尾期、持续风险、利好首次达标及交易日窗口保持原规则；按类型恢复的摘要不影响数值评分。
- 未知财务、未完成正文、初始风险状态不明、解禁前视覆盖不足均保留明确待补，不因清空重建自动变成合格。
- ST只来自参考唯一owner，切换前后路径见第14.1节；非ST排除完整传递，旧eligibility活动引用清零；旧冻结记录不重写。

#### 发布与故障

- 提取中取消、事务中异常、正式提交后raw清理前崩溃均不会出现半发布或丢失待处理任务。
- 真实多批处理记录临时占用峰值，验证下载→提取→发布→清理循环；多股公告不会提前删除，失败文件不无限累积，低磁盘时停止新下载，已提交风险及待核实项不会因删除正文丢失。
- 并发原始输入改变、并发永久排除写入及提取身份改变会触发冲突处理；不覆盖新事实。
- 正式库缺失、损坏或身份不匹配时显式降级；raw暂不可读时正式启动和评分仍可使用有效快照，证据详情独立报错。
- PRAGMA integrity_check与foreign_key_check通过；引用证据无悬空，未引用证据可回收，原始持续风险资料不会误删。

#### 性能与真实运行

- 记录4401股正式加载耗时、峰值内存、SQL查询数及真实文件大小；证明未访问raw并只共享加载一次。
- 新同步的覆盖率、待补原因和失败分类真实可查；正文提取失败不能被统计成已完成。
- 真实`./run.sh v1,v2,v3`在既有历史预算内完成加载；检查状态和合法结果/降级行为，不把空荐股一概解释为故障或成功。
- 定向回归覆盖新schema、发布恢复、排除来源、评分语义和历史序列隔离；受影响Python运行Ruff，公共类型及复杂边界运行mypy。其余门禁按AGENTS.md风险选择，不将定向测试称作全量回归或发布验收。

交付报告必须区分已实现、隔离测试、真实同步、真实启动和未完成项。正式库体积估算、旧批次测试结果、模拟样本及HTTP成功均不能替代本次实测。阶段281必须在原台账完整闭合自身范围后才能标completed；新增E包命名不承接或替代281剩余项。

## 8. 分批实施与交付顺序

下表是工作包及依赖，不是活动进度记录。阶段281沿用原编号；其余独立新任务使用台账下一个可用阶段号，只登记当前工作包，不提前创建多条未完成阶段。每包完成一个原子切片、验证、单提交并推送后停止；用户要求继续时推进下一个完整包。

| 包 | 范围及消费者切换 | 出口 |
| --- | --- | --- |
| A 基线与映射 | 固定源码基线、盘点现存路径及代码定义库，补齐第12、13、15节映射；扩展既有诊断并适配公司新owner | 源码持久表及动态列有去向；五类历史负载、联合/单项同步与启动输入有五轮隔离基线；真实数据缺失项明确登记，不改活动数据 |
| B 参考合并、历史与短窗 | 按第2.4节完成company_st.sqlite3及catalog版本引用；保留清楚的现有文件名，按第3节调整表、字段和类型；全部下载、训练、推荐、结算及诊断消费者切换 | 参考原子发布与版本隔离、新格式fixture、修订/配对/断点通过；旧JSON、独立ST库和旧活动SQL路径删除，性能对照通过 |
| C 冻结与研究 | freeze.sqlite3、publication_state、V1目录、收益/追踪及研究表命名；全部消费者和公开只读投影接线 | 冻结/SSE/结算/序号回归通过；空置data-plane和退役检查点活动代码删除 |
| D DeepSeek文件 | 共享预算/批次/熔断服务改为类型对象和临时JSON；配置、组合根、状态及诊断原子切换 | 第5节矩阵通过；旧DeepSeek数据库创建/访问/路径全部删除；本地降级保持 |
| E 公司字段命名 | 沿用281已有关系表实现，应用第12、13节字段，切换SQL、codec、证据查询及诊断，核对CLI、共享预读和测试消费者；参考沿用B包company_st.sqlite3 | 无第二公司owner、不重做采集、不转移281债务；新schema及全部消费者同批切换，字段语义、覆盖和共享预读无回归 |
| F 样本与列式 | 样本具名列、批量数组和资源释放；按第6.4节做列式对照及唯一接线 | 四档双头固定输入一致；满足门才接入Parquet，失败则删除候选活动分支 |
| G 全链演练 | 在仓库外新目录做空库下载、训练、重跑、中断恢复、启动和真实专项准备 | 完整门禁、性能证据、清理清单和切换命令形成可检查候选；不自动切换活动数据 |
| H 活动重建 | 依据第9节执行已确认范围内的清空、全量下载、四档训练和新日起运行 | 数据、模型、推荐分别验收；失败保留明确待补，不伪造通过或回填旧记录 |

B、C、D各包内必须同时切换新owner、全部消费者并删除旧活动路径；不能只提交schema后留消费者读旧库。中间代码提交不代表允许旧活动数据被新代码打开，最终活动切换前沿用完整旧运行版本。

公司阶段281及历史启动改动已有原负责人，不能接管、重编号或复制未交付内容。281先完成以下原任务：复核失败恢复与诊断白名单，定位历史解码/重复校验/补长读取，完成4401股公司规模及提取准确性测量，按第9.1节重建公司，再实跑三档确认300秒内完成启动。阶段281的21文件310项旧测试不能替代这些真实验收。

续作先检查company_facts_schema/protocol、company_fact_rows、company_facts_index与reader，确认旧JSON发布codec和独立eligibility活动实现已经删除。检查company_evidence_index、company_pipeline_queries及正文文件生命周期只读取采集工作状态，正式发布身份不从raw获取；组合根显式注入正式库、采集库及正文路径。消费者范围包括同步与新闻采集人口、发布版本、风险/利好编译、PublishedCompanyResearch、详情证据、永久排除、CLI/健康状态、统一诊断、数据布局/离线工具和相关回归。

A先完成当前基线及命名清单，B/C/D/F各自依赖A；E依赖281已有关系表代码及A的映射，按用户指定独立完成字段切换；参考接线沿用B，281真实重建及性能验收仍由原阶段完成。G依赖B/C/D/E/F，H依赖G及对应数据切换授权。A/B/C/D/F的独立工作不因281未完成而混改其记录；共享文件先明确各自改动范围。

用户已自行删除data内历史数据及模型，无法再获取原活动库、真实模型启动、训练和连续评分的原样基线。A保留可复现的固定fixture和源码身份，明确测量范围；后续每个性能实现包仍须使用同输入的新旧源码交替对照。G使用同一份新下载的隔离输入补全真实训练、模型与评分测量，H另验活动重建；缺少旧真实数据的比较明确记为不可获得，不能拿合成结果补签真实验收。阶段281的全市场公司/启动未完成项继续保留原归属。

现有用户配置和模型修改不得混入本批代码/文档提交，实际文件范围以交付时git status为准；重训模型作为重建结果按各档双头完整工件另批交付。

每包先核对Afuture及上游、共享工作树和暂存区，只暂存自己的文件/改动。Python命令使用python3或.venv/bin/python3，手工修改使用apply_patch，不amend、不改写分支历史。

## 9. 清空重下、重训与新日起运行

### 9.1 阶段281公司专项重建

用户已明确要求删除之前company下载的数据后全部重跑。这是阶段281已有范围的后续执行步骤，只清理受管company文件，保留market/reference、行情、模型及冻结记录。合并本文不扩大授权，也不表示已经删除或重建；后续全库目标若改变schema，在H包按最终格式另验重建，不能复用旧格式。

以上是原公司专项的授权范围。用户随后自行清空整个data后，旧参考、行情和模型的保留前提已不成立；续作必须重新核对前置输入，缺失就记待重建，不执行重复删除、不从Git恢复旧模型，也不能直接用公司单项同步代替全库重建。

1. 先完成隔离验证并列出受管理文件的明确清单，检查真实解析路径及符号链接，避免清理扩散到其它数据目录。
2. 正常停止当前同步和持有company文件的推荐进程，等待线程、事务和文件锁释放；核实不存在仍在写入的句柄。保留市场、训练和冻结推荐数据。
3. 删除data/company内旧company_facts.sqlite3、evidence.sqlite3、eligibility.sqlite3及对应WAL、SHM、journal、已确认的公司专属旧锁和检查点；原有公司raw采集状态如存在也从零重建。未知用户文件先列明，不以无边界目录删除代替清单。
4. 使用阶段281已验收实现初始化company_facts.sqlite3和raw/company_collection.sqlite3；不导入旧JSON事实、公告、正文、游标、重试或eligibility记录。
5. 执行`./run.sh sync company`。既有market/reference提供证券、ST等参考人口；若参考缺失或过期，按现有参考维护合同报告或维护，不复制ST进company。
6. 采集任务不设整轮人为超时；请求、PDF解析、重试和取消仍必须有界。发生中断时只从本次新库形成的新断点恢复。
7. 输出实际处理股票数、各组件完整/部分/不可用数量、待补正文及原因、发布身份、文件大小。不能用命令退出0或存在4401行代替覆盖验证。
8. 完成同步后正常启动`./run.sh v1,v2,v3`，验证使用的是新进程和新正式发布身份，三个档位共享公司加载，最近有效结果及降级状态可读。

### 9.2 全库目标重建与新日起运行

用户已选择不迁入旧业务数据并自行清空data。代码实现和隔离演练先完成；后续活动数据切换与发布按AGENTS.md核对授权范围，已获同范围明确授权的动作不重复询问，未覆盖的动作在可检查结果形成后再取得授权。已缺失的旧库、模型和回退副本如实记录，不伪造备份，也不以清空状态跳过实现与隔离门禁。

1. 固定旧版源码、配置合同、最后正式交易日和DeepSeek最后物理调用的上海日期，用于新日边界核验；数据已删除而日期无法核实的，登记未知并按第5节关闭当日外部复核，不把未知当成从未调用。实验输出只放显式仓库外目录，物理I/O测量禁止tmpfs/ramfs；以报告实际文件系统为准，不能仅凭/tmp目录名判定内存盘。
2. 在新仓库外工作区验证全部新schema、字段映射、原子文件及完整流程；真实供应商与合成fixture报告分开，先闭合G包。
3. 形成精确删除清单：旧SQLite及WAL/SHM/journal、inputs.json、historical_st.sqlite3及旧ST锁、其他离线参考快照、样本、模型、推荐/研究数据和DeepSeek临时状态；新目标不存在、软链接和路径归属须核实。配置、凭据、代码、无关用户文件不进入清单。配置来源的人工黑名单等业务输入不能因清库丢失。
4. 选定维护窗口，停止sync、train、服务及所有writer，等待退出并确认句柄释放。保留符合回退要求的完整旧release及匹配数据副本，副本不被新程序读取；不在活动库现场改表。旧活动数据随后按清单删除，不做历史导入。
5. 使用新代码的唯一组合根初始化新格式。DeepSeek固定配置保留，运行状态从临时文件重新建立；切换当日保持无外部复核，至少到旧进程最后一次物理调用的下一上海自然日才能建立合法空预算并启用，不能通过清库补回当天额度。
6. 执行`./run.sh sync`。公共准备从空company_st.sqlite3重新采集并发布，核验ST指针、不可变版本、成员及当前发布指针，不创建旧参考文件。参考、history、market、company分别核对覆盖、缺口、绑定及发布。按明确失败项运行`./run.sh sync history`、`./run.sh sync market`或`./run.sh sync company`续传；不因一项成功将整轮标成功。
7. 执行`./run.sh train v1,v2,v3,v5`。先维护一次历史，再四档独立训练；每档Tomorrow/D25整组工件完整校验。失败档明确不可用，不能复制其他档模型、制造零预测或以旧模型替代本次重训。
8. 正常启动并核验实际导入的新代码、数据及双头模型身份；慢资料在重启加载，不新增轮询或热更新模型机制。
9. 新正式推荐的首个交易日必须晚于被删除记录的最后正式交易日，并不早于活动切换后的下一可靠市场交易日。实施时在运行身份中保存显式启用日并在所有正式commit/close_fallback入口统一校验；启用日前只允许合法预览，不向新freeze库写正式记录。此日期只用于一次性重建保护，不改变常规15:00规则。
10. 清空后的旧推荐、逐日追踪和研究历史不恢复、不回填；旧SSE游标触发全量resync。新结果按新数据/模型身份生成，不能拿重算结果冒充曾经发布的历史。
11. 验收正常热运行、午间冷启动、15:00边界、收盘后和正式记录恢复五时段。无合法模型、证据或价格时保持明确待补，不能为页面凑六席。
12. 成功后记录新运行身份、覆盖、待补及真实验证结论；回退只允许完整旧release加匹配旧数据副本，不能让新数据写回旧库。

重下资料不保证恢复历史披露时点、分钟证据或旧市场人口。当前清洁数据、模型训练成功和性能通过均不自动授予收益资格或发布权限；V5仍遵守既有独立权限合同。

## 10. 验证入口、文档和最终交付

### 10.1 复用现有诊断

所有性能、恢复和状态探测继续从`scripts/diagnose_runtime.py`进入，在所属runtime_diagnostics模块实现，不新建平行工具。A包先适配现有公司schema和新路径，不能拿不再匹配代码的旧fixture结果作为基线。

数据库基线示例，`/absolute/external/...`必须替换为仓库外真实文件系统的新目录：

```bash
.venv/bin/python3 scripts/diagnose_runtime.py --profile database-baseline \
  --database-probe-directory /absolute/external/database-probe \
  --download-sizes 8 --history-days 2000 --database-rounds 5 \
  --command-timeout-seconds 900 --output /absolute/external/database-baseline.json
```

该profile的8股合成工作量只证明持久化/恢复行为和局部成本，不代表全市场。沿用现有history-write、history-sqlite、download-performance、training-performance、performance和runtime入口补充实际年库、副本、真实模型及完整总体测量。更大工作量显式设置合理诊断超时，不能拿诊断上限替代业务deadline。

新增DeepSeek文件probe也挂接既有DeepSeek/运行诊断职责，默认只读汇总，不启动真实HTTP，不输出股票级载荷、文件内容或凭据。记录静态参数、临时状态日期/可用性、预算用量和降级原因即可。

### 10.2 门禁

每个实现切片运行受影响Ruff、复杂类型边界的mypy和必要的定向回归，集中Review完整diff及新增/删除路径，检查旧引用、重复owner、TODO、生成物和越界修改。文档批次只做完整Review、链接和git diff --check，不新增业务测试。

完整候选与发布门禁遵守AGENTS.md第6节：格式检查、格式化及无关脚本不能自动运行，即使被Make、测试、hook或skill间接依赖也不执行。先检查入口实际调用范围，拆出与本批直接相关的正确性、类型及功能检查；不可拆分的入口跳过并记录，不能宣称完整发布门禁通过。

发布另按工程设计及用户授权核验打包、仓库外wheel安装、只读API/SSE、真实浏览器、五时段、取消/恢复及资源；不因普通“继续”自动启动这些任务。未运行、fixture、真实供应商、真实全市场和收益验证各自明确标记；快速或定向测试不冒充完整回归。

### 10.3 文档收口

| 文档 | 行为实际切换时的更新 |
| --- | --- |
| 4_下载训练.md | company_st.sqlite3合并、版本引用和原子发布、新路径、唯一owner、raw/qfq、快照、短窗、样本、列式采用结论及完整字段字典 |
| 2_工程设计.md | freeze.sqlite3、发布序号、DeepSeek配置/临时状态、故障降级、资源及启动/重建验收 |
| plan_sqlite.md | 合并后的唯一方案入口；第0节保留本次固定基线摘要，第12、13节保留原字段与现字段映射，不另建活动台账 |
| 1_评分逻辑.md | 仅在说明存储映射时补必要引用；模型特征ID、评分字段及业务规则不随数据库改名 |
| 3_工程实施.md | 唯一活动状态、实际改动、验证和未完成项；本文只引用固定基线，不维护滚动勾选台账 |

数据库字典必须按实际文件、实际表列举，每个字段一行，格式固定为：

| 字段 | SQLite类型 | 可空 | 含义 | 链条第几层，起什么作用 |
| --- | --- | --- | --- | --- |
| code | TEXT | 否 | 六位证券代码，保留前导零 | 按该表实际消费者说明：关联资格、历史窗口、样本或冻结记录 |

示例行只说明格式，正式字典必须按各表真实用途写清楚。链条层号对应评分文档现行十一层；下载控制、训练准备、收盘冻结、收益结算等不属于十一层的字段应写真实阶段，不能硬编层号。不可将多个字段、通配符或整组JSON中的业务事实合成一行。

DeepSeek文件化切换后不再维护其SQLite字段表，工程设计中改列静态参数、临时状态字段及恢复语义；下载训练只保留路径和引用。旧冻结库、inputs.json、historical_st.sqlite3及DeepSeek数据库名称只允许作为明确的迁移/历史说明，不得留在活动配置、建库、消费或启动探测路径；此前拟议的security_st.sqlite3也不再是目标库。

### 10.4 最终可检查结果

交付包含全部活动库字段映射、company_st参考合并及不可变版本、唯一读写owner及消费者切换、旧路径删除、freeze库接线、DeepSeek无数据库实现、性能对照、正确性回归、空库重建证据及权威文档。仅命名改造不冒充提速，清库重训不冒充收益通过，计划文件不冒充已经实施。

<a id="company-protocol"></a>

## 11. 公司存储背景与协议

### 11.1 原问题与证据限制

此前只读核算的 company 目录约850 MiB，其中当前 company_facts.sqlite3 约631 MiB、退役 evidence.sqlite3 约219 MiB，另有约900 KiB的 eligibility.sqlite3。当时的旧主库混有公告目录、正文、分页成员、续传和正式发布投影，不能把全部空间除以4401理解成单条公司风险记录的体积。

此前样本中的公告目录约60.8万条，正文已检查约2.5万篇；原 published 投影本身约21.5 MiB，并有事件、解禁和待核实原因的JSON数组。仅清理完成分页或执行VACUUM，不能解决正式库职责和字段设计问题。

目标是提取可计算的事实，填入明确的类型字段。正文、标题、URL、供应商响应、失败详情和续传过程全部属于采集资料；正式库只保留数值、日期、状态、枚举及必要的短标识。未知值保持未知，不用0、空串或“没有风险”填满字段。

另一个已观测问题是 history_preload_deadline，边界 require_projection:362，历史预加载预算为300秒。它需要独立定位和实测，不能承诺拆公司库就自动修复，也不能通过放大deadline掩盖瓶颈。

以下关系与codec沿用阶段281实现，字段按第12、13节切换为schema 2；原阶段的真实验收继续在工程台账登记。存储列改名不改变领域对象、评分特征或HTTP/CLI字段。

### 11.2 类型、空值与时间

| 类型或约定 | 定义及作用 |
| --- | --- |
| INTEGER枚举 | 使用本文件固定编号，显式codec转换；不得依赖Enum声明顺序或字符串哈希 |
| INTEGER日期 | 自1970-01-01起的日数，使用上海业务日期；例如财报期、解禁日 |
| INTEGER时刻 | 自Unix epoch起的UTC微秒整数，精确保留微秒；读入后转带Asia/Shanghai时区的datetime |
| REAL | 只接受有限数值，拒绝NaN和Infinity；百分比使用百分数单位，1.5表示1.5%，不是0.015 |
| TEXT | 仅股票代码、稳定事件键、案号、来源记录键等必要标识；不得保存标题、摘要、正文、URL或JSON |
| BLOB(32字节) | SHA-256摘要的二进制表示；SQLite用BLOB及length=32约束，不存64字符十六进制文本 |
| NULL | 未知、缺失或对该事件不适用；只有明确证据证明为零时才写0 |
| 布尔值 | INTEGER 0/1；字段不适用或尚无判断时用NULL，不能拿NULL冒充false |

SQLite列必须有真实存储类型、NOT NULL及必要CHECK约束；启用外键。代码为六位ASCII数字TEXT，保留前导0。字符串标识由输入边界校验长度和格式，不携带任意原文。

财务的报告期和披露时刻必须同时存在或同时为空；质押比例和统计日期也必须成对存在。完整性状态与值分开保存：缺值不得标complete。

### 11.3 枚举编号

| 协议 | 固定编号 |
| --- | --- |
| data_state | 0 unavailable无可用数据；1 partial部分可用；2 complete满足该组件完整性合同 |
| risk_type | 1监管警示；2行政处罚；3纪律处分；4财务造假；5正式立案调查；6重大违法；7资金占用；8违规担保；9强制退市；10大股东减持 |
| subject_type | 1上市公司；2控股股东；3实际控制人；4大股东 |
| risk_level | 1正式警示；2一般处罚；3严重处罚；4硬风险事实；具体作用以评分权威为准 |
| risk_status | 1 active有效；2 resolved已解除；3 cancelled已撤销 |
| positive_type | 1股份回购；2股东增持；3经营合同 |
| positive_status | 1 planned计划；2 implementing实施中；3 completed已完成；4 cancelled已取消 |
| positive_level | 0尚不具备奖励资格；1/2/3为经证据核实达到的级别，实际奖励和封顶由评分规则计算 |
| repurchase_purpose | 0未知；1注销；2员工持股；3股权激励；4其它 |
| amount_basis | 0未知或不可比；1口径可比；非合同事件为NULL |
| review_scope | 1风险；2利好；3财务；4质押；5解禁 |
| evidence_source | 1 cninfo；2 sse；3 szse；4 bse；5 issuer_disclosure；6 exchange_disclosure；7 regulator_disclosure；8 court_disclosure；9 judicial_disclosure；10 csrc_disclosure |
| exclusion_reason | 1人工永久黑名单；2已核实的退市排除；不提供ST编号 |
| exclusion_source | 1 strategy_config；2 market_quote；3 exchange_security_master；新增来源必须扩展明确协议 |

来源编号表示实际采集和核验来源，不根据标题推断交易所，不把不同来源无损性未经确认地合并。当前CNInfo返回记录使用cninfo。source_key保留原供应商稳定记录键。

risk_level已经唯一确定severity_basis，正式库不重复保存后者；读出时严格按1→formal_warning、2→ordinary_penalty、3→serious_penalty、4→hard_risk_fact恢复。事件和待核实说明使用类型化字段生成固定中文展示模板，不在数据库重复保存说明文本。

### 11.4 待核实与不具备奖励资格原因

company_review.reason和company_positive_event.ineligible_reason共用下列编号。原始供应商错误详情保留在raw库；进入正式库时转换为稳定失败类别。未知业务原因拒绝发布，保留上一有效版本，不静默塞入“其它”或标为正常。

| 编号 | 原因 | 含义及程序用途 |
| --- | --- | --- |
| 1 | document_pending | 正文未完成提取，禁止仅凭标题形成确定结论 |
| 2 | official_source_unverified | 官方来源尚未核实 |
| 3 | subject_unverified | 涉及主体尚未唯一确认 |
| 4 | conclusion_unverified | 正式结论缺少可靠证据 |
| 5 | case_conclusion_ambiguous | 同案或肯否结论有歧义 |
| 6 | resolution_unlinked | 解除公告未关联原风险事件 |
| 7 | revision_pending | 更正内容待核实，保留已确认风险 |
| 8 | cancellation_unlinked | 利好取消公告未关联原事件 |
| 9 | progress_unlinked | 实施进展未关联原事件 |
| 10 | implementation_or_measurement_unverified | 实施状态或用于分级的数值未核实 |
| 11 | necessary_data_missing | 必需字段或组件数据缺失 |
| 12 | not_current | 数据不属于所需当前期 |
| 13 | source_stale | 数据超过允许年龄 |
| 14 | window_incomplete | 查询窗口覆盖不足 |
| 15 | initial_risk_state_unverified | 上市以来的初始持续风险状态尚无完整证明 |
| 16 | source_task_pending | 采集或解析任务尚未完成 |
| 17 | source_unavailable | 来源暂不可用 |
| 18 | access_denied | 来源拒绝访问 |
| 19 | rate_limited | 来源限流 |
| 20 | timeout | 有界请求或解析超时 |
| 21 | schema_invalid | 供应商结构或字段校验失败 |
| 22 | coverage_mismatch | 分页数量、窗口或范围证据不一致 |
| 23 | filter_ignored | 来源没有遵守指定证券或范围过滤 |
| 24 | source_changed | 同步期间来源身份变化，需要重建该批 |
| 25 | extraction_unsupported | 当前解析器无法可靠提取，维持待核实 |

同一scope、reason和evidence_id只保留一条当前待核实记录；没有evidence_id时仍需去重。detected_at保留该待核实事实第一次发现时刻，不能每次发布都刷新。供应商细分失败映射必须由实现列出穷尽规则并测试，不能直接把异常消息写入正式列。

<a id="company-schema"></a>

## 12. 公司正式库：原字段与现字段映射

正式库12张表使用schema 2（PRAGMA user_version和company_publication.schema_version均为2）。本节保留原列名供核对，现字段对应建表及全部SQL/codec；两列相同即保留，不新增兼容列。原schema 1及旧混合库拒绝读取，不现场改表或导入旧事实；新库初始化、隔离验证和真实重建的证据分别记入工程台账。约束、类型、单位、NULL、枚举、稳定事件键及确定性内容身份保持，列名由持久化边界映射到原领域属性。

下列每行对应一个字段。“否”表示NOT NULL；“是”表示允许NULL。表内不保存自由文本、JSON数组或原始公告列表。事件子表保留确有用途的多条事实，因此4401只股票对应4401条主记录，不意味着整个数据库只能有4401行。

### 12.1 company_facts：每只股票一条当前事实主记录

主键code。共24列。它提供启动加载和组件完整性判断，不承担公告归档或历史评分快照。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| code | code | TEXT | 否 | 六位股票代码；主键及各事实子表的归属键 |
| financial_report_date | report_date | INTEGER | 是 | 最新可用财报报告期；判断报告期和有效性 |
| financial_disclosed_at | disclosed_at | INTEGER | 是 | 财报首次可用披露时刻；防止使用决策时点之后的信息 |
| financial_state | finance_state | INTEGER | 否 | 财务组件data_state；区分缺失、部分及完整 |
| basic_eps | eps | REAL | 是 | 基本每股收益，元/股；财务与风险规则输入 |
| book_value_per_share | bps | REAL | 是 | 每股净资产，元/股；净资产相关判断 |
| revenue_growth_pct | revenue_growth_pct | REAL | 是 | 营收同比增长百分比；成长规则输入 |
| net_profit_growth_pct | profit_growth_pct | REAL | 是 | 归母净利润同比增长百分比；成长规则输入 |
| core_profit_growth_pct | core_profit_growth_pct | REAL | 是 | 扣非净利润同比增长百分比；成长规则输入 |
| roe_pct | roe_pct | REAL | 是 | 净资产收益率百分比；盈利质量规则输入 |
| parent_net_profit | parent_profit | REAL | 是 | 归母净利润，人民币元；盈亏及财务规则输入 |
| core_net_profit | core_profit | REAL | 是 | 扣非净利润，人民币元；主营盈利及财务规则输入 |
| pledge_ratio_pct | pledge_pct | REAL | 是 | 当前质押比例百分比，0—100；质押风险计算 |
| pledge_as_of | pledge_date | INTEGER | 是 | 质押统计日期；判断来源年龄 |
| pledge_state | pledge_state | INTEGER | 否 | 质押组件data_state；只有完整来源才能将明确无质押写成0 |
| unlock_checked_at | unlock_checked_at | INTEGER | 是 | 解禁范围最近完整核验时刻；用于时效判断 |
| unlock_coverage_end | unlock_end_date | INTEGER | 是 | 已核验解禁前视范围截止日；判断当前决策日的前视覆盖 |
| unlock_state | unlock_state | INTEGER | 否 | 解禁组件data_state；不能只因没有事件行就判完整 |
| pending_document_count | pending_docs | INTEGER | 否 | 待提取或待检查的相关正文数，非负；解释资料待补 |
| announcement_coverage_start | announcement_start_date | INTEGER | 是 | 本次事实使用的公告覆盖起日；供覆盖判断 |
| announcement_coverage_end | announcement_end_date | INTEGER | 是 | 本次事实使用的公告覆盖止日；供覆盖及年龄判断 |
| announcement_state | announcement_state | INTEGER | 否 | 公告风险组件data_state；包含初始风险和待核实约束 |
| checked_at | checked_at | INTEGER | 否 | 当前事实所依赖资料的核查时刻；展示来源年龄，不等于发布时刻 |
| published_at | published_at | INTEGER | 否 | 该股票事实发布时刻；内容不变时保留其原发布时间 |

### 12.2 company_risk_event：已识别风险事件

id为内部主键，UNIQUE(code,event_key)，code外键引用company_facts。风险是否在某个决策日有效仍由领域规则结合状态和时间判断，存储层不另存计算结论。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| id | id | INTEGER | 否 | 内部事件主键；供减持计划和证据关联引用 |
| code | code | TEXT | 否 | 股票代码；外键及按股读取索引 |
| event_key | event_key | TEXT | 否 | 原有稳定事件身份；跨发布关联同一事件，不以标题作为身份 |
| risk_type | risk_type | INTEGER | 否 | risk_type编号；选择相应风险规则 |
| subject_type | subject_type | INTEGER | 否 | subject_type编号；区分上市公司和相关股东主体 |
| risk_level | level | INTEGER | 否 | 1—4风险等级；与risk_type组合必须满足领域约束 |
| case_number | case_number | TEXT | 是 | 正式案号或可核验案件标识；用于同案关联及解除核验 |
| first_disclosed_at | first_disclosed_at | INTEGER | 否 | 首次正式披露时刻；确定事件最早可知时点 |
| last_disclosed_at | last_disclosed_at | INTEGER | 否 | 最近关联披露时刻；保留后续进展时点 |
| effective_at | effective_at | INTEGER | 否 | 事件生效时刻；不得早于首次披露 |
| status | status | INTEGER | 否 | risk_status编号；区分有效、解除和撤销 |
| resolved_at | resolved_at | INTEGER | 是 | 解除或撤销披露时刻；非active状态必须提供 |
| expires_at | expires_at | INTEGER | 是 | 有证据或明确规则支持的失效时刻；NULL不表示已经失效 |

### 12.3 company_reduction_plan：减持事件的额外字段

一条减持事件至多一条，risk_id为主键及外键。不能把任意减持公告都解释为已实施减持。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| risk_event_id | risk_id | INTEGER | 否 | 引用company_risk_event.id，且类型必须为大股东减持 |
| planned_start_date | start_date | INTEGER | 是 | 计划减持开始日期；用于计划有效范围 |
| planned_end_date | end_date | INTEGER | 是 | 计划减持结束日期；与开始日期校验顺序 |
| planned_ratio_pct | planned_pct | REAL | 是 | 计划减持比例百分比，0—100；保留计划量化事实 |
| completion_disclosed_at | completion_disclosed_at | INTEGER | 是 | 完成或终止的正式披露时刻；供既有尾部风险期限计算 |

### 12.4 company_positive_event：已识别利好事件

id主键，UNIQUE(code,event_key)，code外键引用company_facts。只存实际测量和证据状态，不持久化最终加分。不同事件不适用的测量列必须为NULL。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| id | id | INTEGER | 否 | 内部利好事件主键 |
| code | code | TEXT | 否 | 股票代码，外键及按股读取索引 |
| event_key | event_key | TEXT | 否 | 原有稳定事件身份；关联计划、实施、进展和取消 |
| event_type | event_type | INTEGER | 否 | positive_type编号；决定测量字段和分级规则 |
| subject_type | subject_type | INTEGER | 否 | subject_type编号；明确实施主体 |
| status | status | INTEGER | 否 | positive_status编号；仅有计划不等于可奖励 |
| first_disclosed_at | first_disclosed_at | INTEGER | 否 | 首次可知时刻 |
| last_disclosed_at | last_disclosed_at | INTEGER | 否 | 最近关联披露时刻 |
| cancelled_at | cancelled_at | INTEGER | 是 | 取消披露时刻；cancelled状态必须提供 |
| positive_level | level | INTEGER | 否 | 0—3已核实级别；非0级别必须有对应milestone |
| ineligible_reason | ineligible_reason | INTEGER | 是 | 不具备奖励资格的原因编号；NULL表示没有该原因，不等于一定可执行 |
| actual_ratio_pct | actual_pct | REAL | 是 | 实际回购或增持比例百分比，0—100；计划比例不能代入 |
| repurchase_purpose | repurchase_purpose | INTEGER | 是 | 回购用途枚举；仅回购事件适用 |
| contract_amount_cny | contract_amount | REAL | 是 | 已核实合同金额，人民币元；仅合同事件适用 |
| reference_annual_revenue_cny | annual_revenue | REAL | 是 | 可比年度营收，人民币元；计算合同相对规模 |
| reference_report_date | revenue_report_date | INTEGER | 是 | 可比营收报告期；核验测量口径 |
| reference_disclosed_at | revenue_disclosed_at | INTEGER | 是 | 可比营收披露时刻；防止使用未来信息 |
| amount_to_revenue_pct | contract_revenue_pct | REAL | 是 | 合同金额/可比年度营收的百分比；可超过100，不套用持股比例上限 |
| binding_confirmed | binding_confirmed | INTEGER | 是 | 合同约束性是否核实，0/1；非合同事件为NULL |
| amount_basis | amount_basis | INTEGER | 是 | 金额可比口径枚举；非合同事件为NULL |

### 12.5 company_positive_milestone：首次达到利好级别的证据

复合主键(positive_id,level)。达到不同级别的首次时间分别保存，不能每次进展公告都重启奖励窗口。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| positive_event_id | positive_id | INTEGER | 否 | 外键引用company_positive_event.id |
| level | level | INTEGER | 否 | 1—3首次达到的级别 |
| qualified_at | qualified_at | INTEGER | 否 | 首次满足该级别的披露时刻；计算交易日奖励窗口 |
| measurement_value_pct | measured_pct | REAL | 否 | 当时达到门槛的实际百分比数值；非负，保留审计依据 |
| evidence_id | evidence_id | INTEGER | 否 | 外键引用company_evidence_ref.id；绑定达标证据 |

### 12.6 company_unlock：已知解禁安排

复合主键(code,unlock_date)。完整前视范围存在主表；没有明细行不能单独证明无解禁。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| code | code | TEXT | 否 | 外键引用company_facts.code |
| unlock_date | unlock_date | INTEGER | 否 | 解禁日期；计算相对决策日的前视风险 |
| ratio_pct | ratio_pct | REAL | 否 | 当日解禁比例百分比，0—100；风险阈值输入 |

### 12.7 company_review：当前待核实事项

id主键；(code,scope,reason,evidence_id)去重，NULL证据也去重。只保留当前有作用的原因，历史采集失败详情属于raw。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| id | id | INTEGER | 否 | 内部待核实记录主键 |
| code | code | TEXT | 否 | 外键引用company_facts.code |
| scope | scope | INTEGER | 否 | review_scope编号；确定受影响组件 |
| reason | reason | INTEGER | 否 | 待核实原因编号；决定降级说明和相应动作约束 |
| evidence_id | evidence_id | INTEGER | 是 | 外键引用company_evidence_ref.id；组件整体缺失时可无单篇证据 |
| detected_at | detected_at | INTEGER | 否 | 该待核实事项第一次被发现的时刻 |

### 12.8 company_evidence_ref：正式事实实际引用的证据键

id主键，UNIQUE(source,source_key)。只保存被风险、利好、达标节点或待核实事项引用的证据，不保存全量公告索引。发布后清理没有任何引用的键。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| id | id | INTEGER | 否 | 紧凑内部证据主键，供关系表使用 |
| source | source | INTEGER | 否 | evidence_source编号；与source_key共同定位原始资料 |
| source_key | source_key | TEXT | 否 | 供应商稳定记录键；原始标题、URL和正文全部留在raw |

### 12.9 company_risk_evidence：风险事件与证据的关系

复合主键(risk_id,evidence_id)，UNIQUE(risk_id,position)。保留现有证据顺序，避免“最近证据”语义改变。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| risk_event_id | risk_id | INTEGER | 否 | 外键引用company_risk_event.id |
| evidence_id | evidence_id | INTEGER | 否 | 外键引用company_evidence_ref.id |
| position | position | INTEGER | 否 | 从0开始的证据序号；按序恢复领域对象的证据元组 |

### 12.10 company_positive_evidence：利好事件与证据的关系

复合主键(positive_id,evidence_id)，UNIQUE(positive_id,position)。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| positive_event_id | positive_id | INTEGER | 否 | 外键引用company_positive_event.id |
| evidence_id | evidence_id | INTEGER | 否 | 外键引用company_evidence_ref.id |
| position | position | INTEGER | 否 | 从0开始的证据序号；保留事件进展证据顺序 |

### 12.11 company_exclusion：非ST永久排除事实

id主键，UNIQUE(code,reason,source,source_key)。该表无需依赖company_facts存在相同股票：已退市或不在当前采集人口中的股票仍可能需要保留排除事实。不保存ST历史，不保存一般公司风险的第二份结论。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| id | id | INTEGER | 否 | 内部排除事实主键 |
| code | code | TEXT | 否 | 六位股票代码；判断永久排除 |
| reason | reason | INTEGER | 否 | exclusion_reason编号；仅人工永久黑名单及已核实退市排除 |
| effective_at | effective_at | INTEGER | 否 | 排除事实生效时刻；支持按观察时刻读取 |
| source | source | INTEGER | 否 | exclusion_source编号；明确事实来源 |
| source_key | source_key | TEXT | 否 | 配置版本或来源证据键；用于幂等和审计 |
| evidence_hash | evidence_hash | BLOB | 否 | 32字节证据摘要；识别内容及检测冲突 |

download应用层提供明确的非ST排除写入用例，组合根将其注入运行时；recommendation通过类型化端口提交新观察事实，不直接操作此表。人工黑名单从现有配置重新生成；旧eligibility内容不导入。写入失败保留内存中的立即限制并显式标记持久化失败，不允许因为落盘失败而放行已知排除股票。

### 12.12 company_publication：正式库发布身份

只允许singleton=1的一行，与事实变更在同一个正式库事务中提交。

| 原字段 | 现字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- | --- |
| singleton | singleton | INTEGER | 否 | 主键，CHECK=1；保证唯一活动发布身份 |
| schema_version | schema_version | INTEGER | 否 | 结构协议版本固定2；原schema 1及旧混合库不读取 |
| extraction_hash | extraction_hash | BLOB | 否 | 32字节提取身份摘要；包含codec/提取协议及相关规则参数 |
| content_hash | content_hash | BLOB | 否 | 32字节确定性内容摘要；绑定本次正式事实及排除事实 |
| published_at | published_at | INTEGER | 否 | 本次完整事务的发布时刻；与各股票资料年龄分开 |

内容摘要按固定表序、稳定业务键、明确类型和长度编码计算；不能依赖无序SELECT、Python对象repr或自增id分配。排除事实的独立写入也必须原子更新内容身份，避免事实和版本脱节。

<a id="company-raw"></a>

## 13. 公司采集库：原字段与现字段映射

文件为data/company/raw/company_collection.sqlite3，23张表使用schema 2（PRAGMA user_version=2，contract.schema_id固定为company_collection）。旧未版本化采集库拒绝读取，不现场迁移或保留旧列。采集日期/时刻继续使用ISO TEXT；record_hash仍是原来源记录摘要TEXT，不强转为32字节SHA-256。采用“临时压缩正文＋SQLite采集索引”：PDF由适配器有界下载到内存，解析结束即释放，不落盘归档；压缩正文暂存在raw/text。SQLite负责按股票、日期、来源定位、去重、分页续传、失败重试和受影响股票队列，不保存正文，不参与推荐启动的常规扫描。

**硬盘占用采用滚动批次限制：下载一部分、提取一部分、正式提交一部分、清理一部分，再进入下一批。raw不是长期保存全部公告附件和正文的归档。** 正常路径默认每批最多50篇，沿用有界并发；正文临时目录上限256 MiB，另留64 MiB文件系统余量。派发前按正文字符上限的UTF-8最坏字节量计算本批容量，剩余空间不足时缩小批次；崩溃恢复时先处理已保存正文，仍不足则明确停止新下载。该额度不包括正式事实、采集索引和其它业务数据，不能理解为整个data目录上限。

正式提交成功并核实该篇公告所影响的本次采集人口内全部股票已经发布后，提交回执并删除正文，仅保留必要的来源记录键、URL、标题、披露时间、内容摘要、处理身份和发布回执。无关公告不长期保存完整目录项；待处理项保留，正式事实仍引用的证据定位保留，已完成范围保留紧凑覆盖证明。解析失败只保留原因及重试计划，后续确需重试时重新下载。后续增加股票或改变提取规则时必须重新验证来源，不能将旧回执当作新规则已处理。

正文按内容SHA-256定位，写入临时文件、刷盘并原子替换后，才能将摘要和已检查状态提交到索引；中途崩溃至多留下未引用文件，不能产生“已检查但文件未写完”的事实。读取校验摘要、解压上限及UTF-8。尚未发布且应有的文件缺失/损坏必须回到待补；已有发布回执而按策略删除的文件不算损坏，也不能当作新空正文重新提取。既有确认事件和待核实项从正式库继续保留；先遇到结案/进展公告而尚未找到对应旧事件时保留待核实，之后发现原事件则重新获取关联公告核验。现有分页、重试和完整性约束保留。

以下结构已在阶段281代码中落地，真实全市场验收尚未完成；E包目标字段与当前字段并列，同名即保留。表名沿用已明确职责的现名，不为单复数重做采集实现。record_hash只更名、不把未核实算法的现有digest强制转为SHA-256或BLOB；body_hash和extraction_hash仍按已验证的32字节SHA-256保存。as_of_date在ratios及structured_members中表示component所限定的统计日或解禁日，不改成交易日。日期TEXT沿用ISO日期，时刻TEXT沿用带时区ISO格式，避免为采集资料增加与正式库无关的格式迁移。除标注“可空”外均非空；组合键、外键及幂等规则在表说明中列出。

| 原表.字段 | 现字段 | SQLite类型 | 含义及作用 |
| --- | --- | --- | --- |
| contract.identity | schema_id | TEXT，可空 | 普通rowid表的TEXT主键允许SQL NULL；初始化与合同校验只接受唯一company_collection，拒绝旧混合库或其它身份 |
| disclosures.id | id | INTEGER | 公告/新闻内部主键；供关联、正文要求和重试引用 |
| disclosures.source | source | TEXT | 原始来源标识；与source_id组成唯一键 |
| disclosures.source_id | source_key | TEXT | 来源内稳定记录ID；与正式证据键关联 |
| disclosures.kind | kind | TEXT | announcement或news；区分公告和新闻，不将新闻直接当官方结论 |
| disclosures.title | title | TEXT | 原始标题；用于选择待提取正文及展示证据 |
| disclosures.published_at | published_at | TEXT | 原始披露时刻；窗口查询和点时过滤 |
| disclosures.url | url | TEXT | 原始公告/文档链接；下载与显式证据查询 |
| disclosures.digest | record_hash | TEXT | 采集记录内容摘要；检测原公告修订及拒收旧正文结果 |
| disclosures.body_hash | body_hash | BLOB，可空 | 正文UTF-8内容的32字节SHA-256；定位raw/text压缩文件并核对内容，未提取时NULL |
| disclosures.body_checked | body_checked | INTEGER | 0/1，正文是否已完成检查；不能把下载失败记成已检查 |
| disclosures.body_retained | body_retained | INTEGER | 0/1，当前是否仍保有待发布正文文件；正式提交并清理后为0，不能单凭此列判断提取成功 |
| document_publications.disclosure_id | disclosure_id | INTEGER | disclosures外键；与code组成主键，记录该来源版本对每个股票的正式发布回执 |
| document_publications.code | code | TEXT | 已成功发布的关联股票；多股票公告全部受影响股票完成后才允许删正文 |
| document_publications.digest | record_hash | TEXT | 已发布的来源记录版本；公告修订后旧回执不能用于清理新文件 |
| document_publications.extraction_hash | extraction_hash | BLOB | 32字节提取身份；规则改变后不得继续使用旧提取完成状态 |
| document_publications.published_at | published_at | TEXT | 确认正式库提交后的回执时刻；供恢复与清理核验 |
| disclosure_codes.code | code | TEXT | 股票代码；复合主键(code,disclosure_id) |
| disclosure_codes.disclosure_id | disclosure_id | INTEGER | disclosures外键，删除公告时级联；支持一篇公告关联多股 |
| partitions.market | market | TEXT | 市场范围；与start_date,end_date组成分区主键 |
| partitions.start | start_date | TEXT | 分区查询起日 |
| partitions.end | end_date | TEXT | 分区查询止日 |
| partitions.next_page | next_page | INTEGER | 下次应请求页码，初始1；用于续传 |
| partitions.expected_rows | expected_rows | INTEGER，可空 | 来源声明的总行数；未获得时未知 |
| partitions.received_rows | received_rows | INTEGER | 已接收且校验的行数；用于完整性核对 |
| partitions.complete | is_complete | INTEGER | 0/1，分区是否完整；未完成不能推进完整覆盖 |
| partitions.checked_at | checked_at | TEXT，可空 | 分区核查时刻 |
| partitions.failure | failure | TEXT | 原始失败码/详情；成功时为空，正式库只保存归类后的枚举 |
| partition_rows.market | market | TEXT | 所属市场；复合主键的一部分 |
| partition_rows.start | start_date | TEXT | 所属分区起日 |
| partition_rows.end | end_date | TEXT | 所属分区止日 |
| partition_rows.source_id | source_key | TEXT | 本轮已接收来源ID；参与幂等核对 |
| partition_rows.code | code | TEXT | 本轮来源ID关联的股票；与前四列组成主键 |
| coverage.market | market | TEXT | 完整覆盖市场；与start_date,end_date组成主键 |
| coverage.start | start_date | TEXT | 已证明完整的范围起日；不能仅保留滚动窗口而丢失初始覆盖证据 |
| coverage.end | end_date | TEXT | 已证明完整的范围止日 |
| coverage.checked_at | checked_at | TEXT | 覆盖证明核查时刻 |
| financials.code | code | TEXT | 股票代码；与report_date组成主键 |
| financials.report_date | report_date | TEXT | 来源财报报告期 |
| financials.published_at | published_at | TEXT | 来源财报披露时刻 |
| financials.basic_eps | eps | REAL，可空 | 来源基本每股收益，元/股 |
| financials.book_value_per_share | bps | REAL，可空 | 来源每股净资产，元/股 |
| financials.revenue_growth_pct | revenue_growth_pct | REAL，可空 | 来源营收同比百分比 |
| financials.net_profit_growth_pct | profit_growth_pct | REAL，可空 | 来源归母净利润同比百分比 |
| financials.core_profit_growth_pct | core_profit_growth_pct | REAL，可空 | 来源扣非净利润同比百分比 |
| financials.roe_pct | roe_pct | REAL，可空 | 来源净资产收益率百分比 |
| financials.parent_net_profit | parent_profit | REAL，可空 | 来源归母净利润，人民币元 |
| financials.core_net_profit | core_profit | REAL，可空 | 来源扣非净利润，人民币元 |
| ratios.component | component | TEXT | pledge或unlock；与code,as_of_date组成主键 |
| ratios.code | code | TEXT | 来源股票代码 |
| ratios.day | as_of_date | TEXT | 质押统计日或解禁日，由component区分 |
| ratios.ratio_pct | ratio_pct | REAL | 来源比例百分比，0—100 |
| component_checks.component | component | TEXT | 被检查组件；与scope组成主键 |
| component_checks.scope | scope | TEXT | market或具体股票等检查范围 |
| component_checks.checked_at | checked_at | TEXT，可空 | 最近核查时刻 |
| component_checks.failure | failure | TEXT | 来源失败细节；向正式事实转换时归为固定原因 |
| structured_progress.component | component | TEXT | 结构化来源组件；与scope组成主键 |
| structured_progress.scope | scope | TEXT | 续传范围 |
| structured_progress.start | start_date | TEXT | 当前结构化任务范围起日 |
| structured_progress.end | end_date | TEXT | 当前结构化任务范围止日 |
| structured_progress.next_page | next_page | INTEGER | 下一待请求页码 |
| structured_progress.expected_rows | expected_rows | INTEGER，可空 | 来源声明总数 |
| structured_progress.received_rows | received_rows | INTEGER | 本轮已确认数量 |
| structured_members.component | component | TEXT | 当前结构化任务组件；与code,as_of_date组成主键 |
| structured_members.code | code | TEXT | 本轮已确认股票 |
| structured_members.day | as_of_date | TEXT | 本轮记录对应日期 |
| structured_members.ratio_pct | ratio_pct | REAL，可空 | 当前批次比例；完整核验后进入ratios |
| ratio_snapshots.component | component | TEXT | 主键，已完整发布的比例组件 |
| ratio_snapshots.start | start_date | TEXT | 该完整快照覆盖起日 |
| ratio_snapshots.end | end_date | TEXT | 该完整快照覆盖止日 |
| ratio_snapshots.checked_at | checked_at | TEXT | 该完整快照核查时刻 |
| document_retries.disclosure_id | disclosure_id | INTEGER | 主键及disclosures外键，随公告删除级联 |
| document_retries.digest | record_hash | TEXT | 本次重试对应的公告摘要；正文变更后旧重试失效 |
| document_retries.policy | retry_policy_id | TEXT | 重试/解析策略身份；策略变化可重新调度 |
| document_retries.failure | failure | TEXT | 本次具体失败原因；不写入正式库自由文本列 |
| document_retries.attempted_at | attempted_at | TEXT | 最近尝试时刻 |
| document_retries.retry_after | retry_at | TEXT | 最早允许再次尝试时刻；控制退避 |
| document_retries.attempts | attempts | INTEGER | 已尝试次数；用于有界重试 |
| disclosure_query_policy.singleton | singleton | INTEGER | 主键且等于1，查询策略唯一活动行 |
| disclosure_query_policy.identity | query_policy_id | TEXT | 查询拆分策略身份；检查续传是否仍匹配 |
| disclosure_queries.market | market | TEXT | 子查询所属市场；主键及父分区外键组成部分 |
| disclosure_queries.start | start_date | TEXT | 所属分区起日 |
| disclosure_queries.end | end_date | TEXT | 所属分区止日 |
| disclosure_queries.scope | scope | TEXT | 子查询范围键；与前三列组成主键 |
| disclosure_queries.plate | plate | TEXT | 供应商板块筛选参数 |
| disclosure_queries.securities | securities | TEXT | 供应商证券筛选参数；仅采集协议，不进入正式库 |
| disclosure_queries.next_page | next_page | INTEGER | 子查询下次页码 |
| disclosure_queries.expected_rows | expected_rows | INTEGER，可空 | 子查询来源声明总数 |
| disclosure_queries.received_rows | received_rows | INTEGER | 子查询已验证接收总数 |
| disclosure_queries.complete | is_complete | INTEGER | 0/1，子查询完整状态 |
| disclosure_queries.failure | failure | TEXT | 子查询失败细节 |
| disclosure_query_rows.market | market | TEXT | 所属子查询市场 |
| disclosure_query_rows.start | start_date | TEXT | 所属子查询起日 |
| disclosure_query_rows.end | end_date | TEXT | 所属子查询止日 |
| disclosure_query_rows.scope | scope | TEXT | 所属子查询范围；前四列外键引用disclosure_queries |
| disclosure_query_rows.source_id | source_key | TEXT | 已接收来源ID |
| disclosure_query_rows.code | code | TEXT | 关联股票；前六列组成主键 |
| disclosure_query_rows.record_key | record_key | TEXT | 供应商原始记录唯一键；校验重复/分页边界 |
| partition_retries.market | market | TEXT | 重试分区市场 |
| partition_retries.start | start_date | TEXT | 重试分区起日 |
| partition_retries.end | end_date | TEXT | 重试分区止日；前三列引用父分区 |
| partition_retries.scope | scope | TEXT | 重试子范围；与前三列组成主键 |
| partition_retries.retry_after | retry_at | TEXT | 最早可重试时刻 |
| company_dirty_codes.code | code | TEXT | 主键，原始输入变化后需要重新编译的股票；不是正式事实副本 |
| company_document_requirements.disclosure_id | disclosure_id | INTEGER | 主键及disclosures外键；仅索引需要正文核验的公告 |
| company_work_policy.singleton | singleton | INTEGER | 主键且等于1 |
| company_work_policy.identity | work_policy_id | TEXT | 待提取正文筛选规则身份；变化时重建工作索引 |
| disclosure_query_contracts.market | market | TEXT | 查询市场 |
| disclosure_query_contracts.start | start_date | TEXT | 查询起日 |
| disclosure_query_contracts.end | end_date | TEXT | 查询止日 |
| disclosure_query_contracts.scope | scope | TEXT | 查询子范围；与前三列组成主键 |
| disclosure_query_contracts.page_size | page_size | INTEGER | 固定30；校验分页合同 |
| disclosure_query_contracts.filter_identity | filter_identity | TEXT | 固定all_announcements；证明查询未漏筛公告类别 |
| company_population.code | code | TEXT | 主键，当前采集工作人口；用于标记受影响股票，不保存风险或发布状态 |

raw中不再有published、publication或company_facts的第二份正式表。正式发布日期、版本和已发布人口从正式库取得；company_population只是采集任务范围。

已完成且形成覆盖证明的分区，可释放partition_rows和disclosure_query_rows的逐页去重成员；未完成分区必须保留。释放成员不能绕过正文正式发布检查、丢失coverage证明或把未完成分区标为完成。结构化批次暂存成员同样按其完整提交合同清理。采集索引本身也必须清理已完成过程项并回收可复用页，不能只删PDF却放任索引、WAL和重试历史无限增长。

清理必须保留正式事件、达标节点和待核实事项引用的证据定位及摘要，无需长期保留它们的原始附件和全文。跨年持续事件及其未解决的待核实原因由正式事实继续保存，不能随正文删除或超出365日查询窗口而消失。后续新公告基于已发布结构化状态增量归并；提取规则升级或确需重核旧文时按证据定位重新下载。清理只影响原始/过程资料，不回写或重新解释旧冻结推荐记录。

<a id="company-publication"></a>

## 14. 公司发布、读取与测量

### 14.1 ST与永久排除的单一来源

1. ST及参考人口统一读取第2.4节data/market/reference/company_st.sqlite3的不可变发布；旧historical_st.sqlite3及inputs.json已退出活动路径，不双读或回退。
2. company_facts不增加is_st、ever_st或ST历史表；company_exclusion也不保存ST记录。
3. 当前报价或证券名称新出现ST时，运行内存立即限制该股票；已有参考维护流程负责后续持久化核查，不能为此在company建一份ST历史。
4. 退市和人工永久黑名单保留现有有效排除语义，移入company_exclusion；普通处罚、造假等公司风险继续由公司事件和评分规则负责，不能意外恢复旧的永久排除规则。
5. eligibility的CLI列表、状态投影、静态筛选、报价筛选、诊断、测试及bootstrap必须一起切换。外部状态字段如继续保留，必须是新事实的只读投影，不能隐藏读取旧库。

### 14.2 编译、原子发布与并发

#### 正常发布

1. 采集器将公告元信息及结构化来源写入raw索引，PDF有界驻留内存、正文压缩写入raw/text，再提交正文摘要并标记受影响股票；20页/50篇等现有有界批次和取消机制继续生效。
2. 固定raw读取快照和当前正式发布父身份，按股票读取原始输入；使用真实不可变类型编译财务、风险、利好、解禁和待核实事项。
3. 从raw解析来源和证据键，将说明文本转换成明确原因枚举。提取规则或阈值身份变化时重建受影响的完整正式人口，不混合两种提取身份。
4. 在正式库事务外完成耗时解析和准备；写入前检查原始输入版本和正式父版本。正式库只在短事务内更新主表、子表、证据引用和发布身份。
5. 同一次事务保证读者看到旧完整版本或新完整版本。单个股票事实及其证据关系不能半更新；失败、取消或进程中断保留最近有效版本。
6. 正式提交后再清理raw的dirty标记。两库不宣称存在分布式事务：正式提交后、清理前崩溃会导致幂等重复编译，不能导致漏编译。清理须确认输入版本仍未变化，否则保留dirty。
7. 独立非ST排除写入与发布共用正式写入所有者及并发父版本检查；发生冲突重新读取并重建，禁止用旧全库快照覆盖新排除事实。
8. 对本批正文登记与来源版本、提取身份绑定的逐股发布回执；全部关联股票完成后删除相应临时正文，再获取下一批。发布失败不清理未提交输入，发布成功但清理中断则在恢复时幂等清理；重复启动不重复下载已核实且来源版本未改变的正文。

数据源不完整可以发布明确partial/unavailable的事实，但不得宣称“全量风险已核验”。365日公告完整并不证明较早上市公司的初始持续风险已知，缺少上市初始状态证据时保留原因15。

#### 读取与资源边界

- 只读加载固定一次发布身份，在同一个正式库快照内分批读取12张表所需内容，使用JOIN/批量查询，避免逐事件N+1查询。
- 同一进程所选档位复用一个公司事实内存快照；阶段281先验收V1/V2/V3；各档评分仍保持原策略隔离。
- 启动、普通刷新及/api/status不得扫描raw、全文或分页表。证据详情单独有界查询，最多32个证据ID，查询时保留股票归属校验。
- schema_version或extraction_hash不匹配时明确拒绝使用，提示重新同步；不双读旧JSON、不猜测格式、不自动迁移。
- 新库缺失或来源失败时保持已有合法只读快照，并呈现待补原因；不能把未知解释成无风险。

### 14.3 体积估算和测量方式

4401行主表的数值字段、短代码和SQLite行开销预计约1—2 MiB以内；事件、证据关系、待核实项和索引取决于实际数量。正式库整体以约3—10 MiB作为初始工程估算，不作为承诺或硬性容量上限。

若大量股票确有多个有效事件或待核实事项，必须如实保存，不能为了体积删除必要事实。正式库若仍达到数百MiB，应逐表检查是否错误纳入全量公告、重复证据、长文本或已失效过程状态。

验收分别报告正式库、raw采集索引、内存附件峰值、压缩正文、WAL/SHM和目录总量，列出行数、dbstat表及索引空间、空闲页和每股平均正式占用。目录仍可能因原始资料较大而增长，关键是启动只依赖轻量正式库。VACUUM只能回收已空闲页面，不能替代本次结构重构。

<a id="all-schema"></a>

## 15. 全库源码清单与字段去向

本节固定于基线0ae8ce75的活动源码：20个建表文件、92个持久表定义；同名表按所属库分别计数，动态行业表分别展开，样本动态特征另列。公司12张正式表和23张采集表已由第12、13节覆盖，以下列出其余57张。临时查询表、SQLite内部表、索引及按年度/档位生成的多份物理文件不重复计数。用户已清空data，因此这是源码清单，不是实库行数或运行状态；当前进度与测量只记工程台账。

### 15.1 读法、类型和身份

每个建表来源给出当前owner、目标owner、消费者及必删职责。表中按基线源码顺序穷举列；涉及退役冻结协议的名称按业务含义描述，不作为旧库读取合同；类型后“?”表示允许NULL，PK单列或组合键另列。目标说明未点名的列保留名称及含义；“删除”指新格式不再建立，不是对已删除实库执行迁移。目标日期统一采用第11.2节的日期codec，时刻采用UTC微秒INTEGER，明确的raw供应商查询证据保留ISO TEXT边界。计数和序号INTEGER，有限价格/量额/收益/特征REAL；SHA-256为32字节BLOB，普通ID仍为TEXT。可空性沿用业务合同，不因重命名将缺失填0。

价格单位为元/股，volume为股、amount为人民币元，change_pct/turnover_pct为百分数；raw/qfq两侧存在性及缺失配对保持。研究价格的adjustment列继续区分口径，不能去掉它后只剩含义不明的open/close。样本特征和标签依各档固定定义，不能因为都是REAL就互换尺度。SQL列改名和类型codec保持逻辑值及研究身份；用户另行授权的freeze协议改名会改变新冻结/事件规范字节与摘要，不反向改写旧记录。

下列PK来自建表定义；所有已有外键、UNIQUE、CHECK和查询索引在原子切换时对应改名。年度库继续支持(code,trade_date,first_seen_sequence,revision_id)、(trade_date,board,code,first_seen_sequence,revision_id)、观察序号及缺口条件索引；样本保留(industry,trade_date,code)读取索引。逐表原约束和索引以链接源码为准，不能仅复制下表列名重建无约束表。目标实现完成时按第10.3节更新实际结构字典。

### 15.2 当前持久表及完整列清单

**[download/infra/history_control_index.py](../src/trader/download/infra/history_control_index.py)**

B；download保持owner，catalog结构化。消费者：history_sync_runner、history_download_flow、history_snapshot_publication/reader/inspection、published_history_snapshot、训练历史输入、推荐共享预读及诊断。旧bound_references整包JSON、immutable_records通用表和旧SQL在B删除。

schema_contract另保存显式注入的reference_file相对路径；calendar_binding和universe_version各自绑定publication_id，避免同一ST/行业摘要下证券或日历更新造成错误复用。历史行业区间转为history_industry，快照月份证明转为snapshot_partition。原通用记录表还承载training到期/提醒，需同批切换为training_due、training_reminder_claim、training_reminder，由training唯一创建与写入；只读消费者不建表。完整字段见[下载训练字典](4_下载训练.md#db05-datamarkethistorycatalogsqlite3)。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| bound_references；PK(content_hash) | content_hash TEXT；payload_json TEXT | reference_binding；content_hash→reference_hash BLOB；payload_json删除，改为第2.4节publication_id及版本引用；保留跨库发布校验，不复制证券/ST/日历。 |
| metadata；PK(singleton) | singleton INTEGER；schema_identity TEXT | schema_contract；schema_identity→schema_id TEXT；singleton保留；增加schema_version INTEGER。 |
| immutable_records；PK(kind,record_key) | kind TEXT；record_key TEXT；content_hash TEXT；payload_json TEXT | 按kind拆为source_version、calendar_binding、universe_version、sync_checkpoint、history_snapshot；kind由表名表达；record_key→各实体id；content_hash保留为BLOB；payload_json按HistorySourceIdentity/HistoryCalendarIdentity/HistoryUniverseIdentity/HistorySyncCheckpoint/HistorySnapshot拆为具名列及成员表，不能改名后继续存整包JSON。 |
| published_snapshot；PK(singleton) | singleton INTEGER；snapshot_hash TEXT；sequence INTEGER | published_snapshot；sequence→sync_sequence INTEGER；snapshot_hash→同名BLOB；singleton保留。 |

**[download/infra/history_download_index.py](../src/trader/download/infra/history_download_index.py)**

B；download保持owner。消费者：history_download_flow、history_sync_runner、续传规划/补缺及统一诊断。删除dates_json和旧任务SQL，完整任务确认仍在价格提交之后。

本组所有目标trade_date、cutoff_date均为自1970-01-01起的天数INTEGER；“保留”只指列名与业务语义，不保留旧日期TEXT类型。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| download_batch；PK(singleton) | singleton INTEGER；sequence INTEGER；source_hash TEXT；calendar_hash TEXT；universe_hash TEXT；cutoff TEXT | download_batch；sequence→sync_sequence INTEGER；cutoff→cutoff_date；source_hash/calendar_hash/universe_hash→同名BLOB；singleton保留。 |
| download_tasks；PK(code) | code TEXT；dates_json TEXT；tencent_done INTEGER；attempts INTEGER；failure TEXT? | download_tasks及download_task_dates；dates_json→download_task_dates(code,trade_date)复合主键行；code/tencent_done/attempts/failure保留，tencent_done仅表示腾讯阶段完成，failure仍为采集失败详情。 |
| download_gaps；PK(code,trade_date) | code TEXT；trade_date TEXT | download_gaps；全部保留。 |
| download_qfq_unusable；PK(code,trade_date) | code TEXT；trade_date TEXT；checked_cutoff TEXT | download_qfq_unusable；checked_cutoff→cutoff_date；表示核验所针对的历史截止日，code/trade_date保留。 |

**[download/infra/history_prices.py](../src/trader/download/infra/history_prices.py)**

B；download保持owner。消费者：history_direct_prices、history_partition_repack、history_snapshot_reader/inspection、published_history_snapshot、训练历史输入、推荐共享预读及结算。删除旧两表与旧价格列codec，不修改领域价格口径。

两个价格表的trade_date均改为天数INTEGER；daily_price_revision增加第3.1节定义的number_flags，保留原数值表示和内容身份。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| metadata；PK(singleton) | singleton INTEGER；schema_identity TEXT；calendar_year INTEGER | schema_contract；schema_identity→schema_id；singleton/calendar_year保留；增加schema_version INTEGER。 |
| daily_records；PK(trade_date,code,revision_id) | trade_date TEXT；code TEXT；revision_id TEXT；first_seen_sequence INTEGER；board TEXT；status TEXT?；raw_present INTEGER?；raw_open BLOB?；raw_high BLOB?；raw_low BLOB?；raw_close BLOB?；raw_volume BLOB?；raw_amount BLOB?；raw_preclose BLOB?；raw_pct_change BLOB?；raw_turnover BLOB?；raw_trading_status TEXT?；qfq_present INTEGER?；qfq_open BLOB?；qfq_high BLOB?；qfq_low BLOB?；qfq_close BLOB?；qfq_volume BLOB?；qfq_amount BLOB?；qfq_preclose BLOB?；qfq_pct_change BLOB?；qfq_turnover BLOB?；qfq_trading_status TEXT?；content_hash TEXT | daily_price_revision；四个百分数列按第3.3节改名；所有价格、量额及百分数从声明BLOB改为REAL；content_hash→同名BLOB；其它列保留。 |
| daily_observations；PK(trade_date,code,sync_sequence) | trade_date TEXT；code TEXT；sync_sequence INTEGER；revision_id TEXT | daily_price_observation；全部保留；外键切到daily_price_revision。 |

**原download/infra/history_reference_codec.py；目标[reference_index](../src/trader/download/infra/reference_index.py)和[history_reference_index](../src/trader/download/infra/history_reference_index.py)**

B；download保持owner。消费者：history_st_source、history_st_merge、reference_publication、PrepareReference、history/market/company共用准备、训练/推荐PublishedReferences和诊断。旧独立ST读写codec、inputs.json发布owner及路径同批删除；第2.4节定义新参考关系。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| st_evidence；PK(code) | code TEXT；checked_on TEXT；status TEXT | security_st_evidence及security_st_current；checked_on→checked_date；status→st_status；code保留；新增版本st_id和content_hash，采集当前指针与发布成员分离。实际供应商核验日与本轮PreparedReference.checked_on不得互相覆盖。 |

**[download/infra/qfq_sqlite.py](../src/trader/download/infra/qfq_sqlite.py)**

B；download保持owner。消费者：qfq_update_runner、短窗只读输入、推荐历史/价格适配、训练补窗及诊断。保留同库双档发布事务，删除旧SQL及路径猜测。

目标trade_date、start_date、end_date、cutoff_date均为天数INTEGER；daily_price与年度价格共用number_flags编码，金额和百分数单位保持。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| bars；PK(code,day) | code TEXT；day TEXT；status TEXT?；raw_present INTEGER?；raw_open BLOB?；raw_high BLOB?；raw_low BLOB?；raw_close BLOB?；raw_volume BLOB?；raw_amount BLOB?；raw_preclose BLOB?；raw_pct_change BLOB?；raw_turnover BLOB?；raw_trading_status TEXT?；qfq_present INTEGER?；qfq_open BLOB?；qfq_high BLOB?；qfq_low BLOB?；qfq_close BLOB?；qfq_volume BLOB?；qfq_amount BLOB?；qfq_preclose BLOB?；qfq_pct_change BLOB?；qfq_turnover BLOB?；qfq_trading_status TEXT? | daily_price；day→trade_date；价格列沿用年度库映射和REAL类型；其余保留，不能按profile复制价格事实。 |
| identities；PK(profile,code) | profile TEXT；code TEXT；digest TEXT；source TEXT；first_day TEXT；cutoff TEXT | window_binding；digest→content_hash BLOB；first_day→start_date；cutoff→end_date；profile/code/source保留。 |
| calendars；PK(profile,day) | profile TEXT；day TEXT | window_calendar；day→trade_date；profile保留。 |
| completions；PK(code) | code TEXT；day TEXT；source TEXT | window_completion；day→cutoff_date；code/source保留；来自context.calendar.open_dates[-1]，表示完成的窗口截止交易日，不是执行核查日。 |
| publications；PK(profile) | profile TEXT；identity TEXT | window_publication；identity→publication_id TEXT；profile保留，不能仅按字符串外观将发布ID转SHA-256。 |

**原infra/dated_query_cache.py；目标[industry_query_index](../src/trader/download/infra/industry_query_index.py)**

B；业务事实仍归download。当前通用primitive却创建两张行业表；目标将行业建表/SQL放入download的唯一行业adapter，infra仅留跨业务SQLite primitive。消费者：history_industry_cache、current_industry_publication、参考发布及诊断；删除dated_query_cache旧活动实现。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| queries；PK(day) | day TEXT；source TEXT；observed_on TEXT；payload TEXT；digest TEXT | industry_query；day→anchor_date；observed_on→checked_date；digest→content_hash BLOB；source保留；payload→members_json TEXT，仅保留供应商查询证据codec，不充当可修改证券总体。 |
| current_industry_queries；PK(day) | day TEXT；source TEXT；observed_on TEXT；payload TEXT；digest TEXT | current_industry_query；day→as_of_date；observed_on→checked_date；digest→content_hash BLOB；source/payload映射同上，当前查询不得覆盖历史锚点。 |

**原data-plane表族；序号新owner：[publication_sequence.py](../src/trader/recommendation/infra/persistence/publication_sequence.py)**

C；recommendation保持序号owner。消费者：publication_sequence、组合根及运行发布/SSE。其余data-plane接口只剩实现/端口/测试，切换前再次搜索全部调用；删除data_plane/sqlite/codec中空置参考读写、端口与相关测试，不误删训练研究快照。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| schema_meta；PK(key) | key TEXT；value TEXT | schema_contract；旧key/value通用表删除；新publication_state.sqlite3只保留schema_id/schema_version固定合同。 |
| security_master_recent；PK(code) | code TEXT；observed_at TEXT；source_time TEXT；source TEXT；data_version TEXT；schema_version TEXT；payload_hash TEXT；payload TEXT；status TEXT；error TEXT；recovery_payload BLOB?；recovery_sha256 TEXT | 删除；全部列删除；证券参考读取download发布，不能将旧recent数据迁入新参考。 |
| security_master_formal；PK(freeze_id,code) | freeze_id TEXT；code TEXT；observed_at TEXT；source_time TEXT；source TEXT；data_version TEXT；schema_version TEXT；payload_hash TEXT；payload TEXT；status TEXT；error TEXT；recovery_payload BLOB?；recovery_sha256 TEXT | 删除；全部列删除；旧freeze_id参考副本不进入新freeze库。 |
| source_cursor_recent；PK(cursor_name) | cursor_name TEXT；observed_at TEXT；source_time TEXT；source TEXT；data_version TEXT；schema_version TEXT；payload_hash TEXT；payload TEXT；cursor_value TEXT；status TEXT；error TEXT；recovery_payload BLOB?；recovery_sha256 TEXT | 删除；全部列删除；没有生产写入消费者，删除对应端口/codec。 |
| source_cursor_formal；PK(freeze_id,cursor_name) | freeze_id TEXT；cursor_name TEXT；observed_at TEXT；source_time TEXT；source TEXT；data_version TEXT；schema_version TEXT；payload_hash TEXT；payload TEXT；cursor_value TEXT；status TEXT；error TEXT；recovery_payload BLOB?；recovery_sha256 TEXT | 删除；全部列删除；没有生产写入消费者，删除对应端口/codec。 |
| trading_calendar_recent；PK(calendar_name) | calendar_name TEXT；observed_at TEXT；source_time TEXT；source TEXT；data_version TEXT；schema_version TEXT；payload_hash TEXT；payload TEXT；status TEXT；error TEXT；recovery_payload BLOB?；recovery_sha256 TEXT | 删除；全部列删除；日历从download不可变参考读取。 |
| trading_calendar_formal；PK(freeze_id,calendar_name) | freeze_id TEXT；calendar_name TEXT；observed_at TEXT；source_time TEXT；source TEXT；data_version TEXT；schema_version TEXT；payload_hash TEXT；payload TEXT；status TEXT；error TEXT；recovery_payload BLOB?；recovery_sha256 TEXT | 删除；全部列删除；不再生成冻结日历副本。 |
| data_plane_quarantine_audit；PK(audit_key) | audit_key TEXT；record_kind TEXT；record_identity TEXT；reason TEXT；observed_at TEXT | 删除；全部列随空置data-plane删除；正式冻结/事件各自仍保留隔离失败职责。 |
| publication_sequence；PK(singleton) | singleton INTEGER；reserved_until INTEGER | publication_sequence；singleton/reserved_until全部保留为INTEGER，保留持久预留高水位及单写事务。 |

**[recommendation/infra/persistence/freeze_records.py](../src/trader/recommendation/infra/persistence/freeze_records.py)**

C；recommendation保持owner。消费者：freeze_coordinator、read_only_queries、recent_news_codes、application_system、结算与追踪、HTTP/SSE只读投影及诊断。删除旧冻结库路径、检查点读写与旧schema；新数据不导入旧记录。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| 旧正式记录表；PK(strategy,trade_date) | strategy TEXT；trade_date TEXT；version TEXT；payload_hash TEXT；payload_sha256 TEXT；payload BLOB；committed_at TEXT；commit_kind TEXT；status TEXT；error TEXT | freeze_record；version→freeze_id TEXT；payload_hash→freeze_hash BLOB；payload_sha256→record_hash BLOB；payload→record_bytes BLOB；error→failure_code TEXT；其余保留，第4节明确规范字节及first-wins约束。 |
| 旧冻结检查点表；PK(strategy,trade_date) | strategy TEXT；trade_date TEXT；version TEXT；原冻结版本 TEXT；observed_at TEXT；boundary_at TEXT；payload_sha256 TEXT；payload BLOB；consumed_at TEXT?；error TEXT | 删除；全部列随退役检查点owner删除；新库不建表、不读旧检查点。 |

**收益新owner：[recommendation/infra/persistence/outcome_evidence_index.py](../src/trader/recommendation/infra/persistence/outcome_evidence_index.py)**

C；改造前的收益计算、类型、结算编排、端口和写入实现位于training，新owner分别为recommendation/domain/outcomes、recommendation/application/outcome_settlement.py、recommendation/application/ports/outcomes.py及上述持久化模块，training只读。消费者：在线收益结算、点时研究、收益证据/状态、行情适配器、只读查询、bootstrap及诊断；原training/domain/evaluation/evaluation.py与models.py、training/application/outcome_settlement.py与outcome_ports.py、training/infra/research/outcome_evidence_index.py随消费者切换删除，研究复用新owner的纯计算和不可变类型。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| benchmark_returns；PK(trade_date) | trade_date TEXT；return_pct REAL；observed_at TEXT；payload_hash TEXT；payload BLOB | benchmark_returns；payload_hash→record_hash BLOB；payload→record_bytes BLOB；trade_date/return_pct/observed_at保留，return_pct为百分数。 |
| recommendation_outcomes；PK(snapshot_id,strategy,stock_code,horizon) | snapshot_id TEXT；strategy TEXT；recommend_date TEXT；stock_code TEXT；horizon INTEGER；status TEXT；settled_at TEXT；payload_hash TEXT；payload BLOB | recommendation_outcomes；stock_code→code；recommend_date→freeze_date；payload_hash→record_hash BLOB；payload→record_bytes BLOB；其余保留，snapshot_id不猜作freeze_id。 |

**追踪新owner：[recommendation/infra/persistence/trace_schema.py](../src/trader/recommendation/infra/persistence/trace_schema.py)**

C；改造前实现位于training，写入owner迁入recommendation；training的research_trace_index保留只读研究职责。消费者：事件sink、冻结发布、研究追踪及诊断；原写入路径删除。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| committed_events；PK(原冻结版本) | 原冻结版本 TEXT；strategy TEXT；trade_date TEXT；observed_at TEXT；原冻结摘要 TEXT；schema_version TEXT；payload_hash TEXT；payload BLOB | committed_events；原冻结版本→freeze_id；payload_hash→event_hash BLOB；payload→event_bytes BLOB；原冻结摘要→freeze_hash BLOB；其余保留，新规范事件统一freeze字段；隔离旧字节不改写。 |
| committed_event_quarantine；PK(原冻结版本) | 原冻结版本 TEXT；reason TEXT；payload_hash TEXT；payload BLOB | committed_event_quarantine；原冻结版本→freeze_id；payload_hash→event_hash BLOB；payload→event_bytes BLOB；reason保留原隔离原因。 |

**[training/infra/research/h1_point_in_time_snapshot.py](../src/trader/training/infra/research/h1_point_in_time_snapshot.py)**

C；training保持研究owner。消费者：h1_point_in_time入口及研究资格/样本读取；不混入生产冻结。具名表/列codec保持同一新规范对象的摘要与算法；freeze_anchor等规范字段改名生成新的研究规格摘要，不复用旧格式证据。h1_price增加utc_offset_us保存原时区偏移表示，避免UTC微秒往返改变新规范摘要。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| specs；PK(strategy) | strategy TEXT；research_identity TEXT；spec_hash TEXT | h1_spec；research_identity→research_id TEXT；spec_hash→同名BLOB；strategy保留。 |
| universe；PK(strategy,code) | strategy TEXT；code TEXT；board TEXT；name TEXT；is_st INTEGER；is_suspended INTEGER；payload_hash TEXT | h1_universe；payload_hash→content_hash BLOB；其余保留。 |
| records；PK(strategy,code,trade_date) | strategy TEXT；code TEXT；trade_date TEXT；observed_at TEXT；open_price REAL；close_price REAL；high_price REAL；low_price REAL；volume REAL；amount REAL；pct_change REAL；turnover_rate REAL?；adjustment TEXT；source TEXT；anchor_price REAL；anchor_volume REAL；anchor_amount REAL；security_state_hash TEXT；sector_hash TEXT；risk_facts_hash TEXT；tail_field_hash TEXT；payload_hash TEXT | h1_price；open_price/close_price/high_price/low_price→open/close/high/low；pct_change→change_pct；turnover_rate→turnover_pct；payload_hash→content_hash BLOB，所有*_hash改BLOB；其余保留，adjustment明确该行口径，anchor_*保留点时锚定含义。 |
| downloads；PK(strategy,code) | strategy TEXT；code TEXT；status TEXT；record_count INTEGER；content_hash TEXT；error_code TEXT | h1_download；error_code→failure_code；content_hash→同名BLOB；strategy/code/status/record_count保留。 |

**[training/infra/research/historical_screening_snapshot.py](../src/trader/training/infra/research/historical_screening_snapshot.py)**

C；training保持研究owner。消费者：历史筛选/选席研究和资格读取。删除旧表名与行codec，保留研究人口和每个身份隔离。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| specs；PK(research_identity) | research_identity TEXT；spec_hash TEXT | screening_spec；research_identity→research_id TEXT；spec_hash→同名BLOB。 |
| universe；PK(research_identity,code) | research_identity TEXT；code TEXT；board TEXT；name TEXT；is_st INTEGER；is_suspended INTEGER；payload_hash TEXT | screening_universe；research_identity→research_id；payload_hash→content_hash BLOB；其余保留。 |
| bars；PK(research_identity,code,trade_date) | research_identity TEXT；code TEXT；trade_date TEXT；open_price REAL；close_price REAL；high_price REAL；low_price REAL；volume REAL；amount REAL；pct_change REAL；turnover_rate REAL?；adjustment TEXT；source TEXT；payload_hash TEXT | screening_price；research_identity→research_id；open_price/close_price/high_price/low_price→open/close/high/low；pct_change→change_pct；turnover_rate→turnover_pct；payload_hash→content_hash BLOB；其余保留。 |
| downloads；PK(research_identity,code) | research_identity TEXT；code TEXT；status TEXT；bar_count INTEGER；content_hash TEXT；error_code TEXT | screening_download；research_identity→research_id；error_code→failure_code；content_hash→同名BLOB；code/status/bar_count保留。 |

**[training/infra/research/historical_residual_ledger.py](../src/trader/training/infra/research/historical_residual_ledger.py)**

C；training保持研究owner。消费者：historical_residual_ledger用例、研究证据/资格；真实预测与结算保持独立，禁止将回算写成原冻结。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| historical_prediction；PK(identity) | identity TEXT；strategy TEXT；trade_date TEXT；code TEXT；horizon INTEGER；model_hash TEXT；parent_split_hash TEXT；content_hash TEXT；payload TEXT | historical_prediction；identity→prediction_id TEXT；payload→prediction_json TEXT；model_hash/parent_split_hash/content_hash→同名BLOB；strategy/trade_date/code/horizon保留，codec边界继续保存不可变研究证据。 |
| historical_outcome；PK(identity) | identity TEXT；strategy TEXT；trade_date TEXT；code TEXT；horizon INTEGER；parent_split_hash TEXT；content_hash TEXT；payload TEXT | historical_outcome；identity→outcome_id TEXT；payload→outcome_json TEXT；parent_split_hash/content_hash→同名BLOB；其余保留。 |

**[training/infra/profile/v1/samples.py](../src/trader/training/infra/profile/v1/samples.py)**

F；training保持owner。消费者：V1样本生成、双头training/矩阵读取、资格与诊断。删除features打包列及旧reader，使用下表固定十列，模型内部feature ID不变。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| samples；PK(day,code) | day TEXT；code TEXT；industry TEXT；features BLOB；target_t1 REAL?；target_d25 REAL? | samples；day→trade_date INTEGER；features→第15.3节十个具名REAL列及zero_signs INTEGER；code/industry/target_t1/target_d25保留。 |

**[training/infra/sample_index.py](../src/trader/training/infra/sample_index.py)**

F；training保持owner。消费者：sample_builder/cache、engine、selected_training、model_fitting、V3样本索引、V5 batch_cache/fitting及诊断。删除位置列SQL及位置名猜测，读出数组顺序显式绑定head manifest。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| samples；PK(trade_date,code) | trade_date TEXT；code TEXT；board TEXT；industry TEXT；average_amount_20d REAL；动态列（见15.3）；target_t1 REAL?；target_t2 REAL?；target_t3 REAL?；target_t4 REAL?；target_t5 REAL?；target_d25_aggregate REAL? | samples；trade_date→INTEGER；average_amount_20d→avg_amount_20d；动态f列及sequence_start见第15.3节；其它列保留，标签仍允许NULL且按原合同过滤。 |
| sample_split_dates；PK(trade_date) | trade_date TEXT；split_name TEXT | sample_split_dates；trade_date→INTEGER，其余保留，split_name枚举及互斥日期分区不变。 |
| sample_statistics；PK(trade_date,industry,target) | trade_date TEXT；industry TEXT；target TEXT；rows INTEGER；total REAL；squares REAL | sample_statistics；trade_date→INTEGER；rows→sample_count INTEGER；total→target_sum REAL；squares→target_sum_squares REAL；trade_date/industry/target保留，汇总仍按原标签单位。 |
| sample_day_diagnostics；PK(trade_date) | trade_date TEXT；candidates INTEGER；missing_t1 INTEGER；missing_t2 INTEGER；missing_t3 INTEGER；missing_t4 INTEGER；missing_t5 INTEGER | sample_day_diagnostics；trade_date→INTEGER；candidates→candidate_count INTEGER；其它列保留，missing_t1～missing_t5为计数。 |
| sample_sequence_blocks；PK(code,block) | code TEXT；block INTEGER；payload BLOB | sample_sequence_blocks；block→block_id INTEGER；payload→sequence_bytes BLOB；code保留，维度与字节序见第15.3节。 |

**[recommendation/infra/deepseek/budget.py](../src/trader/recommendation/infra/deepseek/budget.py)**

D；recommendation统一内存状态及原子文件owner。消费者：预算预留/完成、批次、状态、review编排、bootstrap及统一诊断；全部旧SQL/连接回调/预算库路径删除，字段去向见第15.4节。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| schema_meta；PK(key) | key TEXT；value TEXT | 删除SQL；state.json格式头；key/value删除；schema_version改固定文件字段，不保留通用键值表。 |
| deepseek_call_reservations；PK(reservation_id) | reservation_id TEXT；trade_date TEXT；strategy TEXT；bucket TEXT；phase TEXT；stage_key TEXT；batch_id TEXT；emergency_reason TEXT；requested_at TEXT；completed_at TEXT?；status TEXT；error TEXT；http_status INTEGER?；latency_ms REAL?；token_count INTEGER；timed_out INTEGER；model_role TEXT；requested_model TEXT?；actual_model TEXT?；reasoning_effort TEXT?；system_fingerprint TEXT?；finish_reason TEXT?；prompt_tokens INTEGER；completion_tokens INTEGER；prompt_cache_hit_tokens INTEGER；prompt_cache_miss_tokens INTEGER | requests；reservation_id→request_id；trade_date→budget_date；error→failure_code；token_count→total_tokens；其余字段按第15.4节映射，仅保存当日计费/恢复所需事实。 |
| deepseek_calls；PK(call_id) | call_id TEXT；strategy TEXT；phase TEXT；model TEXT；batch_id TEXT；requested_at TEXT；completed_at TEXT?；http_status INTEGER?；prompt_tokens INTEGER?；completion_tokens INTEGER?；latency_ms REAL?；outcome TEXT；error_code TEXT；model_role TEXT；requested_model TEXT?；actual_model TEXT?；reasoning_effort TEXT?；system_fingerprint TEXT?；finish_reason TEXT?；total_tokens INTEGER；prompt_cache_hit_tokens INTEGER；prompt_cache_miss_tokens INTEGER | 并入requests完成结果；call_id仅作旧关联键，由request_id统一；outcome/error_code→status/failure_code；model与requested_model/actual_model去重但保留请求/实际模型区别；其余统计字段归对应request，不另存一份调用表。 |
| deepseek_review_batches；PK(batch_id) | batch_id TEXT；trade_date TEXT；strategy TEXT；phase TEXT；bucket TEXT；model TEXT；requested_at TEXT；deadline TEXT；completed_at TEXT?；status TEXT；candidate_count INTEGER；cache_hit_count INTEGER；physical_attempts INTEGER；error TEXT | review_batches；trade_date→budget_date；deadline→deadline_at；error→failure_code；其它必要批次字段保留，由第5节限定大小与生命周期。 |
| deepseek_candidate_results；PK(batch_id,stock_code) | batch_id TEXT；stock_code TEXT；outcome TEXT；completed_at TEXT?；error TEXT | 批次有界结果；stock_code→code；error→failure_code；其余保留在当前批次内存投影，结束后仅保留恢复所需结果/计数，不无限累计股票明细。 |

**[recommendation/infra/deepseek/health_gate.py](../src/trader/recommendation/infra/deepseek/health_gate.py)**

D；recommendation与预算服务共享原子状态，不新增第二文件。消费者：review健康门、半开探测与状态投影；全部旧健康SQL和连接回调删除。

| 当前表 / 主键 | 当前字段与声明类型 | 目标表及字段处理 |
| --- | --- | --- |
| deepseek_health_events；PK(batch_id) | batch_id TEXT；trade_date TEXT；completed_at TEXT；transport_failed INTEGER；schema_failed INTEGER；candidate_count INTEGER；accepted_count INTEGER | health_state有界恢复窗口；trade_date→budget_date；其余字段沿用健康窗口逻辑，过窗事件可丢弃，不能由此删除已计费请求。 |
| deepseek_health_state；PK(trade_date) | trade_date TEXT；mode TEXT；open_until TEXT?；probe_batch_id TEXT；recovery_successes INTEGER；reason TEXT；updated_at TEXT | health_state；trade_date→budget_date；open_until→open_until_at；其余保留，时刻用于跨进程恢复，实际等待仍使用单调时钟。 |

### 15.3 动态训练特征：位置列到具名列

V1以[固定特征ID](../src/trader/recommendation/domain/market/v1/features.py)及实际样本reader为依据；V2/V3/V5以[特征合同](../src/trader/recommendation/domain/market/feature_contracts.py)、本档head manifest及sample_builder写入后的值为依据。下表仅改变SQLite列名，模型feature ID、计算、排列、dtype、缺失处理和训练身份保持。V2不能误用残差处理前的八项raw manifest替代实际写入的十项head manifest。

| V1 features内位置 | 当前模型特征ID | 目标REAL列 |
| --- | --- | --- |
| 0 | completed_return_1d | return_1d |
| 1 | completed_return_3d | return_3d |
| 2 | completed_return_5d | return_5d |
| 3 | completed_return_20d | return_20d |
| 4 | completed_return_60d | return_60d |
| 5 | completed_return_120d | return_120d |
| 6 | completed_return_250d | return_250d |
| 7 | completed_volatility_20d | volatility_20d |
| 8 | completed_drawdown_60d | drawdown_60d |
| 9 | completed_log_average_amount_20d | log_avg_amount_20d |

样本格式固定为2，所有trade_date为INTEGER天数。V1增加singleton/feature_hash身份表，V2/V3/V5的sample_contract保存singleton、schema_version、feature_hash及完成准备后的split_hash；摘要均为32字节BLOB，旧格式与错误身份不复用。

V1另有zero_signs INTEGER十位负零位图（0～1023），只保留SQLite REAL会丢失的负零符号；置位的特征必须为0，读回为-0.0，不允许改变其它数值。V1上述字段均基于已完成日线；去掉存储列的completed前缀不准引入当日未完成报价。return/volatility/drawdown保持原定义的小数尺度，log_avg_amount_20d保持原对数变换，不能把对数值当成元或再做一次对数。

| 当前位置列 | 目标REAL列（与实际head特征名一致） | 适用档 |
| --- | --- | --- |
| f0 | qfq_return_1d | V2/V3/V5辅助列 |
| f1 | qfq_return_3d | V2/V3/V5辅助列 |
| f2 | qfq_return_5d | V2/V3/V5辅助列 |
| f3 | qfq_residual_momentum_20d_skip5 | V2/V3/V5辅助列 |
| f4 | qfq_residual_momentum_40d_skip5 | V2/V3/V5辅助列 |
| f5 | qfq_residual_momentum_60d_skip5 | V2/V3/V5辅助列 |
| f6 | qfq_residual_momentum_120d_skip5 | V2 |
| f7 | qfq_residual_momentum_250d_skip5 | V2 |
| f8 | market_qfq_momentum_120d_skip5 | V2 |
| f9 | market_qfq_momentum_250d_skip5 | V2 |

残差动量的窗口、skip5及市场项具有不同计算含义，保留必要限定，不能为缩短而统一成momentum。V5不是366个SQLite特征列：六个辅助REAL列加sequence_start INTEGER，60×6序列由sample_sequence_blocks保存重叠块。通道固定open_return、high_return、low_return、close_return、log_volume、log_amount，沿用float64小端字节、既有块行数与偏移校验；payload改名sequence_bytes，不增设360份重复列。V1原features字节列删除，V5有意义的序列BLOB保留，两者不得机械套用同一“全部拆列”规则。

### 15.4 DeepSeek旧列到临时状态

第15.2节穷举七张旧SQL表字段；D包删除这些表，不把它们原样复制成七组JSON记录。第5节的requests是计费和恢复唯一事实，每次物理预留一个request_id，最多168条当日记录；reservation_id/call_id在新格式统一关联到它。strategy、bucket、phase、stage_key、batch_id及emergency_reason保留各自含义，trade_date改budget_date且明确上海自然日；跨档实例身份另外由组合根显式提供，不能由策略名猜档位。

requested_at/completed_at保存请求时点；status/failure_code保存有界类别，http_status、latency_ms、timed_out保存诊断事实。total_tokens、prompt_tokens、completion_tokens、prompt_cache_hit_tokens、prompt_cache_miss_tokens分别保留其口径，不能把缺失token记为0。model_role、requested_model、actual_model、reasoning_effort、system_fingerprint、finish_reason保留必要完成信息，旧model别名合并到明确的请求/实际模型字段，不保存自由文本error或响应正文。计数同一事务从请求推导/校验，禁止单独重建calls副本。

review_batches保留当前恢复需要的batch_id、budget_date、strategy、phase、bucket、model、requested_at、deadline_at、completed_at、status、candidate_count、cache_hit_count、physical_attempts及failure_code；已结束的无HTTP重复批次不持久累积。candidate结果的code/outcome/completed_at/failure_code在有界批次内使用，持久文件只收录确有恢复需求的结果和计数。health事件保留恢复窗口内的batch_id、budget_date、completed_at、transport_failed、schema_failed、candidate_count、accepted_count；health_state保留mode、open_until_at、probe_batch_id、recovery_successes、reason、updated_at。预算用量不能因候选或健康明细过期被回收。

### 15.5 实现时的对照入口

B包从第2.4节新参考关系及history_control领域对象展开旧payload_json：source的来源/数据集/供应商合同/观察时刻，calendar的开市日成员及来源引用，universe的证券版本成员，checkpoint的同步ID/ordinal/state/完成与总量/失败类别，snapshot的同步序号/截止日/源与参考引用/年度分区及月证明。每种对象有独立表和关系，不用通用kind/key/value或一份新JSON替代。绑定缺失和同ID异内容必须失败，跨库发布顺序保持先参考版本、后catalog可见指针。

盘点可复查各链接源码的CREATE TABLE、PRICE_SQL/PRICE_WINDOW_SQL、动态feature_columns以及对应行codec；研究临时查询表和索引另按其实际创建路径核对。实现包开始时重新搜索这些owner的导入和端口调用，再核对tests、scripts、DataPaths、bootstrap、存储配置及只读投影。当前清单证明字段有去向，实际新schema及全部消费者切换仍由B/C/D/E/F交付，不能将本节目标名称标为已经生效。
