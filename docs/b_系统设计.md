# 系统设计

本篇说明三条业务如何隔开、公共设施如何复用、数据库怎样读写、配置和页面怎样组织。评分字段归 c，下载库和来源归 d，训练样本和工件归 e。本篇不重复维护它们的业务字典。

本篇分别说明现行实现、目标约束与待实测候选；实施状态及证据以[g_施工进度](g_施工进度.md)为准。已有能力不能因为换了文档就算重新完成；候选参数也不能直接当作生产设置。

## 1. 三条业务的输入、输出和边界

| 业务 | 入口 | 输入 | 输出 | 明确的边界 |
| --- | --- | --- | --- | --- |
| download | 人工 `sync history/market/company` | 数据源、已提交断点、下载配置 | 可用资料版本、覆盖和失败记录 | 不训练、不评分、不触发 Web 更新用例 |
| training | 人工选择四档训练 | 固定资料版本、训练配置 | 各档双头模型和评价报告 | 不自动下载，不发布推荐 |
| recommendation | 本地服务启动 | 已发布资料、模型、实时行情 | 不可变推荐批次和只读状态 | 不补历史、不训练、不日更新 |
| http_api、web | 浏览器请求和事件订阅 | recommendation/application 只读用例 | JSON 白名单和桌面展示 | 不抓取、评分、访问业务数据库或写业务状态 |

荐股和三个下载模块都先读取全部股票的代码、名称和ST状态，排除当前ST及已有曾ST记录后，才加载或采集其他个股资料。下载按用户最新要求允许unknown继续采集，保留待核标记和后续核验，不将其改写为clear。荐股从本地已发布资料读取ST状态，状态不明仍待核，缺失时由用户手动执行下载；允许下载不代表荐股已核实资格通过。两者不互相调用业务实现，被排除、已核实、待核分别计数。

`train`只调用`bootstrap.ExecuteSelectedTraining`，读取已经发布的本地资料；缺资料时提示用户手动执行`./run.sh sync history`，不代为下载。原自动组合函数`ExecuteDownloadTrain`删除，`run.sh train`继续转发同一个CLI入口。训练的线程数和进程优先级也由组合根准备。

