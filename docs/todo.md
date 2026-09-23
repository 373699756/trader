 蓝图中共有 9 个显式 #TODO。建议依次按下面的结论处理，并用正式合同替换疑问句。

  1. 数据为什么一开始就被过滤很多

  docs/项目重构详细.md:8 应明确：

  - 数据源层不允许过滤股票，只统计预期、收到、缺失、失败和过期。
  - 每层满足守恒关系：输入 = 输出 + 业务拒绝 + 待补充 + 失败。
  - 只有一级稳定过滤、二级动态过滤能产生业务拒绝。
  - 历史、证券身份或行情缺失进入 data_pending/refresh_pending，不能计入淘汰。
  - 静态数据不是“进程启动后永不更新”，而是启动加载有效快照、后台按交易日或事实变更刷新；每轮评分绑定一个不可变静态快照。
  - amount_median_20d 缺失是历史输入未就绪，不是评分结果。字段存在但低于流动性门槛时，才由二级动态过滤拒绝；字段缺失时应实时恢复，失败则标记 history_data_pending。

  2. scripts/ 如何精简

  docs/项目重构详细.md:16 应规定三类脚本：

  - 统一诊断入口：scripts/diagnose_runtime.py:1，具体探针只能放在 scripts/runtime_diagnostics/。
  - 一次性迁移和归档维护，例如历史转换、repack、运行数据迁移；迁移完成后删除。
  - 发布门禁，例如 wheel 验证、重构质量检查。

  重复查询状态、重复供应商探测、只包装另一个命令、已退役 Today/V1 脚本全部删除。可重复的下载、训练和研究业务命令应迁入 trader-cli，不能由脚本成为第二业务 owner。每个保留脚本必须声明是否联网、是否写入、输
  出位置和资源上限。

  3. 发布后推送 Web，还是 Web 主动查询

  docs/项目重构详细.md:48 应采用混合方案：

  - 页面首次打开通过 GET 获取完整已发布快照。
  - 后续通过 SSE 主动推送 decision、overlay 和 resync_required。
  - 浏览器保存事件游标；断线重连后由服务端重放有限窗口。
  - 游标过期、事件缺口或客户端过慢时，执行一次完整 GET 重同步。
  - SSE 不可用时才启动有界轮询，连接恢复后立即停止轮询。

  这套机制当前已经存在于 src/trader/http_api/decision_sse.py:21、src/trader/recommendation/application/pipeline/freeze_publish/event_stream.py:111 和 src/trader/web/static/dashboard_stream.js:36，因此该
  TODO 应改成正式合同，而不是重新设计另一套推送机制。

  4. Web 是否只在发布节点更新

  docs/项目重构详细.md:53 应拆成两条状态线：

  - 推荐业务快照：只有完整评分、选择、冻结或合法 current 发布完成后，才原子替换；不得把两个运行批次的数据拼在一起。
  - 运行诊断状态：可以更新，但只在某一层完整结束并形成不可变 PipelineStageSnapshot 后发布，禁止逐股、逐字段刷新页面。

  每次运行必须携带 run_id/publication_id。浏览器只接受同一运行中递增的阶段快照，最终发布时一次性切换推荐内容和对应的 14 层审计。冻结后的报价 overlay 可以单独更新价格，但必须校验父快照身份，不能改变分数、
  动作和排名。

  5. entrypoints/ 是否并入 bootstrap.py

  docs/项目重构详细.md:262 的结论应是：保留，不能合并。

  - entrypoints/cli.py：命令行解析和业务命令分派。
  - entrypoints/server.py：进程锁、信号、HTTP 监听和关闭生命周期。
  - bootstrap.py：唯一组合根，只负责装配依赖和构建系统。

  公开入口也已经由 pyproject.toml:36 固定。后续应继续把 CLI 中的具体业务实现下沉到各业务 entrypoints/commands.py，使顶层入口变薄，而不是把进程控制塞进组合根。

  6. 整体目录结构是否合理

  docs/项目重构详细.md:268 的总体划分合理：

  download / training / recommendation   三个业务
  http_api / web                         推荐的只读交付边界
  infra                                  跨业务技术 primitive
  entrypoints                            两个公开进程入口
  bootstrap.py                           唯一组合根

  验收重点不是继续增加目录，而是确保每个职责只有一个 owner、应用层不反向导入 infra、HTTP/Web 不执行业务计算、旧目录原子删除。

  7. artifacts 命名不清楚

  docs/项目重构详细.md:367 建议改名为：

  training/domain/model_bundle/
  training/infra/model_bundles/

  其中 infra 目录只包含 bundle_codec.py、bundle_repository.py、bundle_locator.py 和发布恢复逻辑。训练研究报告、回测证据不能混入该目录。重命名必须在阶段 3 原子更新所有导入、测试和包资源，不保留兼容转发目
  录。

  8. qfq 缺失导致整条链路卡住

  docs/项目重构详细.md:503 应固定为：

  活动历史归档
  -> 命中则直接使用
  -> 仅对缺失窗口的股票实时 qfq 恢复
  -> 恢复失败则逐股 history_data_pending

  实时恢复必须有 deadline、波次、并发和数量上限；结果只进入当前 feature batch，不写归档。不得把 raw 数据伪装成 qfq，也不得因为少数股票缺历史阻断其他股票。

  这部分当前已经由 src/trader/recommendation/infra/market_data/history_recovery.py:35 和 src/trader/recommendation/infra/market_data/published_history_cache.py:132 实现。后续重点是验证恢复覆盖、截止时间和
  失败原因投影。

  9. 已有 2000 日历史时如何快速增量更新

  docs/项目重构详细.md:511 应采用双路径：

  - 已有完整历史：按交易日计算缺口，只请求缺失的 日期 × 股票；结果先进入临时批次，统一校验后按月份批量事务写入，最后一次性发布新 active manifest。
  - 没有完整历史：保持逐股完整回补、checkpoint 和断点续传，不能把全市场 2000 日数据放进内存。

  “某一天一次拉取 5000 只”只有供应商提供且验证通过全市场日接口时才能使用。BaoStock 当前仍是逐代码请求，因此可以优化为有界并发请求和批量落库，但不能声称一次网络请求能取得全市场。任何部分失败都不能推进游标
  或发布半完成快照。

  另外有两个关联矛盾应同步修正：目标树写的是 data/history/，但历史章节仍引用 data/history/baostock；冻结说明中仍有“15:00 冻结”，权威合同应为 14:50 first-wins、15:00 后仅允许缺失记录的 close_fallback。本轮
  没有修改用户正在编辑的文档。

Review 发现

  1. 最高优先级：当前第 2–7 层不是真实生产流水线。

     load_static_market()、normalize_static_market()、filter_permanent_eligibility()、build_dynamic_market_snapshot()、normalize_dynamic_market()、filter_dynamic_market()只有定义，没有生产调用方。真实路径
     直接在 src/trader/recommendation/infra/market_data/market_feature_service.py:94 中执行“行情 → 一级名单 → 历史 → FeatureSnapshot”。

     前九层随后由 src/trader/recommendation/application/pipeline/quality_check/pipeline_status.py:152 用数量差值反推。静态/动态清洗直接显示输入数等于输出数，耗时固定为 0。

     所以“清洗没有失败”目前不能证明清洗稳定，只能说明没有真实的阶段结果可报告。

  2. 静态阶段的类型设计已经倒置。

     静态采集、静态清洗、一级过滤都接收完整 FeatureSnapshot，例如 src/trader/recommendation/application/pipeline/static_market/static_market_loader.py:14 和 src/trader/recommendation/application/pipeline/
     static_filter/permanent_filter_service.py:19。但 FeatureSnapshot 已经包含历史和衍生特征。

     一级过滤本应在昂贵的逐股历史、研究、分钟数据请求之前。现有独立阶段接口与这个目标相反。

  3. 历史实时恢复存在，但冷启动仍可能产生顺序偏差。

     当前已恢复“归档没有历史时实时拉取”的路径：src/trader/recommendation/infra/market_data/published_history_cache.py:132。

     但一次最多处理 120 只，直接截取缺失列表前 120 只，见 src/trader/recommendation/infra/market_data/history_recovery.py:78。结果只保留 15 分钟内存缓存，没有持久 checkpoint。全市场历史缺失时，候选人口容
     易受代码顺序、运行周期和 TTL 影响，无法保证形成完整公平的横截面。

  4. 数据完整度被当作正向 alpha，并受到多次影响。

     完整度既占候选分 5.88%–14.29%，见 config/strategy.json:43，又参与 30% 缺失门槛、可靠度排序和评分资格。

     这与权威文档“数据质量只能作资格、风险或诊断，不能获得正向 alpha”的优化边界冲突，见 docs/01_评分逻辑.md:638。更完整的数据不等于股票预期收益更高，可能系统性偏向覆盖好的大盘股。

  5. 成本门不是完整交易成本。

     当前成本只是：

     0.002 × (1 + 当前候选批次内 Amihud 分位)

     见 src/trader/recommendation/application/pipeline/local_score/model_scoring.py:508。

     同一股票会因候选集合变化得到不同成本；没有仓位、参与率、最低佣金、印花税、买卖双边、滑点和退出容量。它可以作为现行代理，但不能据此声称成本后收益可靠。

  6. 永久资格把不同性质的风险混为一类。

     单年年报亏损、任一历史 ST、重大违法、强退都进入同一个不可逆追加名单，见 src/trader/recommendation/domain/market/eligibility.py:20 和 src/trader/recommendation/infra/persistence/
     issuer_eligibility.py:32。

     违法、造假、强退适合永久处理；经营亏损和已经解除的 ST 是否永久排除需要独立验证，否则会长期丢失扭亏反转机会。

  7. 动态风险采用“任意非零即拒绝”，过于粗糙。

     解禁、质押、减持阈值都是 0，见 config/strategy.json:28。实现只判断 value > threshold，见 src/trader/recommendation/domain/candidate/filters.py:374。

     1% 解禁与 50% 解禁、低比例质押与高比例爆仓风险不应完全等价。应按比例、期限和变化速度区分 reject、observe、penalty。

  8. 选择逻辑存在重复 owner 和合同矛盾。

     本地评分阶段已经执行一次 TopK/行业限制，见 src/trader/recommendation/domain/selection/scored_selection.py:713，最终融合后又执行正式 TopK/集中度，见 src/trader/recommendation/domain/risk/
     scored_fusion.py:544。前一次结果基本只留下诊断原因，容易造成漏斗归因错误。

     此外，评分权威要求三板合并后的全局 Top6且集中度后不补位，见 docs/01_评分逻辑.md:538；重构蓝图却写成每个板块组独立 TopK并补位，见 docs/项目重构详细.md:1327。按仓库规则，评分与选择应以 01_评分逻辑.md
     为准。

  9. 14、15、16阶段三种口径同时存在。

     领域枚举是 14 层，src/trader/recommendation/domain/evidence/pipeline.py:14；评分文档又写“15阶段审计”和“前十+后六”，见 docs/01_评分逻辑.md:558。这不会直接改分，但会破坏冻结审计和收益损失归因。

  10. 当前没有可靠证据支持直接调权。

  活动 V2 工件仍明确标记 point_in_time_parity=false、historical_data_insufficient、production_authority=false。权威文档也确认当前只有 15:00 收盘代理，不能证明 14:50 真实收益，见 docs/01_评分逻辑.md:821。

  可执行优化计划

  1. 先修链路真实性，不改任何策略参数。

     串联真实 RawBatch → NormalizedBatch → FeatureBatch → EligibilityBatch；一级过滤输入轻量静态对象，并前置到历史/研究/分钟请求之前。删除前九层差值反推，每层直接输出逐股终态、首个失败原因、真实 latency
     和连续 batch identity。

  2. 采集与清洗执行融合。

     同一次 provider 请求、缓存读取和 deadline 内完成解析、归一化和特征构建，避免重复遍历和 I/O；但仍分别发布采集、清洗、特征结果。历史缺失先读正式归档，再做候选优先的有界实时恢复，同时由下载业务持久回补
     并支持 checkpoint。

  3. 统一唯一阶段和选择合同。

     固定 14 个主阶段，细节作为 typed substage metrics；删除“15阶段”和“前十+后六”的第二编号。移除本地评分阶段提前执行的 TopK/集中度，最终选择只保留一个 owner；修正 D25 全部成本门失败时未归类为
     no_positive_net_utility 的诊断问题。

  4. 建立收益归因基础后再研究参数。

     补齐真实 14:50 分钟锚点、行业/资格 effective_at、可交易标签和 CandidateRecallLedger。逐层统计 oracle Top10/20/50 首次损失、成本后净超额、严重亏损、MAE、回撤、换手、容量与不可交易率。

  5. 预注册消融，不直接改生产。

     依次比较：完整度正向权重与仅作资格；永久亏损/ST与连续盈利重新准入；动态风险 reject/observe/penalty；每板 60/120/180/240/full；当前成本代理与绝对 ExecutionCost；local-only、facts-only veto、固定
     68/32；Top6、板块60%、行业2和动作阈值。

  6. 通过确认集、终端留出、Shadow 后单项切换。

     每次只切换一个已验证因素，不能同时修改过滤、评分、成本、DeepSeek和TopK。任一净收益置信下界、严重亏损、最大回撤、换手、容量或市场状态门禁失败，就保留现规则。