正常训练由组合根一次加载下载owner的最新有效`CurrentIndustrySnapshot`，四档双头按这份映射处理全部价格历史；正常history不再准备历史行业。训练输入及复用同时绑定价格和行业语义，刷新时间不冒充语义变化，缺失／过期／损坏明确拒绝。日期预检、样本事实及模型工件合同归[e第1、3、7章](e_训练设计.md#1-输入固定双头独立)，独立点时收益研究仍须另具历史证据。

## 2. 模块与依赖

```mermaid
flowchart TD
    B[bootstrap 唯一组合根] --> E[业务入口]
    B --> I[业务 infra 适配器]
    E --> A[业务 application 用例和端口]
    I --> A
    A --> D[业务 domain 纯规则]
    I --> P[公共 infra 技术能力]
    H[http_api 与 web] --> R[recommendation application 只读用例]
```

domain 不读配置、时间、网络、文件或数据库。application 依赖明确端口和真实类型，不导入本业务 infra、Flask 或其它业务实现。适配器不暗中创建客户端、连接池或线程池，由组合根注入并负责释放。`CreateApp()` 只组装应用，无线程、联网、建库、写文件副作用。

无I/O的跨业务技术primitive按具名合同复用：SqliteActivity、SqliteObservation、IndependentSqliteWork只提供上下文内计数和独立工作隔离，不创建连接或执行SQL。应用层与全仓架构测试共用同一导入判定，观测模块也只开放这三个符号；sqlite_connection/session、业务adapter和隐藏I/O仍禁止，不通过扩大模块白名单授予数据库能力。

跨业务交接是有明确类型的不可变对象或已发布工件。JSON 只在配置、供应商解析、存储编码和公开投影边界出现，不用 `dict`、`Any`、反射或空对象掩盖内部合同。活动源码单文件最多1500行，按职责拆分。

| 公共能力 | 唯一职责 | 不应放入的内容 |
| --- | --- | --- |
| 时钟 | 上海业务时间、单调 deadline | 自动判断某业务该下载什么 |
| HTTP 传输 | 超时、取消、重试计数、连接回收 | 财务语义、公司风险判定 |
| SQLite primitive | 连接、事务、锁、介质参数、观察计数 | 各业务 schema、SQL、发布资格 |
| 执行器 | 有界任务、背压、停止、等待和在途统计 | 隐式跨业务编排 |
| 配置解析 | 一次解析为类型对象并注入 | 每层自行读配置、复制默认值 |

顶层`infra/settings`只持有运行配置与技术解析。荐股策略、Long清单、因子和风险配置由`recommendation/infra/settings`持有，训练资源由`training/infra/resource_settings`解析；旧业务配置路径删除，消费者直接使用各owner。特征schema归`recommendation/domain/market/feature_schema`，配置校验不依赖特征构建实现；下载当前行业准入使用自身domain合同，不构造荐股模型对象。

历史日线值、价格基准和会话类型，以及均线、ATR、波动、回撤、Amihud与历史上下文计算，统一由`recommendation/domain/market/history_metrics.py`持有；供应商、发布历史和缓存adapter只负责事实解析与投影。原公共infra历史算法路径及导出删除，全部消费者直接使用新owner；公式、单位、窗口、完成日和停牌语义不因迁移变化。源码进入domain后纳入原训练实现摘要，复用合同变化须如实保留，不重贴旧模型或冻结身份。

`HttpRequestScope`仅接收显式HTTP工厂，管理单次请求的打开和关闭；`WorkerScope`仅管理root注入的执行器，部分启动失败和正常退出均以同一截止回收已启动资源。资源构造仍由`bootstrap.py`持有。HTTP关闭先取消并拒绝新请求，锁忙显式报告pending，在途调用的finally负责最终关闭；请求关闭与完成关闭分开观察，避免执行器已超时后又无期限等待HTTP锁。

## 3. 启动、运行和退出

```mermaid
flowchart TD
    B[程序启动] --> S[读取股票名单和ST状态]
    S --> F[先ST过滤 再处理其他资格]
    F --> P[只为合格股票预加载资料并准备模型]
    P --> I[全部启用档位初始化完成]
    I --> R[采集并锁定批次 整轮最多120秒]
    R --> W[Web一次替换全部短线结果]
    W --> N{是否请求停止}
    N -->|否| R
    N -->|是| C[禁止新任务 取消并回收在途任务]
    C --> E[报告退出结果或具体失败]
```

资源表必须列创建者、使用者、停止动作、等待上限和状态。连接、进程、线程、文件映射、队列及 OCR 子进程均不能只“发出取消”却不回收。超时轮次彻底退出前不得开始下一轮；清理耗时独立记录，不能悄悄算作正常120秒内成功。

荐股执行资源已由`config/scheduler.json.pipeline.executors`启动解析并交`bootstrap._build_worker_context`创建。默认并发保持原值，运行配置身份记录有效参数；模型特征/训练manifest不因改变线程数而更换。`CreateApp`和系统组合不启动这些资源。以下是A本阶段核对的实际生命周期；下载/训练/OCR资源仍由各自owner验收，实测CPU/RSS/I/O由g第4.5节的CodexC内部唯一后台承接。

| 资源 | 创建/启动与唯一owner | 停止、等待与状态 |
| --- | --- | --- |
| 每档control池 | 组合根创建；SchedulerRuntime启动，normal/urgent独立；每档各一份 | RuntimeSupervisor → SchedulerRuntime按统一ShutdownDeadline停池；BoundedExecutorStatus报告在途/拒绝/完成/运行状态 |
| source_data及两个报价池、分钟池 | 组合根创建，第一档拥有，后续档共享；ApplicationResources.start启动 | 源lane先禁新请求再等待；共享池仅第一档stop，各自回收排队Future并按同一deadline等待在途；后续档不重复关闭 |
| HTTP隔离进程池 | 组合根创建，第一档启动；请求时惰性spawn进程并独占transport | stop先停止接收/发取消，等待并terminate/kill/join自有进程；未回收不归还并发名额，ShutdownStep报告未完成 |
| company_exclusions单writer池 | 组合根固定1 worker/1排队，CompanyEligibility启动；它是串行事实写入的职责上限 | CompanyEligibility先停派发、排空已承诺写入，stop保留失败/未提交原因；不把单writer变成可并行写库参数 |
| scheduler与deferred初始化线程 | RuntimeSupervisor启动，后者只在有后台初始化器时创建 | 启动异常沿同一个stop路径发停止信号并保留线程引用，等待所有实际已启动线程；未启动线程不join。停止报告分别包含scheduler/runtime/deferred_initializers，后台初始化未退出不能标completed |
| Long、输入预载、缓存与预算 | 组合根注入；Long每档独立，共享预载/缓存/预算归第一档 | ProfiledApplicationSystem逆序停档，第一档最后关闭共享资源；超时线程不能强杀，明确返回未完成，调用者继续遵守整轮回收门 |

所有监督器默认停止期限使用已注入单调时钟，显式传入deadline时各资源共用该对象。后台初始化收到停止后不再执行后续初始化器；某个正在执行的不可取消调用超时，只能报告未完成，不能以daemon线程替代回收证明。

资源或档位的普通关闭异常须记入原`ShutdownReport`后继续尝试其余owner，跨档仍逆序关闭，第一档最后回收共享资源；失败诊断只保留有界异常类型因果链，不输出原异常载荷。部分启动失败也回收已尝试启动的owner，关闭不完整时保留报告与原启动异常因果，外层沿用已有回滚期限；预算句柄由application资源编排统一最后关闭并计入报告。缓存停止可先释放等待者，但真实loader登记和计数仅在loader退出后移除，重复停止不得因等待者已释放而报告全部回收。

`trader-server`启动收到`ShutdownIncompleteError`时，回滚owner已经尝试关闭；入口按不完整回收策略立即失败退出2，不重复stop或创建新期限，也不等待日志、时间线投影或诊断线程。原报告及启动因果保留在异常中，强退时不保证来得及输出。回滚完整后的普通异常和进程控制异常仍按原身份传播。

系统已启动后的绑定、地址输出及Web线程启动／运行失败，先由原controller建立或复用关闭期限及看门狗；Web异常先通知主线程，再做诊断。Web与应用两个owner的停止先派发再按同一期限等待，不因诊断或清理异常重复停止；已绑定但尚未运行的Web仅关闭socket，不调用需要服务循环响应的shutdown。看门狗启动失败或回收不完整立即失败退出2，完整回收后保留原主线程异常／控制异常语义。日志及时间线投影仅尽力异步输出，不作为硬退出前置条件。

隔离HTTP与SDK使用有上限的字节帧，父进程在完整帧头、正文收发的每一段复核原期限和取消，不以poll可读代表整帧已经到达。HTTP只传公开请求／响应字段及原始正文，metadata上限64KiB、请求正文16MiB，响应沿用请求的max_response_bytes；SDK只返回至多1MiB的字节结果，由供应商adapter解码，父进程不反序列化子进程给出的Python对象。超时、取消及中途退出均丢弃并有界回收该worker，健康HTTP进程继续复用；物理请求预算规则不变。

服务关闭使用同一总期限；业务stop返回后线程看门狗继续保持，不能提前取消后无限等待非daemon线程。这一线程机制尚不能证明进程实际退出已受硬截止约束：阶段281保留内部业务返回约0.319秒、外部观察进程33.09秒才退出的反例，30秒门保持未完成。内部期限从Python信号处理器实际执行时开始，须先将该起点与外部发信号及pidfd退出观察对齐，不能把两者差值全部归因于解释器退出尾；本HDD缺旧PC原报告，信号前段与退出尾均待定位。BoundedExecutor通过启动时登记的真实工作线程按剩余期限join，任务计数归零或Future完成不代表后续完成回调已结束；再次stop继续检查原线程。关闭诊断保留有界的开始/结束时间线及线程栈投影，在独立daemon线程输出，不作为清理完成或实际退出的证明。真实信号验收须绑定同一进程出生身份，核对进程退出事件、退出码与资源报告。

手工所选档位训练使用同一`ManualCancellationScope`持有取消事件和关闭期限：已有首次信号时沿用原期限，正常结束时才建立关闭期限；正常清理期间首次信号仍只请求取消，再次信号才强退。最终`selected_training_result`在消费executor的真实`ShutdownStep`后形成，`training_status`保留关闭前作业结果，`status`反映整作业终态；逐档已提交模型和原失败／取消及通知问题分别保留。关闭不完整或watchdog启动失败使原成功作业返回非0；取消仍为130，无法保持异步保护且资源未关时明确强退，正常失败为2、取消为130，不返回后无限等待。

训练汇总的`shutdown`只投影`name/completed/timed_out/cancelled_count/failure/watchdog_started/watchdog_failure`，失败只给安全类型，不公开原异常文本。尚未取得关闭报告及明确未完成时保留原watchdog；watchdog启动失败仍用已建立的同一期限执行资源关闭并报告能力失败。以上是CLI工程合同，不替代281真实服务30秒或平台验收。

手工下载／训练的`ManualCancellation`强退回调只执行既有退出码映射和强退，不先写stderr或取得关闭互斥锁；首次信号通知仍由正常取消路径报告。首次信号身份只赋值一次，强退读取已发布的只读快照；通用controller超时码及手工取消130／正常失败2的规则保持。满终端管道或关闭状态锁不能阻塞最终期限，强退时不依赖最后一条终端提示完成。

下载进度的计数与结果事实由application唯一持有，经不可变typed事件交给infra投影；列宽、字符清洗、来源中文及耗时显示不进入应用编排。命令观察线程仅由entrypoints在当前`ManualCancellationScope`内启动，并沿同一关闭期限停止／等待及记录`ShutdownStep`；线程或状态锁未回收保持未完成，不能以daemon或任务计数归零签收。直接调用下载组合用例只同步投影，不隐藏创建第二观察线程。

同一线程池内的任务不能同步等待自己再次提交的任务，避免池满后互等。资源上限说明实际是软件预约/观察门，还是宿主进程/系统硬隔离，不把RSS采样写成保证不会被系统杀进程。

## 4. 数据发布与读写隔离

下载先读取全部股票的名单和ST状态，排除ST后，再安排合格股票的下载任务和断点，最后检查整批完整性，通过后切换可用发布。训练和荐股只读取固定发布身份；一个操作中途不跟随新资料切换。ST过滤不能拖到日线、财务、公告正文或OCR之后；续传和补缺同样受这道门约束。

正常盘中从实时采集到服务端推荐发布，目标是零数据库连接、零 SQL。启动预读在轮前；非关键诊断或研究写入用有界后台队列，在正常轮次外计量；历史查询、序号预留和15:00正式冻结分别计量。队列满、写入失败或未排空必须可见，不能把正式持久化承诺降成易失内存。

启动预读的同一受管worker完成初始化后，每30秒在轮外观察参考、历史、公司正式事实及模型的已提交身份。只更新磁盘身份、观察失败及`restart_required`，不替换已加载资料／模型、不重新扫描价格／事实或加载模型载荷；API、SSE和评分状态读取仍只用内存。模型观察持有最多1秒的既有发布锁，仅核对有界metadata及双头身份，不代表完整模型载荷审计。观察失败保留上次有效身份，取消后的晚结果丢弃；该worker沿服务既有共享deadline停止与等待。新资料和模型仍由重启采用。

已加载证券参考超过原时效门或读取状态降级时，全市场刷新在采集／特征构造前以`static_reference_degraded`拒绝，不将空总体写入最近有效特征缓存。旧快照保留原来源时间，过期资格仍不得派发定向报价；同一参考失败在恢复前只打印一次，健康状态继续保留失败及次数，页面提示更新参考资料后重启采用。

“内存中一次替换全部短线视图”与“多数据库一次事务”是两件事。现有跨档数据库不能假称全局事务。正式冻结先取得真实持久成功凭证；部分提交恢复保留已提交事实，不覆盖、不拼成假完整批次。若实测及正确性证据支持合库，才考虑同库原子冻结方案。

## 5. 数据库布局和公共字典规范

下面列的是数据库文件和工件目录，不是字段表。路径按 `config/runtime.json` 中 `data_dir=data` 举例；实际数据根目录由该配置决定。

| 资料族 | 当前代码或文档中的代表路径 | 负责文档 | 主要读写方式 |
| --- | --- | --- | --- |
| 完整历史目录和年库 | `data/market/history/catalog.sqlite3`、`prices_<year>.sqlite3` | d 第3、8章 | 下载批写，训练按日期顺序读，启动按窗口读 |
| 独立短窗 | `data/market/daily_window.sqlite3` | d 第4、8章 | 整窗 raw/qfq 配对提交 |
| 证券、ST、行业 | `data/market/reference/company_st.sqlite3`、`industry.sqlite3` | d 第2、8章 | 手动更新、启动批量读取 |
| 公司采集与正式事实 | `data/company/raw/company_collection.sqlite3`、`company_facts.sqlite3` | d 第5、8章 | 采集账本和轻量正式事实分开 |
| 样本与模型 | `data/training/` | e 第3、7章 | 身份固定、有界批读、各档双头发布 |
| 训练到期与提醒 | `data/training/maintenance.sqlite3` | e 第3.7节 | 训练owner独立事务、同日提醒first-wins；历史catalog只读，不随历史重排复制 |
| 冻结、序号、研究 | `data/recommendation/<profile>/`、`data/research/<profile>/` | c 第12章、e 第8章 | 追加正式事实、按日期查询、研究隔离 |

这是路径索引，不证明本次检查过活动库。后续字典应从 schema、SQL 和消费者核对，不能只抄某次落盘清单。年度价格表只写一套结构及分区规则，不把2018–2026同一字段表重复九次。

```mermaid
flowchart TD
    D[下载任务] --> S[读取股票名单和ST状态]
    S --> ST[排除ST股票]
    ST --> H[合格股票 历史目录 年度价格 独立短窗]
    ST --> C[合格股票 公司采集库 原件和断点]
    C --> F[公司正式库 事实和发布凭证]
    H --> T[训练 样本和双头模型]
    H --> R[荐股启动内存]
    F --> R
    T --> R
    R --> P[推荐正式记录 轮次审计]
```

这是数据去向图，不表示这些库共享一个事务。采集库与正式库、不同年度库、不同档位冻结的实际提交与恢复边界分别由d/c说明。

每张表必须写清用途、唯一写入者、全部读者、主键、索引、事务、发布边界、保留范围和预计读写量。每列按下表逐项说明，不允许“用于业务”“相关数据”等空话。

下面是字段说明模板。示例来自 `data/company/company_facts.sqlite3` 的 `company_facts` 表；这里的 `published_at` 是本地正式快照发布时间，财报原披露时间是另一列 `disclosed_at`。

| 列名 | 中文含义 | 类型和单位 | 来源及原始字段 | 空值含义 | 更新比较规则 | 荐股层/训练用途 | 写入与读取代码 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 示例：`published_at` | 本地事实快照发布时间 | INTEGER，时间编码由持久化codec解释 | 本地正式发布事务 | NOT NULL | 事实与成功凭证提交时记录，不能替代原披露时间比较新旧 | 追踪采用的资料版本；不表示财报当时已可见 | `company_facts_schema.py`、`company_facts_index.py`；完整消费者归d第8章 |

### 5.1 跨业务供数与消费者核对

本节连接下载物理来源、不可变端口和荐股/训练消费者；当前下载表列、索引、codec及download.json字段以[d第8章](d_下载设计.md#8-当前数据库与字段字典)、[第9章](d_下载设计.md#9-downloadjson-配置字典)为唯一字典。下载物理字典的原核对基线为`2f0a8710`，当前行业训练交接按358的`75e1fffd`及后续两批补齐；阶段288的`758cb500`及318的`138a0de8`只作旧字典交付来源，不把旧schema编号或表列计数套给当前定义。字段说明齐全不代表活动库内容或真实业务覆盖已验收。

| 数据或资源 | 唯一来源、类型与消费者 | 边界及证据归属 |
| --- | --- | --- |
| 证券/ST/日历 | reference的company_st.sqlite3 → PreparedReference/HistoryStEvidence → 发布参考；A的PublishedReferences/StaticMarketCache与B的PrepareReference使用各自资格，训练按发布总体/日历读取 | 下载排除当前／曾ST、unknown继续采集；荐股unknown仍待核。原检查日与本次观察时点分开；d2.3、c2/16.2；A340、B323（bdd65998）的功能证据不等于实际整链采用 |
| 历史发布/年度价格 | catalog与prices_<year>.sqlite3 → ReadPublishedHistoryUseCase、HistoryTrainingRow/Point；A共享预载、C共享历史fanout | 固定snapshot/revision/sequence，取消与只读名单在解码前生效；不另建子集manifest；d3/8、e3.1及3.4 |
| 独立短窗 | daily_window.sqlite3 → 同一历史只读端口的短窗adapter → A启动历史投影 | 原始/前复权配对、成员和发布凭据独立；不借完整历史断点，不在运行期补历史；d4/6.1 |
| 行业 | industry.sqlite3 → CurrentIndustryCache → A的PublishedReferences展示／模型路由；组合根另冻结CurrentIndustrySnapshot → C四档全历史分组 | 当前映射、缺口、收到／刷新期限及原件凭据各保留含义；最新映射用于正常训练，不冒充历史当时分类。历史点时研究另验，旧发布不回写；d2.5、c7及e3.1 |
| 公司正式事实 | company_facts.sqlite3 → CompanyEvidenceReadPort/CompanyFacts → PublishedCompanyResearch内存投影 → A过滤/评分/风险 | 采集库、文件/PDF/OCR不是正式评分输入；未知不填无风险，披露日与本地published_at不同；d5.4/5.9/5.10、c6/16.4 |
| 模型/样本 | C的封存samples.sqlite3/complete.json及双头model.json → 严格profile加载 → A模型排序/净效用/动作门 | V1/V5独立排序，V2/V3仍local_score；数值重放、质量、收益与实际采用分别取证；e3.4–3.6/7.1、c7–12 |
| 实时报价/分钟 | A行情adapter → 不可变批次 → 特征/横截面/四档决策 → 只读快照/HTTP/SSE | 固定同轮输入，正常轮不访问业务数据库；Long独立、旧结果保留，不能拿启动查询替代运行轮观测；c1/4/13、b8 |
| SQLite页缓存/mmap | 公共sqlite.json → SqliteIoPolicy → download/荐股已有连接边界；训练样本连接采用training.json.resources.samples的独立设置 | 技术primitive不成为第二业务owner；保留样本独立作用域，不把纯I/O参数写成模型语义；b7.2、e6.2 |
| 执行器/公司预载 | scheduler.json → ExecutionSettings/CompanyPreloadSettings → bootstrap显式创建/注入 → A执行器/共享预载 | 创建、启动、停止、等待见b3；默认容量和30秒/3次保留，生命周期回归见342；改值未签性能 |
| 训练预约/批读/缓存 | training.json.resources → TrainingResourceSettings/TrainingSampleSettings → C训练组合根/fanout/全部样本读写者 | 每档有界队列、CPU/内存预约、取消清理见e6.2–6.4及7.2–7.3；已提交345资源接线，实测由C后台承接 |

量额追链结果：`tencent_daily._row`将日线第5位成交手数乘100成为股数、第8位万元金额乘10000成为元；`tencent_qfq_supplier._parse_side`按raw/qfq分支原样传量额，`price_columns._side_values`写各分支列，不使用价格复权比去乘成交额。训练SQL明确取`daily_price_revision.qfq_amount`与`raw_volume`，`history_prices._decode_training_row`生成`HistoryTrainingPoint`；`AlignTrainingRows`按市场日历区分停牌估值与真正缺行，`sample_builder`再用于20日金额、V1因子及V5量额通道。因此qfq_amount是qfq分支随价格一起取得的实际金额输入，不是数据库内重新计算的复权名义成交额；不能仅凭字段名把它等同raw_amount或改模型公式。这里确认当前代码来源和换算，供应商真实历史同口径/修订一致性仍由B审计，不替代全部来源事实核验。

字典交接继续沿原341：当前静态下载字典已在d第8–9章按基线源码整理，后续只核源码/配置及现场未提交差集，不再从已替换的同路径旧稿取当前定义。真实字段读回、适用分母和覆盖仍归332；C的样本/工件/每机器资源字段见e，实际数值和释放须绑定原封存身份。文档核对不采用或提交B/C现场代码。

## 6. HDD 与 SSD 怎样分别设计

| 事项 | HDD 候选方向 | SSD 候选方向 | 必须一起检查 |
| --- | --- | --- | --- |
| 批读 | 按物理库、日期归组顺序读取 | 比较小批和适度并发 | 总耗时、读取字节、RSS、是否重复扫描 |
| 批写 | 单物理库一个 writer，合并事务减少寻道 | 比较批次和独立库并发 | 持久性、锁等待、事务数、尾批恢复 |
| 连接与索引 | 复用连接，按真实条件设计索引 | 同样避免重复连接和冗余索引 | 查询计划、写放大、整个业务耗时 |
| WAL/checkpoint | 在提交边界控制集中写入成本 | 比较 checkpoint 频率和写放大 | WAL 大小、关闭成本、故障恢复 |
| 样本布局 | 顺序读取、连续数组候选 | 批读与映射读取候选 | 首次生成、复用、增量和清理总成本 |

现状：`runtime.json.disk_medium` 是全局选择；`sqlite.json.sqlite_policy.hdd/ssd` 各有连接超时、读窗、驱动批次和 checkpoint，当前两组数值相同。用户已在2026-11-05明确换到HDD PC，本批只接续本机HDD；另一台SSD的样本、报告、失败和待验原样保留，两机分别验证记录，不将虚拟磁盘的`ROTA`当作宿主机介质证明。配置选择、用户确认和只读disk自动检测分别记录于g，不能相互替代。`download.history.write_batches` 已有介质差异，不表示所有模块都已调优。

组合根`BuildSqlitePolicy`选择唯一不可变策略，显式配置优先，自动检测unknown使用HDD保守策略。下载、训练及荐股持久化adapter把同一个策略传到公共SQLite边界；只读HTTP/Web不自行选介质或打开业务数据库。`connection_timeout_seconds`进入连接超时，`read_window_rows`限制底层取行窗口，`transaction_batch_rows`限制一次驱动批量提交的行数，`checkpoint_mode`由显式checkpoint操作消费。驱动分批仍处于调用者原有事务内，不按每个小批次分别COMMIT。

history另外通过`ChooseHistoryWriteTuning`选择业务writer的flush阈值：HDD首建8股/16384格，增量16股/8192格；SSD首建及增量16股/8192格，unknown按HDD。它与SQLite驱动批次是不同层的限制，不共享第二套默认值。策略和消费者证据及实测状态只记入g。

不得关闭正式数据持久性或弱化完整性换取速度。SQLite、数组和列式候选必须把首建、增量、重复读、取消、恢复、关闭都计入，不只比较一次查询。

## 7. 配置文件怎样写清楚

### 7.1 文件归属

下面的文件全部位于仓库 `config/` 目录；本表说明文件分工，各字段的当前值和消费者放在对应详细章节。

| 文件 | 主要配置 | 作用边界 | 详细字段放在哪里 |
| --- | --- | --- | --- |
| `runtime.json` | 数据目录、运行目录、介质、配置入口 | 组合根 | 本章 |
| `strategy.json` | 默认评分档位 `scoring_profile` | 统一解析 | 本章 |
| `sqlite.json` | hdd/ssd 连接、批次、读窗、checkpoint | 公共存储 | 本章 |
| `scheduler.json` | deadline、worker、队列、轮次和退出 | 荐股调度 | c 第1、14章 |
| `market_data.json` | 行情来源、请求期限、重试、熔断 | 荐股采集 | c 第4章 |
| `cache.json` | 数据集、容量、有效期、分组和总内存 | 荐股内存 | c 第13章 |
| `download.json` | 三模块范围、请求、批次、断点和 PDF/OCR | 下载 | d 第9章 |
| `training.json` | 四档窗口、线程、内存、模型与早停 | 训练 | e 第6章 |
| `features.json` | 特征定义和参数 | 特征合同 | c 第6章、e 第3章各说明消费用途，定义只保留一处 |
| `filter.json` | 资格、黑名单、硬过滤 | 荐股第2、4层 | c 第3、5章 |
| `scoring.json` | 评分组件和权重 | 荐股第7层 | c 第8章 |
| `risk.json` | 本地风险 | 荐股第8层 | c 第9章 |
| `selection.json` | 动作、六席、板块限制 | 荐股第9、11层 | c 第10、12章 |
| `cost.json` | 成本参考与净效用 | 荐股第5、6层 | c 第6、7章 |
| `delivery.json` | API、SSE、客户端恢复参数 | 只读交付 | 第8章 |
| `long.json`、`long_watchlist.json` | Long 行为和固定名单 | 独立展示 | 第8章 |
| `research.json` | 研究分区、评价和统计参数 | 离线研究 | c 第15章、e 第8章 |
| `performance.json` | 负载、轮数、延迟、内存和介质门槛 | 测量与验收 | 第10章 |
| `deepseek.json` | 既有请求、预算和复核参数 | 可选复核 | c 第11章；改造暂缓 |

### 7.2 字段说明格式

```mermaid
flowchart TD
    R[runtime 目录和介质选择] --> S[strategy 默认评分档位]
    S --> J[加载各业务JSON参数]
    J --> P[统一解析 类型与关系检查]
    P --> B[bootstrap 显式注入]
    B --> X[按本次入口注入下载 训练或荐股]
```

配置按 JSON 路径逐个叶子字段列清单；数组需写元素结构，动态名称需写允许值。每行必须有当前值、中文意思、类型、单位、范围、解析者、最终消费者、在哪一步起作用、何时生效及是否影响结果身份。敏感配置只写凭据位置，不写秘密内容。字段表前必须注明文件；混合多个来源时拆表，或逐行写清来源。

以下字段来自 `config/runtime.json`，字段路径从该文件根节点开始，不含额外的 `runtime` 层。

| 字段路径 | 中文意思 | 类型/单位 | 核对时当前值 | 作用 | 生效和约束 |
| --- | --- | --- | --- | --- | --- |
| `runtime_dir` | 运行状态目录 | 路径 | `.runtime/trader` | 组合根建立运行资源 | 启动读取；不得提交运行数据 |
| `data_dir` | 业务数据根目录 | 路径 | `data` | 各业务构造自己的数据路径 | 启动读取；不是自动删除范围 |
| `disk_medium` | 选择存储策略 | `hdd/ssd` | 本轮HDD PC工作区为`hdd`；另一台SSD进度保留 | 给实际读写 adapter 注入策略 | 启动读取；配置值不是物理介质证明 |
| `strategy_config` | 业务配置入口 | 文件名 | `strategy.json` | 统一配置解析 | 启动一次读取 |
| `long_watchlist` | Long 名单文件 | 文件名 | `long_watchlist.json` | Long 只读名单 | 不影响短线模型身份 |

以下字段来自 `config/sqlite.json` 的 `sqlite_policy` 对象。`<介质>` 表示 `hdd` 或 `ssd`，由上表 `disk_medium` 选择。

| 字段路径 | 中文意思 | 类型/单位 | 核对时当前值 | 作用 | 生效和约束 |
| --- | --- | --- | --- | --- | --- |
| `sqlite_policy.<介质>.connection_timeout_seconds` | SQLite 等锁超时 | 秒 | 两组均5 | 连接边界 | 有限正值；不能无限等待 |
| `sqlite_policy.<介质>.read_window_rows` | 一批读取的行数 | 整数/行 | 两组均512 | 批读 adapter | 先测后调 |
| `sqlite_policy.<介质>.transaction_batch_rows` | 每次向驱动批量提交的行数上限 | 整数/行 | 两组均64 | 公共SQLite批写边界 | 分批不拆业务事务，不改变逻辑原子单位 |
| `sqlite_policy.<介质>.checkpoint_mode` | WAL 收尾方式 | 枚举 | 两组均TRUNCATE | 连接/任务收尾 | 计入真实收尾耗时 |

这张表只是已核对的公共字段，不冒充全部参数字典已完成。其它文件逐叶清点和调用者对照是 g 中 P06、P07 及各业务任务的明确交付物。

合法改值要真正传到消费者；不能读完配置又用硬编码替代，也不能用“必须等于当前默认值”锁死调参。配置重启生效，只有改变特征、样本、模型语义的字段才影响对应身份。

### 7.3 完整配置清点与身份边界

本节与c第16章合起来覆盖`config/`顶层20个JSON文件。数组按元素结构或具名选择器列举，避免把每个Long名单成员、每个风险规则的相同结构复制成另一套配置。`config/performance_fixture/manifest.json`是性能夹具的子目录清单，不是第21个运行配置入口，归CodexC内部唯一后台沿g第4.5节保留验收。

解析是输入边界，最终消费者决定参数是否实际起作用。`runtime_loader._configuration_identity`包含合并runtime配置，但排除`sqlite_policy`和`pipeline.history_preload`；`factor_validation._strategy_contract_identity`包含完整strategy合并配置（包括描述性因子字段和关闭的公司奖励参数）。组合根再合并runtime、strategy和cost身份。改因子文义会形成新的运行配置身份，不重写旧冻结记录，也不改模型工件特征manifest身份。

公共路径/介质的五个叶子及SQLite两介质各四个叶子已逐项列于7.2。`strategy.json.scoring_profile`是枚举`v1/v2/v3/v5`（当前`v2`），由`LoadStrategySettings`解析；显式入口可覆盖档位，结果身份保留有效档位。其余评分文件见[c第16章](c_评分设计.md#16-字段配置与消费者核对)，下载的实际表列及配置见[d第8–9章](d_下载设计.md#8-当前数据库与字段字典)，训练见[e第3及6章](e_训练设计.md#61-trainingjson现有逐叶配置)。字典按所属业务定义，本章只维护跨业务消费者；现场未提交实现不据此认定已交付或已验收。

#### scheduler.json

来源：`config/scheduler.json`。`runtime_loader.LoadRuntimeSettings` → `PipelineSettings` → `bootstrap`、`runtime/schedule.py`、`supervisor.py`及各有界队列。事件队列/worker为正整数，执行器排队容量和来源urgent线程数允许0；轮期限0.1–120秒，停止期限≥0.1秒；预载提示0.001–3600秒；cadence为正秒。所有字段启动生效；除`history_preload`外进入runtime身份。

| 字段路径 | 中文作用 | 当前值 | 类型/单位 |
| --- | --- | --- | --- |
| `pipeline.event_queue_size` | 事件队列容量 | `512` | 整数 |
| `pipeline.priority_queue_size` | 高优先级队列容量 | `64` | 整数 |
| `pipeline.executors.control.normal_workers` | 每档控制池普通线程数 | `1` | 正整数/线程 |
| `pipeline.executors.control.urgent_workers` | 每档控制池紧急线程数，保证控制请求有独立lane | `1` | 正整数/线程 |
| `pipeline.executors.control.queue_capacity` | 控制池普通排队容量 | `4` | 非负整数/任务 |
| `pipeline.executors.source_data.normal_workers` | 共享来源调度普通线程数 | `5` | 正整数/线程 |
| `pipeline.executors.source_data.urgent_workers` | 共享来源紧急线程数，0表示不分独立lane | `1` | 非负整数/线程 |
| `pipeline.executors.source_data.queue_capacity` | 共享来源普通排队容量 | `5` | 非负整数/任务 |
| `pipeline.executors.candidate_quotes.workers` | 定向报价池线程数 | `4` | 正整数/线程 |
| `pipeline.executors.candidate_quotes.queue_capacity` | 定向报价排队容量 | `4` | 非负整数/任务 |
| `pipeline.executors.market_quotes.workers` | 全市场报价池线程数 | `3` | 正整数/线程 |
| `pipeline.executors.market_quotes.queue_capacity` | 全市场报价排队容量 | `3` | 非负整数/任务 |
| `pipeline.executors.intraday_minutes.workers` | 分钟池线程数及每批分钟并发上限，使用同一值 | `5` | 正整数/线程 |
| `pipeline.executors.intraday_minutes.queue_capacity` | 分钟请求排队容量 | `5` | 非负整数/任务 |
| `pipeline.executors.http_workers` | HTTP隔离进程并发上限 | `12` | 正整数/进程 |
| `pipeline.round_timeout_seconds` | 整轮统一期限 | `120` | 数值/秒 |
| `pipeline.shutdown_timeout_seconds` | 停止等待期限 | `30` | 数值/秒 |
| `pipeline.long_quote_max_age_seconds` | Long 实时报价最大来源年龄；按source_time计算，超过后保留报价并标记过期降级 | `30` | 正数/秒；必填，自定义scheduler须补齐 |
| `pipeline.history_preload.warn_after_seconds` | 历史预载慢提示期限 | `300` | 数值/秒 |
| `pipeline.company_preload.timeout_seconds` | 公司快照完整预载总期限 | `30` | 有限数值/秒，0.001–3600；不逐尝试重置 |
| `pipeline.company_preload.publication_attempts` | 完整预载尝试总次数，包含首次 | `3` | 严格整数，1–10；默认首次加最多2次发布身份漂移重读，一般异常不重试；全部尝试共用总deadline |
| `pipeline.cadence_seconds.full_market.before_close` | 任务 full_market 在 before_close 阶段的派发间隔 | `10` | 数值/秒 |
| `pipeline.cadence_seconds.candidate_quotes.before_close` | 任务 candidate_quotes 在 before_close 阶段的派发间隔 | `10` | 数值/秒 |
| `pipeline.cadence_seconds.topk_quotes.before_close` | 任务 topk_quotes 在 before_close 阶段的派发间隔 | `10` | 数值/秒 |
| `pipeline.cadence_seconds.topk_quotes.after_close` | 任务 topk_quotes 在 after_close 阶段的派发间隔 | `10` | 数值/秒 |
| `pipeline.cadence_seconds.long_quotes.before_close` | 任务 long_quotes 在 before_close 阶段的派发间隔 | `10` | 数值/秒 |
| `pipeline.cadence_seconds.long_quotes.after_close` | 任务 long_quotes 在 after_close 阶段的派发间隔 | `10` | 数值/秒 |
| `pipeline.cadence_seconds.score.before_close` | 任务 score 在 before_close 阶段的派发间隔 | `10` | 数值/秒 |
| `pipeline.cadence_seconds.intraday_tail.afternoon` | 任务 intraday_tail 在 afternoon 阶段的派发间隔 | `10` | 数值/秒 |
| `pipeline.cadence_seconds.intraday_tail.final_review` | 任务 intraday_tail 在 final_review 阶段的派发间隔 | `10` | 数值/秒 |
| `pipeline.cadence_seconds.industry_heat.warmup` | 任务 industry_heat 在 warmup 阶段的派发间隔 | `120` | 数值/秒 |
| `pipeline.cadence_seconds.industry_heat.morning` | 任务 industry_heat 在 morning 阶段的派发间隔 | `60` | 数值/秒 |
| `pipeline.cadence_seconds.industry_heat.afternoon` | 任务 industry_heat 在 afternoon 阶段的派发间隔 | `60` | 数值/秒 |
| `pipeline.cadence_seconds.industry_heat.final_review` | 任务 industry_heat 在 final_review 阶段的派发间隔 | `60` | 数值/秒 |
| `pipeline.publish_heartbeat_seconds` | 发布心跳间隔 | `15` | 数值/秒 |

上述executors由`settings/execution.ParseExecutionSettings`严格解析为不可变ExecutionSettings；缺项、未知键、布尔冒充整数及非法范围均拒绝。normal_workers与urgent_workers相加是对应池总线程数，urgent排队容量仍由有界执行器随urgent线程数推导。控制池至少各有1条普通/紧急线程；其他池不接受没有消费者的urgent配置。分钟loader已移除隐藏的5线程截断，合法值传到实际请求并发；所有新参数重启生效并进入runtime身份，不改变模型manifest、120秒整轮上限或168次复核预算。

旧`pipeline.market_workers`迁为source_data的normal_workers；保留旧自定义容量时，urgent_workers按原规则在market_workers>1时填1，否则填0。三个未消费的normalization_workers/strategy_workers/deepseek_workers删除，旧键一律拒绝，无兼容别名。现有自定义scheduler配置须按此13叶子结构迁入，并补必填company_preload的timeout_seconds/publication_attempts，默认30/3。唯一解析生成CompanyPreloadSettings，由组合根及所有具名诊断传给PublishedCompanyResearch；总deadline跨重读共享，取消不读取证据，失败/取消不采用部分投影，无可空或隐藏默认回退。原12,000代码安全上限不作为性能调参入口。

#### market_data.json

来源：`config/market_data.json`。`runtime_loader` → `MarketDataSettings` → 组合根显式注入`infra/market_data`来源路由/来源lane及公共HTTP。超时为有限正秒，次数和行数为正整数，布尔严格解析；`tushare.token_file`仅记位置，不记内容。来源合同标识不证明来源被调用。进入runtime身份；开关关闭仍保留配置身份。

| 字段路径 | 中文作用 | 当前值 | 类型/单位 |
| --- | --- | --- | --- |
| `market_data.eastmoney_timeout_seconds` | 东方财富请求期限 | `8` | 数值/秒 |
| `market_data.sina_timeout_seconds` | 新浪备用请求期限 | `8` | 数值/秒 |
| `market_data.full_market_primary_timeout_seconds` | 全市场主路期限 | `12` | 数值/秒 |
| `market_data.full_market_quote_attempts` | 全市场主路尝试上限 | `2` | 整数 |
| `market_data.candidate_timeout_seconds` | 定向行情期限 | `3` | 数值/秒 |
| `market_data.minimum_market_rows` | 全市场完整性最小行数 | `1000` | 整数 |
| `market_data.single_flight` | 同键合并在途请求 | `true` | 布尔 |
| `market_data.circuit_breaker_failures` | 连续失败熔断阈值 | `3` | 整数 |
| `market_data.circuit_breaker_seconds` | 熔断冷却 | `30` | 数值/秒 |
| `market_data.source_contracts.eastmoney` | eastmoney来源合同标识；进入来源/批次身份，不代表本轮实际调用 | `"eastmoney_quote_security_master"` | 文本 |
| `market_data.source_contracts.sina` | sina来源合同标识；进入来源/批次身份，不代表本轮实际调用 | `"sina_quote_backup"` | 文本 |
| `market_data.source_contracts.tencent` | tencent来源合同标识；进入来源/批次身份，不代表本轮实际调用 | `"tencent_quote"` | 文本 |
| `market_data.source_contracts.tushare` | tushare来源合同标识；进入来源/批次身份，不代表本轮实际调用 | `"tushare_sdk_120_point_daily_audit"` | 文本 |
| `market_data.tushare.enabled` | 功能启用 | `true` | 布尔 |
| `market_data.tushare.points` | Tushare积分声明 | `120` | 整数 |
| `market_data.tushare.timeout_seconds` | 物理请求期限 | `8` | 数值/秒 |
| `market_data.tushare.token_file` | 凭据文件位置 | `".token_key"` | 文本 |

#### delivery.json

来源：`config/delivery.json`。`runtime_loader` → `ServerSettings/ApiSettings` → HTTP/SSE及Web投影。端口1–65535，容量为正整数，保留时间为有限正秒；`TRADER_HOST/TRADER_PORT`可覆盖监听配置。查询条数不改变固定六席，HTTP仅读application投影。启动生效并进入runtime身份，环境覆盖另由有效ServerSettings携带。

| 字段路径 | 中文作用 | 当前值 | 类型/单位 |
| --- | --- | --- | --- |
| `server.host` | 监听地址 | `"127.0.0.1"` | 文本 |
| `server.port` | 监听端口 | `5000` | 整数 |
| `server.debug` | 调试开关 | `false` | 布尔 |
| `server.use_reloader` | 重载开关 | `false` | 布尔 |
| `server.allow_insecure_non_loopback` | 允许非回环明文监听 | `false` | 布尔 |
| `api.default_top_n` | 只读查询默认条数 | `12` | 整数 |
| `api.maximum_top_n` | 只读查询条数上限 | `12` | 整数 |
| `api.sse_history_size` | SSE保留事件数 | `256` | 整数 |
| `api.sse_client_queue_size` | 单客户端事件队列容量 | `16` | 整数 |
| `api.sse_max_clients` | SSE客户端上限 | `32` | 整数 |
| `api.web_snapshot_retention_seconds` | 浏览器快照保留时间 | `35` | 数值/秒 |

#### deepseek.json

来源：`config/deepseek.json`。`runtime_loader` → `DeepSeekSettings` → `DeepSeekBudgetLedger`、reviewer及显式HTTP客户端。当前`enabled=false`；读取配置不代表已发请求。预算为非负整数，批量/token/窗口为正整数，比例在0–1，期限为正秒；总物理请求≤168，重试/失败也计数。进入runtime身份；本阶段只核对现状，不改复核流程。

| 字段路径 | 中文作用 | 当前值 | 类型/单位 |
| --- | --- | --- | --- |
| `deepseek.enabled` | 功能启用 | `false` | 布尔 |
| `deepseek.base_url` | 复核API地址 | `"https://api.deepseek.com"` | 文本 |
| `deepseek.model` | 复核模型名 | `"deepseek-v4-flash"` | 文本 |
| `deepseek.timeout_seconds` | 物理请求期限 | `20` | 数值/秒 |
| `deepseek.batch_size` | 单次复核股票上限 | `4` | 整数 |
| `deepseek.max_tokens` | 单次输出token上限 | `1800` | 整数 |
| `deepseek.daily_hard_limit` | 每日物理请求总上限 | `168` | 整数 |
| `deepseek.strategy_limits.tomorrow` | tomorrow独立预算桶上限（物理请求次数） | `38` | 整数 |
| `deepseek.strategy_limits.d25` | d25独立预算桶上限（物理请求次数） | `16` | 整数 |
| `deepseek.strategy_limits.shared_preheat` | shared_preheat独立预算桶上限（物理请求次数） | `4` | 整数 |
| `deepseek.strategy_limits.emergency` | emergency独立预算桶上限（物理请求次数） | `5` | 整数 |
| `deepseek.stage_targets.shared_preheat` | shared_preheat阶段软目标（物理请求次数） | `2` | 整数 |
| `deepseek.stage_targets.tomorrow_morning` | tomorrow_morning阶段软目标（物理请求次数） | `6` | 整数 |
| `deepseek.stage_targets.tomorrow_afternoon` | tomorrow_afternoon阶段软目标（物理请求次数） | `10` | 整数 |
| `deepseek.stage_targets.tomorrow_final` | tomorrow_final阶段软目标（物理请求次数） | `5` | 整数 |
| `deepseek.stage_targets.d25_morning` | d25_morning阶段软目标（物理请求次数） | `2` | 整数 |
| `deepseek.stage_targets.d25_afternoon` | d25_afternoon阶段软目标（物理请求次数） | `4` | 整数 |
| `deepseek.stage_targets.d25_final` | d25_final阶段软目标（物理请求次数） | `2` | 整数 |
| `deepseek.stage_targets.emergency` | emergency阶段软目标（物理请求次数） | `0` | 整数 |
| `deepseek.stage_limits.shared_preheat` | shared_preheat阶段硬上限（物理请求次数） | `4` | 整数 |
| `deepseek.stage_limits.tomorrow_morning` | tomorrow_morning阶段硬上限（物理请求次数） | `10` | 整数 |
| `deepseek.stage_limits.tomorrow_afternoon` | tomorrow_afternoon阶段硬上限（物理请求次数） | `18` | 整数 |
| `deepseek.stage_limits.tomorrow_final` | tomorrow_final阶段硬上限（物理请求次数） | `10` | 整数 |
| `deepseek.stage_limits.d25_morning` | d25_morning阶段硬上限（物理请求次数） | `4` | 整数 |
| `deepseek.stage_limits.d25_afternoon` | d25_afternoon阶段硬上限（物理请求次数） | `8` | 整数 |
| `deepseek.stage_limits.d25_final` | d25_final阶段硬上限（物理请求次数） | `4` | 整数 |
| `deepseek.stage_limits.emergency` | emergency阶段硬上限（物理请求次数） | `5` | 整数 |
| `deepseek.adaptive.consecutive_failure_limit` | 连续失败降级阈值 | `2` | 整数 |
| `deepseek.adaptive.rolling_window` | 健康滑动窗口 | `5` | 整数 |
| `deepseek.adaptive.minimum_application_ratio` | 低应用率阈值 | `0.4` | 有限数值 |
| `deepseek.adaptive.healthy_application_ratio` | 健康应用率阈值 | `0.6` | 有限数值 |
| `deepseek.adaptive.healthy_batch_count` | 恢复所需健康批数 | `3` | 整数 |
| `deepseek.adaptive.cooldown_seconds` | 复核降级冷却 | `900` | 数值/秒 |

#### performance.json

来源：`config/performance.json`。`runtime_loader`/`market_policy.ParsePerformanceBudgets` → 性能诊断与门禁比较。行数/轮数/字节/延迟为非负或正数，百分比为有限数值；它们是验收预算，不是自动调度或收益参数。进入runtime身份；`database.hdd.evidence_status=validated`是旧配置标签，不签本轮CodexC后台或当前PC已通过。

| 字段路径 | 中文作用 | 当前值 | 类型/单位 |
| --- | --- | --- | --- |
| `performance_budgets.workload.market_rows` | 全市场测量负载行数 | `5500` | 整数 |
| `performance_budgets.workload.candidate_rows` | 候选测量负载行数 | `5500` | 整数 |
| `performance_budgets.rounds.warmup` | 预热轮数 | `1` | 整数 |
| `performance_budgets.rounds.measurement` | 计量轮数 | `5` | 整数 |
| `performance_budgets.database.hdd.sqlite_improvement_percent` | SQLite候选改善门槛 | `10` | 数值/百分比 |
| `performance_budgets.database.hdd.parquet_improvement_percent` | 列式候选改善门槛 | `15` | 数值/百分比 |
| `performance_budgets.database.hdd.minimum_wins` | 重复测量最少胜出轮数 | `4` | 整数 |
| `performance_budgets.database.hdd.evidence_status` | 原历史证据状态 | `"validated"` | 文本 |
| `performance_budgets.database.ssd.sqlite_improvement_percent` | SQLite候选改善门槛 | `10` | 数值/百分比 |
| `performance_budgets.database.ssd.parquet_improvement_percent` | 列式候选改善门槛 | `15` | 数值/百分比 |
| `performance_budgets.database.ssd.minimum_wins` | 重复测量最少胜出轮数 | `4` | 整数 |
| `performance_budgets.database.ssd.evidence_status` | 原历史证据状态 | `"unverified"` | 文本 |
| `performance_budgets.latency_p95_ms.market_normalization` | market_normalization阶段P95延迟门槛 | `250` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.market_merge` | market_merge阶段P95延迟门槛 | `600` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.canonical_snapshot` | canonical_snapshot阶段P95延迟门槛 | `900` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.targeted_overlay_commit` | targeted_overlay_commit阶段P95延迟门槛 | `100` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.board_preselection` | board_preselection阶段P95延迟门槛 | `5000` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.candidate_union_projection` | candidate_union_projection阶段P95延迟门槛 | `100` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.board_local_scoring` | board_local_scoring阶段P95延迟门槛 | `250` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.two_strategy_board_scoring` | two_strategy_board_scoring阶段P95延迟门槛 | `750` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.three_board_wall_clock` | three_board_wall_clock阶段P95延迟门槛 | `1000` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.global_selection` | global_selection阶段P95延迟门槛 | `100` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.board_ready_to_draft` | board_ready_to_draft阶段P95延迟门槛 | `500` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.quote_to_draft` | quote_to_draft阶段P95延迟门槛 | `5000` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.deepseek_to_hybrid` | deepseek_to_hybrid阶段P95延迟门槛 | `1000` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.sse_publish` | sse_publish阶段P95延迟门槛 | `100` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.browser_patch_to_paint` | browser_patch_to_paint阶段P95延迟门槛 | `100` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.snapshot_api` | snapshot_api阶段P95延迟门槛 | `200` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.etag_api` | etag_api阶段P95延迟门槛 | `50` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.dates_api` | dates_api阶段P95延迟门槛 | `100` | 数值/毫秒 |
| `performance_budgets.latency_p95_ms.status_api` | status_api阶段P95延迟门槛 | `100` | 数值/毫秒 |
| `performance_budgets.data_age_p95_seconds.topk_critical` | topk_critical输入年龄P95门槛 | `2` | 数值/秒 |
| `performance_budgets.data_age_p95_seconds.topk_other` | topk_other输入年龄P95门槛 | `5` | 数值/秒 |
| `performance_budgets.data_age_p95_seconds.candidate_main` | candidate_main输入年龄P95门槛 | `5` | 数值/秒 |
| `performance_budgets.data_age_p95_seconds.candidate_other` | candidate_other输入年龄P95门槛 | `10` | 数值/秒 |
| `performance_budgets.data_age_p95_seconds.full_market_main` | full_market_main输入年龄P95门槛 | `10` | 数值/秒 |
| `performance_budgets.data_age_p95_seconds.full_market_other` | full_market_other输入年龄P95门槛 | `15` | 数值/秒 |
| `performance_budgets.memory.cache_logical_bytes` | 缓存逻辑字节门槛 | `536870912` | 整数 |
| `performance_budgets.memory.process_peak_rss_bytes` | 进程RSS字节门槛 | `2147483648` | 整数 |
| `performance_budgets.memory.growth_percent` | 内存增长百分比门槛 | `20` | 数值/百分比 |
| `performance_budgets.relative_regression_percent` | 相对回退百分比门槛 | `5` | 数值/百分比 |

#### research.json

来源：`config/research.json`。`training/infra/research_settings.LoadResearchSettings` → 离线研究/终端留出及消融合同；与`training.json`正式拟合设置分开。跨度/隔离为正交易日，profile为已支持档位，arms为显式有序方案名；研究身份消费，不注入在线数值评分。

| 字段路径 | 中文作用 | 当前值 | 类型/单位 |
| --- | --- | --- | --- |
| `terminal_holdout_sessions` | 一次终端留出交易日数 | `200` | 整数 |
| `embargo_sessions.tomorrow` | tomorrow研究分区隔离交易日数 | `5` | 整数 |
| `embargo_sessions.d25` | d25研究分区隔离交易日数 | `5` | 整数 |
| `fit_date_candidates` | 按profile、dates[]声明候选拟合日期跨度；不是生产训练设置 | `[{"profile":"v2","dates":[750,1000,1250]},{"profile":"v3","dates":[500,750,1000]}]` | 有序数组 |
| `ablation_arms` | 离线消融方案集合 | `["rule_only","rule_model_gate","industry_model_utility","pooled_model_utility"]` | 有序数组 |
| `deepseek_comparison_arms` | 复核对照方案集合 | `["local_only","structured_facts_veto"]` | 有序数组 |

### 7.4 cache.json：数据集、容量和预算

来源：`config/cache.json.cache_policy`；`market_policy.ParseCachePolicy`解析为不可变`CachePolicy`，`bootstrap`创建同一有界缓存，`infra/cache.py`控制容量/年龄/组字节和总池预算，具体语义失效见c第13及16.8节。启动生效，进入runtime身份；`persisted=false`表示不落盘，不代表对应业务没有正式数据库。

每个`datasets.<名称>`都必须有以下八个叶子，下面逐数据集给出全部当前值。TTL和动作年龄为可空非负秒，capacity为正条数，group须存在于groups，persisted为严格布尔；cadence_task为调度任务名或null，multiplier为正倍数或null。引用cadence的两组用对应任务有效间隔作为刷新TTL，再乘3作为动作最大年龄（当前10/30秒）。缓存复用保留原source_time，TTL不授予新来源时间。

| 叶子 | 中文作用/最终消费者 |
| --- | --- |
| `refresh_ttl_seconds` | 缓存fresh/stale判定；null时由cadence解析 |
| `action_max_age_seconds` | 是否允许新动作的最大来源年龄；null时由cadence×multiplier解析 |
| `cadence_task` | 获取统一调度间隔的任务名 |
| `action_max_age_multiplier` | 由调度间隔推导动作年龄的倍数 |
| `negative_ttl_seconds` | 失败/负缓存保留秒数；不伪装成功观测 |
| `capacity` | 此数据集LRU最多条数；完整总体容量不足明确失败 |
| `group` | 共享字节预算所属组 |
| `persisted` | 缓存是否允许持久化（当前全部false） |

| datasets名称 | 刷新TTL秒 | 动作年龄秒 | cadence_task | 年龄倍数 | 负缓存秒 | 容量条 | 组 | persisted |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `full_market_quotes` | `null` | `null` | `"full_market"` | `3` | `10` | `12000` | `"observation"` | `false` |
| `candidate_quotes` | `null` | `null` | `"candidate_quotes"` | `3` | `3` | `12000` | `"observation"` | `false` |
| `intraday_minutes` | `45` | `90` | `null` | `null` | `45` | `12000` | `"observation"` | `false` |
| `security_master_calendar` | `86400` | `86400` | `null` | `null` | `300` | `12000` | `"observation"` | `false` |
| `quote_factor_inputs` | `86400` | `86400` | `null` | `null` | `60` | `12000` | `"feature_batches"` | `false` |
| `market_cross_section` | `86400` | `86400` | `null` | `null` | `60` | `4` | `"feature_batches"` | `false` |
| `board_cross_section` | `86400` | `86400` | `null` | `null` | `60` | `24` | `"feature_batches"` | `false` |
| `candidate_preselection` | `86400` | `86400` | `null` | `null` | `60` | `4` | `"feature_batches"` | `false` |
| `competition_group_mapping` | `86400` | `86400` | `null` | `null` | `60` | `2` | `"feature_batches"` | `false` |
| `raw_deepseek_review` | `600` | `600` | `null` | `null` | `60` | `2000` | `"review"` | `false` |
| `strategy_deepseek_review` | `600` | `600` | `null` | `null` | `60` | `2000` | `"review"` | `false` |
| `canonical_market_snapshot` | `60` | `180` | `null` | `null` | `10` | `3` | `"canonical_snapshots"` | `false` |
| `canonical_candidate_snapshot` | `30` | `90` | `null` | `null` | `10` | `6` | `"canonical_snapshots"` | `false` |
| `current_quote_index` | `30` | `90` | `null` | `null` | `10` | `3` | `"canonical_snapshots"` | `false` |
| `candidate_feature_batch` | `86400` | `86400` | `null` | `null` | `60` | `24` | `"feature_batches"` | `false` |
| `hard_filter_batch` | `86400` | `86400` | `null` | `null` | `60` | `24` | `"feature_batches"` | `false` |
| `board_score_batch` | `86400` | `86400` | `null` | `null` | `60` | `24` | `"local_scoring"` | `false` |
| `global_local_draft` | `86400` | `86400` | `null` | `null` | `60` | `4` | `"local_scoring"` | `false` |
| `deepseek_seen_codes` | `600` | `600` | `null` | `null` | `60` | `6000` | `"review"` | `false` |
| `published_recommendation_view` | `86400` | `86400` | `null` | `null` | `60` | `72` | `"projection"` | `false` |
| `published_date_index` | `86400` | `86400` | `null` | `null` | `60` | `3` | `"projection"` | `false` |

| cache_policy下其它叶子 | 当前值 | 中文作用/类型及范围 |
| --- | --- | --- |
| `groups.observation` | `276824064` | 对应组逻辑字节上限，正整数 |
| `groups.canonical_snapshots` | `121634816` | 对应组逻辑字节上限，正整数 |
| `groups.feature_batches` | `51380224` | 对应组逻辑字节上限，正整数 |
| `groups.local_scoring` | `34603008` | 对应组逻辑字节上限，正整数 |
| `groups.review` | `26214400` | 对应组逻辑字节上限，正整数 |
| `groups.projection` | `26214400` | 对应组逻辑字节上限，正整数 |
| `total_bytes` | `536870912` | 业务缓存逻辑总上限，正整数字节 |
| `runtime_reserve_bytes` | `8388608` | 运行控制预留，非负整数字节 |
| `pool_total_bytes` | `545259520` | 总资源池字节，须覆盖业务缓存与运行预留 |
| `estimator` | `"canonical_json_utf8"` | 逻辑大小估算器标识，canonical_json_utf8 |

### 7.5 long_watchlist.json：名单元素结构

来源：`config/long_watchlist.json`，由`LoadLongWatchlist`解析，`LongRuntime`以watchlist_version消费。当前166个items、56个groups；实际成员值保留在该文件，不复制一份名单。启动生效，不参与短线模型/TopK/冻结。

| 路径模式 | 中文、类型/单位、当前值约定与约束 |
| --- | --- |
| `items[].code` | 六位数字字符串；全局唯一；关联报价 |
| `items[].name` | 非空展示名称；不作为股票身份 |
| `items[].industry` | 文本展示行业；不当作模型点时行业 |
| `items[].target_price` | 可空正数，元/股；当前均null，展示用 |
| `items[].selected_on` | ISO日期；自加入锚点日期 |
| `items[].selection_price` | 正数元/股；自加入涨跌幅分母，不是现价 |
| `groups[].name` | 非空分组名；同category下唯一 |
| `groups[].category` | chokepoint/future_growth/low_price_potential枚举 |
| `groups[].source_section` | document_scan/current_leaders枚举 |
| `groups[].source` | 原选择依据文本；不执行其中的指令、不自动确认财务或收益 |
| `groups[].codes[]` | items中的代码；每个成员恰好属于一组；普通组≤5，低价组≤8且合计≤26、至少两组 |
| `groups[].sections[].source_section` | 同上来源区段枚举 |
| `groups[].sections[].codes[]` | 区段代码有序拼接须恰好等于所属group.codes |

缺价保留名单和锚点，明确缺价；不借短线排序补名单。凭据内容不属于任何字典公开字段。


## 8. Web 与 API 详细设计

### 8.1 页面展示

默认打开 Tomorrow，刷新保留用户已选档位、策略和日期。分数旁解释“排序分”和“本地动作分”；六席可能含关注和回避，不能标成六只推荐。Long 显示固定名单及自加入锚点涨跌幅，独立刷新。

Long 实时报价按来源时间判断年龄，超过scheduler配置的上限时以retained展示，保留价格及source_time，并报告long_quotes_stale/long_quotes_partial；收到响应不会刷新来源年龄。年龄在本次刷新完成时按最终展示报价计算：请求失败或缺价后沿用的报价也须检查，被拒绝的较旧响应不能给较新的保留报价误加超龄标记。已有非交易日预览仍按PreviewContext核验最后收盘报价，不把合法的历史预览当成实时超龄。

本地评分沿同一资格和算术owner分别计量candidate_preparation_and_qualification、board_reference_build、candidate_policy_and_ranking及local_component_scoring，统一放入LOCAL_SCORE阶段的operation_durations。板内参考构建只使用完整合格总体，后续策略投影及排名使用与它绑定的不可变资格输入；计量不进入正式冻结身份，也不表示新增缓存或优化已启用。

收盘结算先只读核验当日已保存基准的标量、canonical bytes和hash；有效时直接复用，缺失才获取收盘市场输入。按日及按区间读取共用同一解析/核验实现，非法字段类型、非有限数值或内容冲突均归settlement_evidence_conflict，不重写原记录。等待预载、市场不可用及收益未完整分别保留settlement_inputs_pending、settlement_market_data_unavailable和settlement_outcomes_incomplete；调度端只接受这些有限代码，未知原因归settlement_unavailable，并允许后续重试。

页面分开显示当前已发布结果与正在执行轮次。执行中不能清空上一轮。每层显示状态、输入数、通过数、业务淘汰数、待资料数、耗时、首个阻断和中文原因；未开始显示“未开始”，不能用0毫秒冒充完成。过滤计数来自后端，不由前端相减猜测。

### 8.2 接口

| 当前接口 | 内容 | 约束 |
| --- | --- | --- |
| `/` | 桌面页面 | 本地使用 |
| `/api/freezes/current` | 全部启用档位双策略批次 | 在同一锁内捕获不可变引用，再投影 |
| `/api/freezes/<strategy>/current` | 单个只读视图 | 不拼接成新批次 |
| `/api/freezes/<strategy>/history?date=YYYY-MM-DD` | 精确日期正式历史 | 通过只读用例，HTTP 不直接读库 |
| `/api/freezes/<strategy>/dates` | 正式日期目录 | Long 无历史，日期为空 |
| `/api/status` | 内存状态和诊断摘要 | 不读库、文件或网络 |
| `/api/events` | 有界 SSE 事件 | 短线整批事件、Long 独立事件 |

`profile` 只允许一个已启动的 v1/v2/v3/v5，`strategy` 只允许 tomorrow/d25/long。内部模型与完整供应商载荷不得直接序列化，adapter 用公开白名单。

`/api/status`的`market_data.sources.company_evidence.last_error_code`只公开有限的预载状态和已知读取异常类别；未知原因归`company_snapshot_unavailable`，无错误为null，不公开原始错误文本。planned_count/error_count统计已采用资料，预载完全失败时也可能同时为0，须结合错误码、loaded_identity及覆盖判断，不能仅据0错误签加载成功。

公司库读取失败时打印错误，服务和只读Web继续启动，相关资料资格按缺失处理。读取因果链中的已知SQLite结构、必需记录和完整性失败分别保留有限错误码；旧结构仍明确拒绝，普通日志给出公司资料备份及隔离重下的处理范围，结构不兼容的详细安全栈转入DEBUG。总健康同时反映公司预载、排除事实完整性、参考／历史加载，以及历史／参考／公司／模型四类发布观察失败，页面异常详情和既有Web健康工具消费同一降级原因。模型只统计当前档位且owned的Tomorrow／D25，按档位和策略去重；其它档位失败仍逐行可见。恢复仅解除对应原因，其它异常保留，最近有效身份和快照不变，状态读取不执行I/O。公司资料真实恢复和活动库替换仍须按下载授权边界处理。

`/api/status`按原层级公开`market_data.published_history_observation_failure`、`market_data.reference_publication.publication_observation_failure`、`market_data.sources.company_evidence.publication_observation_failure`和`scoring_profile.maintenance[].publication_observation_failure`。仅接受对应来源已定义的有界ASCII异常类别码或发布不可用码，成功为null；不公开原始异常文本、路径或对象。该投影不改变已加载资料／模型、restart_required及评分行为。

模型计算继续由原计算锁串行保护；状态读取仅获取最近一次完整的不可变计算快照，不等待正在进行的预测。快照在计算完成或已处理的deadline终态一次性发布，未处理异常或取消保留上一份完整快照；状态接口不触发模型计算。

独立V1同样使用短锁读取完整状态，计算退出后发布原有尝试计数（含异常、取消和deadline）。调度器在主锁外读取其它owner状态，在主锁内共同捕获自己的计数、问题和日历；公司资料在锁内捕获不可变epoch及同批发布元数据，锁外按真实时刻资格投影和聚合，不新增时效缓存。

全局轮次采集超时只在后续完整本地批次被原子接收后标记恢复，累计失败次数仍保留；单策略独立发布、hybrid更新及无关错误各按原身份处理，不能借局部成功清除全局超时或其它未解决问题。

未发布正式结果时，`/current`可用观察draft的版本作为ETag/projection_version，`/status`仍不公开该draft身份。健康工具只对双方明确not_ready、无正式freeze且符合观察draft合同的情况免比较这项版本；日期、覆盖和正式结果身份差异仍报告，不通过公开draft到status消除误报。

历史视图的ETag除冻结与报价投影身份外，还包含收益汇总的全部公开字段及汇总读取的降级状态。冻结和报价未变时，结算汇总更新、读取失败或恢复也须使条件请求重新获得200及当前投影；投影未变才返回304，不改写正式冻结记录。

Tomorrow/D25×V1/V2/V3/V5八个评分组合分别保留排名最高的6只股票，API、SSE、页面及冻结读取保持已固定席位的同一组代码和排名；推荐、关注、回避及复核结论随股票展示，不能用旧“只显示正式推荐”的投影隐藏已固定席位。选席前的输入、模型、成本及板限合同按[h的时间线与R08](h_需求文档.md#1-依据和冲突处理)核对，不从“保留6只”推导其全部撤销。Long完整名单独立更新，不进入这八组排名、发布屏障或冻结。

### 8.3 一次替换与恢复

```mermaid
flowchart TD
    R[推荐发布完整短线批次及轮次序号] --> A[只读边界生成一个batch事件]
    A --> W[浏览器校验身份和全部成员]
    W --> C[一次替换所有短线缓存和页面]
    C --> E{事件是否连续且连接正常}
    E -->|是| A
    E -->|否| H[浏览器请求current完整批次]
    H --> V[核对序号 丢弃迟到旧响应]
    V --> W
```

事件丢失、队列溢出、重连时重新读取完整批次；迟到响应不能覆盖较新事件。名单、分数、报价、原因和诊断同批替换；连接状态、来源年龄、错误提示可以单独更新。桌面验收用稳定版 Chrome、Edge 或 Firefox，检查可读性、操作和数据一致性，不定死分辨率。

真实页面检测使用统一入口的`recommendation/web/browser-live`，访问已运行服务，选择可用Chrome/Chromium或Firefox/geckodriver，并在报告注明实际浏览器；不启动模拟服务。检测自身的浏览器资源由会话及统一工具进程组有界回收。页面/API一致、SSE已连接与短线结果ready分项判断，空结果一致不能签荐股成功。

慢SSE客户端不得阻塞发布。游标超前、过期、不连续或身份不匹配时重同步；策略/日期切换后，迟到结果不能覆盖当前页。具体队列和超时参数由delivery配置逐字段解释。

## 9. 工具脚本全面重整：按业务和功能划分，只留一个接口

阶段338已完成 `scripts/` 唯一公开入口、业务归组、调用者切换及旧路径删除。本章保留现行结构和后续扩展约束，原文件清单只作来源对照；后续功能及性能／平台欠项查g原记录，不重复执行整套迁移。检查、生成及显式维护都从 `scripts/tools.py` 按参数选择，内部模块不再提供独立命令或兼容转发。

### 9.1 先清点现有脚本和调用者

来源：仓库 `scripts/` 全部Python文件及Makefile、README、测试、子进程和启用的Python skill调用。逐文件用途、副作用、消费者及原路径到目标路径的清单见[scripts说明](../scripts/README.md#全量文件去留与消费者清点)；下表保留本轮整理的原始范围。实施与验证证据只记入g阶段338。

| 现有范围 | 负责的事情 | 重整方向 |
| --- | --- | --- |
| `diagnose_runtime.py`、`runtime_diagnostics/` | 荐股、来源、下载、训练、存储、浏览器和性能诊断 | 保留一个入口，内部按业务和功能重新分组，删除重复或过期检查 |
| `check_tomorrow_training_memory.py` | 独立训练内存/重排验证 | 核对有效消费者，仍需要的检查并入训练资源模块，删除独立检测入口 |
| `check_refactor_quality.py`、`code_quality/` | 架构、命名、质量，以及显式格式/改名操作 | 检测能力归工程检查模块；修改类操作与只读检查严格区分，不默认运行 |
| `verify_wheel_install.py` | 安装包及公开入口验证，会创建隔离环境 | 检查归工程包验证模块，必须显式选择和指定隔离工作区；切换调用者后删除独立检测入口 |
| `generate_long_watchlist_asset.py --check` | 生成物与配置一致性检查 | 检查与生成归荐股资源模块，分别以consistency/generate动作进入统一入口 |
| `skill_evaluation/` | skill评测 | 核对是否仍有有效用途和消费者；停用skill遗留且无消费者的功能列入删除，不仅凭目录名认定无用 |
| `migrate_runtime_data.py`、`runtime_migration.py`、`repack_history_partitions.py` | 数据维护、迁移和重排 | 迁移归工程migration，重排归下载history；原操作和codec迁入业务目录，所有动作进入统一入口，不在整理时执行 |

清单每项记录现有文件、功能、业务归属、读取/联网/写入行为、输入输出、全部消费者、保留/合并/删除决定、目标位置和必删路径。不能仅凭名称老、没有直接导入或运行较慢就删除。

### 9.2 一个入口，按三层参数选择功能

公开入口使用 `scripts/tools.py`，实现以下三层选择；`--list`列出唯一catalog中的有效组合：

```text
--business recommendation --module pipeline --action latency
--business recommendation --module assets   --action consistency
--business recommendation --module web      --action company-ledger
--business recommendation --module web      --action shutdown
--business download       --module company  --action coverage
--business training       --module resources --action memory
--business infra          --module disk     --action profile
--business engineering    --module packaging --action install
```

`business`选择业务，`module`选择业务内部功能，`action`选择具体动作。选择关系唯一声明在`core/catalog.py`，当前58项，包括原49项功能及资源generate、迁移build/rollback、重排build/activate/rollback/finalize、公司live-sync前台同步计量和关闭时轴shutdown。公司coverage另以互斥具名参数公开pacing及材料键审计，沿用所属业务的事实输入、外部目录及回收预算围栏；公司I/O候选由B独立交付，不纳入本批候选或功能验收。根帮助只展示中立公共选项；选中动作的帮助只导入其参数模块，展示该动作输入、上限和副作用。原profile分派、`--check`及live/full/sources/runtime别名删除，无参数拒绝执行；内部worker仅由同一入口在隔离子进程调用，不保留旧独立入口或转发模块。普通产品CLI合同仍由产品实现拥有。

`recommendation/web/shutdown`经同一入口在前台观测显式`--pid`、`--birth-ticks`及已有普通日志文件`--shutdown-log`，默认只读；只有显式`--send-sigterm`才向经当前boot/PID/出生时刻复核的pidfd发送一次SIGTERM。调用者每次必须重新核对当前身份；CLI不接收预期boot，不能自动拒绝跨重启的旧参数。观测默认40秒、最多60秒，超时只结束观测，不强杀目标；该动作归工具层，产品HTTP/Web继续只读。

该动作仅支持Linux；当前解释器或内核缺少所需pidfd能力时明确报告unsupported，不自动更换解释器或退回PID信号。报告分别保留发送、Python处理器、业务返回与pidfd退出观测时间，缺失或冲突不签完整时轴；pidfd就绪是退出观测而非精确内核退出时刻，目标退出码仍未知，`runtime_shutdown_acceptance`保持false。30秒退出门不变，工具定向验证不能代签真实服务退出验收，具体参数见[scripts说明](../scripts/README.md)。

公共报告参数为`--report`及`--report-format`，默认标准输出，保存时只能使用显式仓库外路径的新文件；解析时拒绝已有文件、目录和链接，最终独占创建，不能覆盖配置、输入或旧报告。业务原有`--output`保留其工件含义，不作为公共报告别名。公共报告必须与原生工件文件、维护source/target/backup及skills工件目录分离；相同路径及双向祖先重叠都在执行前拒绝。业务参数先由所属解析器转换为不可变类型，不再有包含所有业务字段的全局Options对象。

只运行明确选中的动作。真实网络、浏览器、性能探测、训练样本/隔离数据构建、安装验证及维护等必须显式选择，并沿用各自授权和隔离围栏；格式检查、自动格式化、改名和破坏性迁移不能被普通检查或聚合命令间接触发，仍遵守用户授权限制。

### 9.3 内部按业务和功能分目录

业务目录与中立公共能力直接位于`scripts/`下，不再放在`checks/`之内；不为目录整齐制造空模块或转发层。

```text
scripts/
  tools.py                    唯一公开工具入口
  core/                       中立选择、执行、报告及公共类型
  recommendation/
    pipeline/                 荐股链路、就绪与性能
    web/                      只读Web、浏览器、公司账本展示与显式关闭观测
    long/                     长期观察准入
    assets/                   荐股静态资源检查与生成
  download/
    history/                  历史、来源、发布、恢复、布局对照与分区重排
    market/                   市场来源与短窗检查
    company/                  字段、目录、正文、OCR、续传和发布
  training/                   历史输入、样本、模型、资源和性能
  infra/
    disk/                     介质和SQLite策略检查
    database/                 隔离数据库基线
  engineering/
    quality/                  架构、命名与显式格式/改名
    packaging/                隔离安装验证
    migration/                隔离旧数据迁移与codec
    skills/                   显式skill评测
```

入口只选功能，不包含业务实现；各业务持有自己的解析器、不可变输入、校验和命令构造，不启动未选择的动作。`core`不导入业务选项或参数默认值，通用执行器不依赖业务目录；仅在选择后按catalog加载所属处理器。业务与功能的对应关系只声明一次，不能又在JSON又在代码维护两套名单。公共报告和执行上限共用，业务规则仍由实际业务拥有，工具代码不复制评分或下载规则。

### 9.4 删除无用功能，并一次切换调用者

把重复检查、旧业务/旧schema专用检查、已结束且没有后续用途的临时探针、失效参数及重复报告列成删除清单。仍承担有效研究或故障验证的检查保留；同类检查合并到同一实现，禁止同一项业务事实有两套检查结论。

同一批次完成内部模块、唯一入口、Makefile、README、诊断文档、测试和子进程调用切换，再删除旧脚本路径与别名。不能留“旧脚本转发新脚本”的兼容壳，也不能漏掉通过命令字符串调用的消费者。无用测试随废弃功能删除，关键故障和公开参数回归保留。

### 9.5 先测检测开销，再证明重整结果

先量实际仍需保留的检查：启动/导入、参数解析、重复读取、报告生成、子进程和总耗时；只对有热点证据的路径做性能改造。同数据、同功能、同输出语义对照，不能以少运行必要检查来宣称变快。

验收必须证明：只剩一个公开检测入口；每项保留功能有唯一实现；无旧路径/参数/文档/测试残留；选择一个检查不会执行其它检查；错误、超时、取消、跳过和合法空分别报告；退出码和中文/JSON结果一致。未授权格式脚本及其触发用例排除在本次自动验证之外；测试基础设施受影响时按实际范围补相应功能回归，不用运行禁止脚本凑门禁。

负责人：A负责入口、公共执行/报告及工程与荐股工具，B负责下载工具，C负责训练工具。具体文件、交接和本次实施状态以g中的原P61工作包及阶段338为准，功能迁移不代表实际运行、性能或发布已验收。

## 10. 性能与交付证据

### 10.1 证据身份与性能归属

每项实测报告固定代码、配置、数据、模型身份，实际 CPU/内存/介质，负载、缓存状态、重复次数、输出一致性和失败恢复。HDD 与 SSD 分别固定门槛；未有实机证据不能跨介质外推。

性能执行统一由g第4.5节的CodexC内部唯一后台承接，保留原P编号及历史D证据，不另设独立D或第二执行者。A/B/C提供已交付代码、诊断接口、合法输入和功能证据，继续各自实现及功能回归；后台只对冻结版本在空闲或独立机器执行。发生开发验证争用时，C后台仅延后、暂停或作废自己的受影响轮次，不停止A/B/C前台或用户进程。缺性能证据的优化仍不能启用，但不阻塞其它正确性改动交付；普通测试在SSD/HDD目录执行只说明功能夹具位置，不能当成介质性能通过。当前只接续本机HDD，另一台SSD进度原样保留，任务归属、排队和两种介质的结果只在g维护。

当前 `performance.json` 中分别有 HDD/SSD 门槛和证据状态，但配置中的 `validated` 不是本轮重新验收。旧5秒局部预算、来源年龄、120秒整轮 deadline 的对象不同，必须在测量报告中逐项对应，不能混成一个通过指标。

第一项荐股公共因子优化提供显式离线候选`raw_factor_reuse`，入口及参数见[scripts说明](../scripts/README.md)。现有基础因子、横截面缓存继续唯一拥有其算术；候选只在既有连续回放中缓存原`FeatureBuilder._raw_features`结果，不缓存整份快照，不修改生产组合根或默认实现。缓存身份包含完整报价、原始日线、历史摘要、模型horizons、基础配置、交易日和schema；返回副本供本次公司/风险/证据/分钟及模型专属组装处理。候选与基础缓存共用既有特征组容量/字节预算及停止生命周期，预算拒写仍返回本次计算结果；非有限输入继续交原实现计算并保留其异常，不缓存失败或取消。报告区分基础与候选原计算次数、原函数调用、特征组全部逻辑字节和实际RSS。只有功能等价及C后台完整同输入性能对照均通过后，才交原业务owner考虑原子采用并复测；这不自动授权生产切换，不能把计算次数减少直接称为提速。

### 10.2 按风险选择门禁

| 改动 | 必须形成的检查结果 |
| --- | --- |
| Markdown、注释、非运行元数据 | 完整diff、链接及`git diff --check`；仅在确有必要时选择现有文档合同 |
| Python逻辑 | 受影响Ruff及按风险选择的定向回归 |
| 公共类型或复杂边界 | 再加受影响mypy，不以Any、ignore或局部跳过掩盖消费者问题 |
| 测试基础设施或全仓测试调整 | 完整功能回归，显式排除未授权格式及无关脚本的触发用例 |
| 正式发布候选 | 完整lint、Long资源一致性、工程债务门、功能、类型、构建、仓库外wheel安装及下列真实专项分别有完整结果；格式门须明确授权，未执行就保留欠项，性能、平台和绝对预算单列 |

`make test`/`make test-fast`是非slow快速集，不代替完整功能回归；`make test-history`、`make test-train`、`make test-recommendation`按业务包含各自慢用例，`make test-full`是全量入口，`PYTEST_WORKERS`控制worker数量。`make lint`包含Long资产一致性、Ruff和工程债务检查；`make format-check`是须另获明确授权的格式门。运行前必须检查Make依赖、收集钩子及实际调用范围：格式检查、自动格式化和无关脚本只有用户明确授权才执行，不通过聚合目标或测试间接触发。含禁止项的入口须拆开；无法拆分则跳过并记录未执行范围，不宣称完整发布门禁通过。类型、构建分别核对现有`make type-check`和`make package`的实际依赖。

测试只读种子先关闭，写入或损坏场景复制独立文件，禁止共享可变SQLite连接或硬链接。mock、fixture、真实供应商、全市场、收益和发布是不同证据范围；失败、中断、缺XML及旧进程结果不得写成通过。

### 10.3 发布专项

| 专项 | 必须核验的边界 |
| --- | --- |
| 架构和业务 | 三业务依赖、CreateApp无副作用、只读HTTP/Web、评分与结构化veto分离、共享输入和四档双策略完整发布 |
| V5启动和降级 | 核默认四档与`./run.sh train v5`、`./run.sh v5`单档及PowerShell同义入口；V5独立TCN从同一已发布历史读取有界窗口，不借V3模型或新增下载源。序列在裁剪最近20根预读前生成并计入共享缓存；Torch仅V5按需加载，V1/V2/V3不加载。V5缺依赖或工件关闭其新决策准入，保留只读Web及合法旧投影，整批发布继续遵守共同屏障 |
| 乱序和恢复 | 来源乱序、超时、取消、迟到、慢客户端、SSE游标、15:00 first-wins及摘要恢复；旧正式记录不可改写 |
| 安装包 | 仓库外安装wheel，核console scripts、资源、包内各平台维护模板及`pip check`，不能只凭仓库内可导入签安装通过 |
| 桌面 | 使用当前稳定版Chrome、Edge或Firefox，实际渲染、交互、模型身份、同轮诊断、历史及Long一致且可读，不强制固定分辨率 |
| 性能和资源 | 固定源码、配置、模型、输入及环境；离线fixture禁外网，DeepSeek用固定响应。延迟、512 MiB共享逻辑缓存、2 GiB进程RSS、100 tick增长和完整轮次分别测量 |
| 生产权限和研究 | V1/V5人工生产授权、模型可加载、质量和收益各自取证；旧Shadow保持原身份。收益门及一次留出只由c第15.5节定义，本章不授权打开真实留出 |

DeepSeek联合改造暂缓。其每日168次物理请求、失败及重试计数、独立原子预算桶仍是现行合同；启用状态和未覆盖联合场景明确记录，不以本地链通过补签。相关回归使用固定时钟和隔离目录，不消耗真实额度。

### 10.4 五时段矩阵

| 场景 | 必须验证 |
| --- | --- |
| 上午热运行 | 四档双策略同一输入批次、一次完整发布；Long按现有频率独立更新 |
| 午间热运行或冷启动 | 统一120秒整轮，完成或回收后重采；失败保留上一完整批 |
| 15:00边界 | 最新合法决策、first-wins；迟到不能修改正式记录，冻结不被当前轮等待阻塞 |
| 15:00后热运行 | 正式批次的名单、分数、动作及报价固定；Long独立更新 |
| 正式记录命中或收盘恢复 | 精确同日恢复，缺记录才合法close_fallback，不跨日替代；fallback不能代签正常scheduled轮 |

### 10.5 整链、停止与存储恢复

1. 固定实际启用档位、源码、配置、模型、发布输入窗口、进程及出生身份，用新进程验收。缺资料或模型明确原因，不在荐股中补下载或训练，不停止其它终端任务。
2. 从页面反查API/SSE，再对齐报价、输入就绪、动态过滤、公共特征、模型/评分、本地发布及渲染的策略、交易日、轮次和身份。排查过期报价、full_market超时、排队争用、paired_input_epoch_mismatch、ready后未发布及旧页面覆盖。
3. 连续取得至少5个真实盘中不同且完整的轮次。四档双策略一次发布；慢档、供应商卡住、评分失败或迟到不能形成半批。验证119秒完成和120秒到期拒绝，阶段间不重置总预算；本轮回收完成前下一轮采集不增长，超时、取消、会话变化及停止均不得遗留占用。
4. API、SSE和实际桌面的策略、决策身份、数量、发布时间和来源年龄一致；空结果区分业务淘汰、资料待补和执行失败，并核首个阻断原因。固定六席后的动作或风险状态不得隐藏、删除或递补已保留股票；Long不评分、不排名、不冻结。
5. 同次记录排队、采集、准备、评分、发布及整轮耗时，取消清理和浏览器绘制单列。正常盘中采集到发布零连接、零SQL；启动、后台批写、历史和冻结另计。100 tick内存观察不能代替完整五轮。
6. 覆盖来源失败保留有效批次、SSE重连/轮询、Long独立、真实繁忙状态正常退出≤30秒，及批写队列排空或明确未完成项；工具发信号或局部fixture不能代签真实服务退出。
7. 存储恢复覆盖首次构建、同日/次日重跑、取消、部分提交、重启复用、当前结构不兼容拒读、参考冲突、raw/qfq单侧缺失、未发布修订隔离、冻结同内容幂等/异内容拒绝、损坏隔离和旧SSE游标resync。只接受当前实际结构，不补列、迁移、忽略编号或静默清空；恢复不得改写旧快照、模型或冻结记录。
8. DeepSeek的168上限并发、预留后崩溃、跨日迟到和状态丢失继续保留为隔离回归合同，按实际改动及授权选择，不把暂缓联合验证补签为完成。

诊断扩展唯一`scripts/tools.py`入口及所属模块。盘中或平台条件不足时保留具体未验项，不用预览、HTTP 200、合成输入或历史证据替代真实五轮；缺平台不阻塞Linux HDD正确性切片交付。任一适用绝对预算红项仍阻断发布，不能用其它阶段余量抵消。检查通过只形成候选，发布、PR/合并/tag、活动资料切换、删除和数据库重建仍需对应具体授权。

本章承接迁移前系统设计第13章中未被用户替代的门禁，历史原文可用`git show 23624ca0:docs/b_系统设计.md`溯源；执行本章无需再从历史正文拼接清单。实际改动、结果和未完成项只记g。
