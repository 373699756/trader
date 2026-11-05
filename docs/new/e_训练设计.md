# 训练设计

训练的目标是四档全部结束的总时间最短，同时保留样本、预测和质量的正确性。先测哪里耗时，再决定共享计算、调整线程或改变存储。减少日期、行业、样本或训练质量不能算工程提速。

本篇只管训练输入、样本、资源和模型工件。收益研究放c第15章，下载和数据库源字段放d；训练独立运行，不下载缺失资料。

## 1. 输入固定，双头独立

每次作业固定一个已发布历史版本及配置、特征身份。四档各有 Tomorrow、D25，共八个模型头；每档双头合格才发布该档模型。某档失败不污染其它档位，不覆盖旧有效工件。

独立训练入口为 `ExecuteSelectedTraining`。A的提交 `7a562886`已将CLI切换到该入口并删除 `ExecuteDownloadTrain`及旧下载异常分支；该提交的远端交付状态见g，不由C推断。下面原始字段字典以 `e69d3e02`核对，第9.2节保留该基线旧消费者作为交接证据；C的计量改动不新增下载或组合入口。

## 2. 四档比较

参数来源：`config/training.json` 的各档训练设置；特征定义见 `config/features.json` 和各档特征合同。算法和样本组织来自对应训练实现，不是数据库原列。下表当前窗口和拟合日期是只读核对值，不是新的调参建议。

| 项目 | V1 | V2 | V3 | V5 |
| --- | --- | --- | --- | --- |
| 算法 | LightGBM LambdaRank＋独立Ridge收益门 | 行业LightGBM回归 | 行业Ridge回归 | 行业TCN回归 |
| 历史回看 | 251完整日 | 251日 | 61日 | 61日形成60×6序列 |
| 当前有效拟合日期 | 750 | 1000 | 750 | 61 |
| 主要输入 | 十个完成日因子 | 长短动量、收益、流动性等 | 短窗收益、动量、流动性 | 序列及辅助输入 |
| 样本组织 | 日期排序query＋独立收益门 | 行业、日期、头 | 行业、日期、头 | 行业、序列、头 |
| 主要待测开销 | 矩阵读取、排序器与收益门 | 特征长窗、行业树拟合 | 矩阵转换、线性拟合/校准 | 序列、张量、逐行业迭代和线程 |

表中数值是本次只读配置核对结果，不是优化后的推荐值。当前V5为行业5000行门、网络8通道、float32计算、最多10轮、早停耐心3、相对改善阈值0.005、请求1线程；网络隐藏通道与输入6通道不是一回事。

## 3. 用什么数据库、字段做什么

### 3.1 原始历史与参考

数据库：`data/market/history/catalog.sqlite3` 保存发布/日历/总体身份，年度 `data/market/history/prices_<year>.sqlite3` 的 `daily_price_revision`、`daily_price_observation` 提供对应版本价格；行业使用该历史发布绑定的参考。`HistoryTrainingRow`、`HistoryTrainingPoint` 是读取后形成的程序对象，不是数据库表名。路径以 `config/runtime.json` 的 `data_dir=data` 举例。

| 数据库/对象 | 字段 | 中文意思及训练作用 |
| --- | --- | --- |
| history catalog | 快照、数据截止、标签截止、发布序号 | 固定同一次输入，后续修订不可混入 |
| history catalog | 交易日历、证券总体、上市/退市区间 | 同日切分、持有期、有效日期边界 |
| 年度 `daily_price_revision` | trade_date、code、board | 日期、证券、板块 |
| 同表 | revision_id、first_seen_sequence | 选择该发布可见的价格修订 |
| 同表 | 价格列、status | raw/qfq及有效/停牌/缺失语义 |
| `daily_price_observation` | sync_sequence、revision_id | 观察与修订的可见关系 |
| `HistoryTrainingRow` | code、trade_date、board、industry、cell_status | 训练窄投影、行业和日历对齐 |
| `HistoryTrainingPoint` | qfq_close_price、qfq_open、qfq_high、qfq_low | 收益、动量、波动及V5价格通道 |
| 同对象 | raw_volume | 实际成交股数及V5量通道 |
| 同对象 | qfq_amount | 模型合同金额输入；须追到存储及adapter确认与原始实际金额的关系 |
| 历史行业参考 | 行业ID、日期、生效与版本 | 当时行业、横截面、行业模型，不能用当前行业倒填 |

源表全列的SQL类型、单位、来源和索引归d第8章；本表只解释训练消费。尤其量额必须沿真实解析链核对，不因qfq字段名猜测或改公式。

### 3.2 样本数据库

数据库：V2/V3/V5在 `data/training/samples/<profile>/` 保存 `samples.sqlite3` 和 `complete.json`；V1在同一样本根的 `v1` 子目录下创建临时作业目录，退出时清理，当前没有跨作业封存样本复用。常规字段在 `samples`，分区和统计分别在下表列出的 `sample_*` 表，V5序列块在 `sample_sequence_blocks`。这些值由第3.1节的历史输入生成，列定义/顺序受特征合同约束，构建窗口来自 `config/training.json`。

| 表/字段 | 用途 | 正确性检查 |
| --- | --- | --- |
| `samples.trade_date/code/board/industry/avg_amount_20d` | 行身份、板块/行业和流动性暴露 | 日期与代码对齐、列语义 |
| 具名特征列 | 各档输入 | 公式版本、顺序、维度和必需缺失 |
| `target_t1` | Tomorrow标签 | 未来第1个市场交易日，成熟后使用 |
| `target_t2`～`target_t5` | D25分期证据 | 市场日历定位，缺失分期如实保留 |
| `target_d25_aggregate` | D25唯一聚合头 | 按现行标签合同生成，不混成四头模型 |
| `sample_split_dates` | 训练、早停、校准、评价分区 | 同日股票同区，边界隔离 |
| `sample_statistics` | 日期、行业、标签统计 | 避免反复扫宽表；与封存行数一致 |
| `sample_day_diagnostics` | 每日标签缺失归因 | 不以缺标签者反向改变当日特征总体 |
| V5 `sequence_start` | 当前样本对应序列起点 | 时间步顺序不变 |
| `sample_sequence_blocks.code/block_id/sequence_bytes` | 相邻样本复用重叠序列 | 无损解码、身份匹配，释放缓存 |
| V1十因子、`target_t1/target_d25/zero_signs` | 排序器与收益门样本 | 独立目标和query，保留约定负零语义 |

V1十因子和V5的60×6通道在c第7章逐项解释；物理字段、列顺序和消费者见第3.4–3.6节。公式owner仍在recommendation，不在训练另建计算定义。

### 3.3 日期与标签

2000日是整体历史目标，251/61日是特征回看，750/1000/61日是各档有效拟合日期，200日是终端留出最低范围，它们不是同一门槛。新股不要求上市前有数据，不统一要求每股拥有全部历史。

同一市场日历定位标签。已发布资料中的确认停牌、推定停牌、个股缺行和整市场缺日分别处理；可按合同用此前同源收盘作估值时保留estimated标志，量额为0不等于随意填缺失为0。异常单侧价格不能补造，不能向后借值；原始库事实不改写。

### 3.4 V2/V3/V5物理样本字典

以下表来自 `training/infra/sample_schema.py::TrainingSampleSchema`、`sample_index.py`，不是活动数据库审计。数据库位置见第3.2节；日期由 `infra/sqlite_codec.py` 编为距1970-01-01的整数日，程序边界解码为 `date`。主写入者是 `BuildTrainingSamples` 调用的 `SQLiteTrainingSampleIndex`；拟合、校准和评价通过同一index读取，训练覆盖诊断只读相同统计和列合同。

| 表 | 主键、索引和生命周期 | 读取者 |
| --- | --- | --- |
| `sample_contract` | 单行 `singleton=1`；封存前写入split身份 | index初始化、复用校验 |
| `samples` | `(trade_date,code)`，`WITHOUT ROWID`；分区准备时建立 `samples_industry_date(industry,trade_date,code)`，封存后只读 | 同档双头行业共享读取、V5批次、显式样本/读取诊断 |
| `sample_split_dates` | `trade_date`，`WITHOUT ROWID` | 拟合/早停/校准/评价按日期关联 |
| `sample_statistics` | `(trade_date,industry,target)`，`WITHOUT ROWID` | 逐行业行数、标签矩和有效日期覆盖 |
| `sample_day_diagnostics` | `trade_date`，`WITHOUT ROWID` | 每日候选及标签缺口归因、training-coverage |
| V5 `sample_sequence_blocks` | `(code,block_id)`；普通rowid表 | `SampleSequenceReader` 批读/缓存，两个头共用只读块 |

除上述行业日期索引外，这组schema没有另建二级索引。样本库是可重建中间产物，构建使用独立事务，统计随样本写入，日期分区和索引准备后提交；正式模型的持久性和发布边界另见第7章，不能把样本库的可重建写策略套到正式库。

| 表/列 | 类型、单位与空值 | 中文含义、来源和使用 |
| --- | --- | --- |
| `sample_contract.singleton` | INTEGER主键，固定1 | 单份列合同 |
| `schema_version` | INTEGER非空，固定2 | 命名列样本格式 |
| `feature_hash` | BLOB非空，32字节 | schema、特征有序列和序列布局的SHA-256 |
| `split_hash` | BLOB可空，32字节 | 构建期可空；分区准备/封存后必须匹配split身份 |
| `samples.trade_date` | INTEGER非空，日 | 信号日；同日股票必须进入同一分区 |
| `code` | TEXT非空 | 六位证券代码，与日期构成行身份 |
| `board` | TEXT非空 | 板块暴露，来自历史窄投影 |
| `industry` | TEXT非空 | 历史发布绑定的行业，供残差化及行业模型选择 |
| `avg_amount_20d` | REAL非空，金额沿输入合同 | 20日均额，供对数流动性暴露；写入前检查有效性 |
| 第3.5节具名特征列 | REAL非空、有限数 | 按有序manifest生成，不能用0填必需缺失 |
| V5 `sequence_start` | INTEGER非空，≥0，时间步 | 当前证券的60步序列在重叠块流中的起点；不是BLOB字节偏移 |
| `target_t1` | REAL可空，收益小数 | T+1个股收益减同日该期限已观察收益的等权基准；缺失表示标签不可用 |
| `target_t2` | REAL可空，收益小数 | T+2累计收益减对应期限基准，D25诊断分期 |
| `target_t3` | REAL可空，收益小数 | T+3同义分期 |
| `target_t4` | REAL可空，收益小数 | T+4同义分期 |
| `target_t5` | REAL可空，收益小数 | T+5同义分期 |
| `target_d25_aggregate` | REAL可空，收益小数 | `target_t2`至`target_t5`全部有效后取算术均值；是D25一个头的标签 |
| `sample_split_dates.trade_date` | INTEGER主键，日 | 分区的信号日期 |
| `split_name` | TEXT非空，枚举 | `training/early_stopping/calibration/validation/holdout`；由split写入，不能由模型自行切行 |
| `sample_statistics.trade_date` | INTEGER非空，日 | 同日统计，不重新读全部宽行 |
| `industry` | TEXT非空 | 统计所属行业 |
| `target` | TEXT非空 | `rows`表示样本总数，其余是上述六个目标名 |
| `sample_count` | INTEGER非空，行 | 有效标签数；`rows`项为样本行数 |
| `target_sum` | REAL非空 | 有效目标值之和；`rows`项为0 |
| `target_sum_squares` | REAL非空 | 有效目标平方和；用于计算标准差，`rows`项为0 |
| `sample_day_diagnostics.trade_date` | INTEGER主键，日 | 每日缺口归因的日期 |
| `candidate_count` | INTEGER非空，股 | 构造当日特征总体的候选数 |
| `missing_t1` | INTEGER非空，股 | 当日T+1未来收益缺失数 |
| `missing_t2` | INTEGER非空，股 | 当日T+2未来收益缺失数 |
| `missing_t3` | INTEGER非空，股 | 当日T+3未来收益缺失数 |
| `missing_t4` | INTEGER非空，股 | 当日T+4未来收益缺失数 |
| `missing_t5` | INTEGER非空，股 | 当日T+5未来收益缺失数 |
| `sample_sequence_blocks.code` | TEXT非空 | 与样本证券身份一致 |
| `block_id` | INTEGER非空 | 每块64步的逻辑块号，末块可不足64步 |
| `sequence_bytes` | BLOB非空 | 小端float64、每步6通道；`SampleSequenceWriter`无损编码、reader解码 |

标签生成的实际owner为 `sample_builder.py::_finalize_sample_day` 和 `domain/label_benchmark.py::ObservedTrainingBenchmark`。各期限缺未来收益只影响该证券/期限，不反向删去已形成的特征总体；某行Tomorrow与D25聚合目标都不可用才不写样本。标签未预扣交易成本，不能在训练和运行门各扣一次。

### 3.5 有序输入列与V5序列

来源：`config/features.json.model_contracts`、各档 `training/infra/profile/*/contracts.py` 与 `sample_schema.py::TrainingFeatureNames`。下表只解释训练消费和存储位置；收益/动量公式、残差化和在线锚点仍由c及recommendation纯函数拥有。V2/V3/V5辅助列的收益单位为小数，不是百分点。

| 列 | 中文用途 | 当前使用 |
| --- | --- | --- |
| `qfq_return_1d` | 信号收盘的1日收益 | 三档Tomorrow；双头合并样本中保留 |
| `qfq_return_3d` | 3日收益 | 三档双头 |
| `qfq_return_5d` | 5日收益 | 三档双头 |
| `qfq_residual_momentum_20d_skip5` | 剔除市场、板块、行业及对数均额暴露后的20日动量，跳过近5日 | 三档双头 |
| `qfq_residual_momentum_40d_skip5` | 同定义40日残差动量 | 三档双头 |
| `qfq_residual_momentum_60d_skip5` | 同定义60日残差动量 | 三档双头 |
| `qfq_residual_momentum_120d_skip5` | 同定义120日残差动量 | 仅V2双头 |
| `qfq_residual_momentum_250d_skip5` | 同定义250日残差动量 | 仅V2双头 |
| `market_qfq_momentum_120d_skip5` | 当日有效样本总体120日动量等权均值 | 仅V2双头，不是外部指数收益 |
| `market_qfq_momentum_250d_skip5` | 同总体250日动量均值 | 仅V2双头 |

V2物理辅助列按上表10列顺序，Tomorrow取10列，D25去掉第一列取9列。V3/V5物理辅助列只取前6列，Tomorrow取6列，D25去掉第一列取5列。manifest顺序不是集合：调换列顺序会改变身份，必须同时切换训练与推断消费者。

V5在各头辅助列后接360个序列值；两个头完整输入维数分别366与365。序列逻辑次序由 `recommendation/domain/market/v5/features.py::CalculatePriceVolumeSequence` 固定为“旧时间步在前，每步按下表通道顺序”。物理样本只保存辅助列、`sequence_start`与重叠块，不展开360个REAL列；样本块始终float64，拟合时才按 `tcn.compute_dtype` 转换。

| 通道顺序 | 原始输入 | 每步含义及单位 |
| --- | --- | --- |
| `open_return` | `qfq_open`、前日 `qfq_close_price` | 开盘/前收减1，收益小数 |
| `high_return` | `qfq_high`、前日qfq收盘 | 最高/前收减1 |
| `low_return` | `qfq_low`、前日qfq收盘 | 最低/前收减1 |
| `close_return` | 当日/前日qfq收盘 | 收盘收益小数 |
| `log_volume` | `raw_volume` | 实际成交股数的 `log1p` |
| `log_amount` | `qfq_amount` | 输入金额的 `log1p`；供应商到存储的金额口径需由d确认 |

序列使用信号日前61个完整日生成60步，不含信号日。训练滚动容器另保留信号行，故内部会需要62个点；不能把这个容器宽度改写成62日序列合同。辅助收益与标签仍以信号收盘为锚，V5不能直接复用另一档的完整可变输入矩阵。

### 3.6 V1独立样本

来源：`training/infra/profile/v1/samples.py::_SAMPLE_SCHEMA` 和 `recommendation/domain/market/v1/features.py`。V1样本库也标schema 2，但结构与V2/V3/V5不同，不能仅按数字2互读。以下所有列除两个标签外均非空；`samples`以 `(trade_date,code)` 为主键、`WITHOUT ROWID`，没有额外二级索引。`sample_contract`只有 `singleton INTEGER PRIMARY KEY CHECK(singleton=1)` 和32字节非空 `feature_hash BLOB`。

| `samples`列，按实际顺序 | 类型/单位 | 含义和消费者 |
| --- | --- | --- |
| `trade_date` | INTEGER，日 | 信号日期，四分区和query共同使用 |
| `code` | TEXT | 六位代码，排序/追溯使用 |
| `industry` | TEXT | 历史行业证据及诊断，不据此换成行业V2模型 |
| `return_1d` | REAL，收益小数 | 前一完整日锚点的1日收益 |
| `return_3d` | REAL，收益小数 | 同锚点3日收益 |
| `return_5d` | REAL，收益小数 | 同锚点5日收益 |
| `return_20d` | REAL，收益小数 | 同锚点20日收益 |
| `return_60d` | REAL，收益小数 | 同锚点60日收益 |
| `return_120d` | REAL，收益小数 | 同锚点120日收益 |
| `return_250d` | REAL，收益小数 | 同锚点250日收益 |
| `volatility_20d` | REAL，收益小数尺度 | 20日波动，具体估计式由特征owner维护 |
| `drawdown_60d` | REAL，比例 | 60日回撤 |
| `log_avg_amount_20d` | REAL，对数 | 20日平均金额的对数特征 |
| `zero_signs` | INTEGER，0–1023 | 上述十因子的负零位掩码，读取时恢复符号位 |
| `target_t1` | REAL可空，收益小数 | T+1超额标签，不可用则NULL |
| `target_d25` | REAL可空，收益小数 | T+2～T+5超额均值，四期齐全后有效 |

写入者 `BuildLearnedRankSamples` 先从251个历史完成日构造特征，再加入当前信号日；因此不能将V2同名收益列直接借给V1。`LearnedRankSampleIndex.read_matrices` 一次按日期/代码排序读取，最多分发双头四分区共8个独立矩阵，4096行批读并恢复负零；query组内同日、两个头分别按自身有效标签过滤。

V1日期切分由 `BuildLearnedRankDateSplit` 拥有：拟合、早停、校准、验证四段，每段之间至少5个市场日隔离；两头成熟期分别1/5日。它没有V2/V3/V5的 `sample_split_dates`、统计表和 `complete.json`。当前工件有效时可直接复用工件；工件不能复用时，V1从历史重建临时样本，“样本缓存复用”实验对V1须记为不适用，不能伪报缓存命中或另造缓存。

### 3.7 训练到期和提醒表

数据库是 `data/market/history/catalog.sqlite3`，表owner是 `training/infra/history/training_maintenance_index.py::TrainingMaintenanceIndex`；同文件中的历史发布表仍归download。`history_training_due.py`和训练提醒流程写入，状态/提醒读取及隔离重排复制流程消费；同身份冲突拒绝覆盖。三个表都是 `WITHOUT ROWID`，没有额外二级索引；提醒表与领取表的 `due_id`外键指向 `training_due`。

日期仍用整数日，时刻用UTC纪元微秒并在程序边界解为上海时区；不能按文本或毫秒猜测。

| 表/列 | 类型、空值 | 用途 |
| --- | --- | --- |
| `training_due.due_id` | TEXT主键 | 不可变到期身份，含档位/头等业务身份编码 |
| `reason` | TEXT非空 | 到期/未到期/资料不足等类型化原因，具体枚举由domain校验 |
| `baseline_date` | INTEGER可空 | 上次有效训练标签截止，无基线可空 |
| `cutoff_date` | INTEGER可空 | 当前可用标签截止，资料不足可空 |
| `matured_days` | INTEGER非空，≥0 | 自基线以来新增成熟标签日数 |
| `input_revision` | INTEGER非空，0/1 | 输入是否发生真实语义修订 |
| `observed_at` | INTEGER非空，微秒 | 本次到期观察时刻，不冒充拟合时间 |
| `content_hash` | BLOB非空，32字节 | 记录完整性，读取解码后复核 |
| `training_reminder_claim.due_id` | TEXT非空 | 已领取的到期身份 |
| `reminder_date` | INTEGER非空 | 上海提醒日期，与due_id组成主键 |
| `claimed_at` | INTEGER非空，微秒 | 先持久提交领取，再做桌面通知；同日first-wins |
| `content_hash` | BLOB非空，32字节 | 领取证据完整性 |
| `training_reminder.due_id` | TEXT非空 | 通知结果所属到期身份 |
| `reminder_date` | INTEGER非空 | 与due_id组成主键 |
| `outcome` | TEXT非空 | 仅 `sent/notification_degraded` |
| `attempted_at` | INTEGER非空，微秒 | 实际尝试通知的时刻 |
| `failure_code` | TEXT可空 | 通知降级原因，不含完整异常载荷 |
| `content_hash` | BLOB非空，32字节 | 通知结果完整性 |

读取不会为缺表自动建库；写入在确认 `history_catalog` schema 2 后创建完整表组并事务提交。仅完成训练模型可读不能替代到期状态检查；提醒领取也不代表训练或桌面通知已经成功。

## 4. 一次作业如何并行

```mermaid
flowchart TD
    S[固定历史和配置身份] --> R[逐档判断工件能否复用]
    R --> Z[全部复用 价格扫描0次]
    R --> H[确需样本 一次共享历史扫描和日历对齐]
    H --> Q[每档独立有界队列]
    Q --> F[四档并行执行 各有双头<br/>V1 排序器和独立收益门<br/>V2 行业树模型<br/>V3 行业线性模型<br/>V5 行业TCN]
    F --> P[各档独立评价与双头发布]
```

现有 `application/selected_training.py` 按256行不可变批次分发，每档队列容量2；`infra/selected_training.py` 固定SQLite快照并在扫描前判断复用。全部复用时零价格扫描；共享扫描与日历对齐已存在，四档样本/特征仍各自构建。

慢消费者会背压公共扫描。先量出是哪档计算、写入、资源等待造成背压，再比较“先算公共基础量”“调整批次”“隔离慢消费者”等候选。不能为了取消背压引入无限队列或四份完整历史。

## 5. 什么能共用

| 工作 | 共用条件 | 保持独立的部分 |
| --- | --- | --- |
| 历史扫描与解码 | 同快照、时间范围取并集 | 每档取自身有效日期和列 |
| 日历对齐 | 同日历与停牌/估值语义 | 原始事实、估值标志、标签 |
| 基础收益、均额、波动等 | 同窗口、公式、末日、缺失规则 | V1完成日与其它实时锚点定义 |
| 市场/板块/行业统计 | 同日期、同资格总体及公式 | 不同历史长度和输入集合 |
| 标签 | 同期限、成本与成熟定义 | 两头有效标签过滤和不同目标 |
| 样本读取 | 同档双头使用同一行特征 | 标签、切分、可变矩阵和模型 |
| 存储/内存 | 只读批次、连续数组或映射 | 不共享会被模型原地修改的矩阵 |

不能只因名称相同就共享。每个候选先记录当前重复次数、CPU、读写字节、RSS，再证明结果一致和总时间改善。

## 6. 怎样让四档全部尽快结束

当前CPU预算按实际亲和性和cgroup约束，拟合总预算约为 `max(1, 可用CPU-1)`，为扫描和协调保留资源，并限制BLAS/OpenMP线程。现有内存原子预约、批读和临时映射避免四档同时膨胀；这些机制先复用，不另建资源中心。

“慢档多分资源”是待测策略。先测每档/算法随线程数变化的边际收益：I/O或Python单线程瓶颈加模型线程可能无用；小任务获得太多线程也会挤压长任务。比较现有固定申请、长任务优先、有空闲时借用等候选，以全部完成的时间裁决。

下表是资源说明，不是数据库字段。训练线程、窗口和拟合参数来自 `config/training.json`；通用存储策略来自 `config/sqlite.json`，介质选择来自 `config/runtime.json`。CPU/cgroup、RSS和队列等待是运行时测量；尚未配置化的预算需沿实际实现清点，不能写成配置中已有字段。

| 资源 | 要测什么 | 配置化核对 |
| --- | --- | --- |
| CPU | 模型原生线程、等待、超额竞争、实际利用 | 总预算、每算法上限、协调预留；不得每档各占全CPU |
| 内存 | 只读共享、矩阵、张量、缓存、换页、预约等待 | 核对现有3GiB上限、1.5GiB预留等硬编码是否迁出 |
| 磁盘 | 公共顺序扫描、样本写入、映射随机I/O | HDD/SSD各自批次和并发，保持唯一策略来源 |
| 队列 | 长度、阻塞、批次字节和取消清理 | 批次、容量、最大驻留 |

`training.json`、`features.json`逐叶字段要写当前值、类型、单位、范围、各档含义、解析和最终消费者、身份影响及生效时机。V5早停/拟合日期属于质量相关配置，不能作为纯工程参数随意缩减。

### 6.1 training.json现有逐叶配置

以下来自 `config/training.json`，`profiles[]`是按 `profile` 选择的对象数组。记 `V1=profiles[profile=v1]`、`V2=profiles[profile=v2]`，其它同理；这只是文档定位写法，不是新增JSON字段。当前文件只有这四个profile，不含CPU总预算、内存预算、扫描批次或队列配置。

所有字段在训练作业准备时读取。V1经 `LoadV1TrainingSettings → ParseV1TrainingSettings → V1TrainingSettings` 进入独立构造/拟合；V2/V3/V5经 `LoadProfileTrainingSettings → ProfileTrainingSettings` 进入 `engine.py`，V5参数再经 `ParseTcnParameters → TcnFittingParameters` 校验。字段名必须精确匹配，整数不接受bool，数值必须有限；下表范围按实际解析和类型校验写，不虚构额外上限。

| JSON叶字段 | 当前值 | 类型/单位、范围 | 具体用途/最终消费者 |
| --- | --- | --- | --- |
| 各档 `.profile` | `v1/v2/v3/v5` | string，选中档位必须唯一 | profile选择；不是随意改名的显示文本 |
| V1 `.fit_dates` | 750 | int，≥1，市场日 | `BuildLearnedRankDateSplit`拟合段 |
| V1 `.early_stopping_dates` | 20 | int，≥1，日 | 独立早停分区 |
| V1 `.calibration_dates` | 20 | int，≥1，日 | 收益门校准分区 |
| V1 `.validation_dates` | 20 | int，≥1，日 | 工程评价分区 |
| V1 `.embargo_dates` | 5 | int，≥5，日 | 分区隔离，不能短于两头成熟要求 |
| V1 `.minimum_training_rows` | 1000 | int，≥1，行 | V1训练覆盖准入 |
| V1 `.learning_rate` | 0.05 | number，0–1且>0；解析下限0.000001 | LambdaRank学习率 |
| V1 `.max_depth` | 3 | int，≥1 | LambdaRank树深 |
| V1 `.num_leaves` | 7 | int，≥2 | LambdaRank叶数 |
| V1 `.minimum_leaf_rows` | 20 | int，≥1，行 | 单叶最少样本 |
| V1 `.boosting_rounds` | 200 | int，≥1，轮 | 最大提升轮数 |
| V1 `.early_stopping_rounds` | 20 | int，≥1，轮 | 排序器早停耐心 |
| V1 `.maximum_bins` | 63 | int，≥2 | 连续值分箱上限 |
| V1 `.histogram_pool_mib` | 64 | int，≥1，MiB | LightGBM直方图池 |
| V1 `.seed` | 0 | int，≥0 | 复现实验随机种子 |
| V1 `.ridge_penalty` | 10.0 | number，>0；解析下限0.000001 | 独立Ridge收益门 |
| V1 `.ranking_round_trip_cost` | 0.002 | number，≥0，收益小数 | 排序标签等级构造的往返成本参考；不授权改运行成本 |
| V2 `.fit_dates` | 1000 | int，≥1，日 | 行业树模型拟合日期 |
| V2 `.minimum_industry_training_rows` | 20000 | int，≥1，行 | 每行业覆盖门 |
| V2 `.lightgbm.learning_rate` | 0.05 | number，≥0.000001 | 行业LightGBM学习率 |
| V2 `.lightgbm.max_depth` | 3 | int，≥1 | 行业树深 |
| V2 `.lightgbm.num_leaves` | 7 | int，≥2 | 行业叶数 |
| V2 `.lightgbm.minimum_leaf_rows` | 20 | int，≥1，行 | 单叶覆盖 |
| V2 `.lightgbm.boosting_rounds` | 200 | int，≥1，轮 | 最大拟合轮数 |
| V2 `.lightgbm.early_stopping_rounds` | 20 | int，≥1，轮 | 独立早停区上的耐心 |
| V2 `.lightgbm.maximum_bins` | 63 | int，≥2 | 分箱 |
| V2 `.lightgbm.histogram_pool_mib` | 64 | int，≥1，MiB | 直方图池 |
| V2 `.lightgbm.seed` | 0 | int，≥0 | 随机种子 |
| V3 `.fit_dates` | 750 | int，≥1，日 | 行业Ridge拟合日期 |
| V3 `.minimum_industry_training_rows` | 20000 | int，≥1，行 | 每行业覆盖门 |
| V3 `.ridge.penalty` | 10.0 | number，≥0.000001 | 行业Ridge正则惩罚 |
| V5 `.fit_dates` | 61 | int，≥61，日 | 最近拟合日期；不减少独立确认和留出区 |
| V5 `.minimum_industry_training_rows` | 5000 | int，≥1，行 | 每行业训练覆盖门 |
| V5 `.tcn.learning_rate` | 0.001 | number，≥0.000001且≤1 | TCN优化器学习率 |
| V5 `.tcn.weight_decay` | 0.0001 | number，0–1 | 优化器权重衰减 |
| V5 `.tcn.dropout` | 0.1 | number，0≤值<1 | 训练dropout，工件保存网络参数 |
| V5 `.tcn.batch_size` | 256 | int，1–256，行 | 张量批次，不是共享扫描批次 |
| V5 `.tcn.epochs` | 10 | int，1–100，轮 | 最大迭代；默认和后台均按当前10轮方案 |
| V5 `.tcn.early_stopping_rounds` | 3 | int，1–epochs，轮 | 早停耐心 |
| V5 `.tcn.seed` | 0 | int，≥0 | 网络随机种子 |
| V5 `.tcn.hidden_channels` | 8 | int，仅4/8/16 | 网络隐藏宽度，不是6个输入通道 |
| V5 `.tcn.compute_dtype` | `float32` | string，仅float32/float64 | 拟合计算精度，不改变样本块float64编码 |
| V5 `.tcn.early_stopping_relative_delta` | 0.005 | number，0≤值<1 | 早停相对改善门 |
| V5 `.tcn.compute_threads` | 1 | int，1–4，线程 | TCN请求线程；实际授权仍受全作业CPU预算约束 |

拟合日期还要满足历史预算和split的关系约束，不能把“单字段≥1”理解为任意组合都能训练。V2/V3/V5配置中的算法参数由各拟合器使用，V1/V5还直接保存配置身份；现实现会把整组拟合设置纳入训练身份，改变线程、批次也可能失去工件复用资格。后续若区分纯运行参数与语义参数，须由A/C一起核对全部身份消费者后切换，不能先在文档声称日志、端口或所有资源参数都会触发重训。

`config/features.json.model_contracts[]`目前只含V2/V3/V5，下列每个叶字段都由 `LoadProfileTrainingSettings` 解析并与各档合同逐项比较；数组元素顺序也属于合同。V1十项完成日特征由自身纯合同拥有，不在JSON新增一份。

| 字段 | 当前值V2 / V3 / V5 | 类型与约束、最终用途 |
| --- | --- | --- |
| `profile` | v2 / v3 / v5 | 唯一选中profile |
| `history_sessions` | 251 / 61 / 61 | int≥61，匹配profile合同；滚动回看 |
| `momentum_horizons[]` | `[20,40,60,120,250]` / `[20,40,60]` / 同V3 | 严格递增、不重复正整数；基础动量和残差化列 |
| `market_state_momentum_horizons[]` | `[120,250]` / `[]` / `[]` | 可为空的递增正整数；当日总体均值 |
| `heads.tomorrow[]` | 第3.5节10 / 6 / 6列 | 非空、不重复字符串；特征选择和列序 |
| `heads.d25[]` | 第3.5节9 / 5 / 5列 | 同上；不能把Tomorrow首列自动补入D25 |

`factor_index/factor_contract/news_signal`由A维护公式与解析，C只提出列、单位、锚点和缺失需求，不修改这些公共配置。

### 6.2 作业资源配置与仍待核对的常量

P54本批按用户授权由C接通公共解析及训练组合根。`config/training.json.resources`必含`hdd`及`ssd`两份独立机器策略，每份具有下表字段；默认保持原值，尚未以磁盘名称推定CPU/内存大小或启用性能候选。两台个人电脑分别依据自身实测调整对应策略，另一策略不随之变化。组合根按runtime的`disk_medium`或原磁盘探测结果选取，unknown明确沿用现行保守HDD策略；无静默补默认值，任一机器段缺字段、未知字段、bool冒充整数或非法范围均拒绝。

| 每机器策略字段 | 默认值、单位与范围 | 最终消费者 |
| --- | --- | --- |
| `cpu.coordinator_cpus` | 1，整数≥0，CPU | 从当前进程亲和性/cgroup可用CPU中预留；至少留下1个拟合槽 |
| `cpu.maximum_threads` | null或整数≥1，线程；null不另加配置上限 | 总拟合预约容量再受当前机器可用CPU收紧 |
| `cpu.native_threads` | 2，整数≥1，线程 | 环境变量及全作业原生库基线，实际不超过总预约容量 |
| `memory.maximum_bytes` | 3221225472，整数≥1，字节 | 拟合预约上限，不是RSS硬隔离 |
| `memory.reserve_bytes` | 1610612736，整数≥0，字节 | 当前宿主MemAvailable及每个可见cgroup限制减去此预留 |
| `memory.minimum_available_bytes` | 268435456，整数1–maximum_bytes，字节 | 宿主低水位延续原行为；不抬高更紧的cgroup限制 |
| `fanout.batch_rows` | 256，整数≥1，行 | 公共历史流每次取数/派发行数 |
| `fanout.queue_batches` | 2，整数≥1，批/档 | 每档独立队列容量；另外有一个接收中批次及公共派发批次 |
| `samples.sqlite_cache_bytes` | 33554432，整数≥1024且为1024倍数，字节 | V2/V3/V5样本连接页缓存；连接边界转换为KiB |
| `samples.sqlite_mmap_bytes` | 0，整数≥0，字节 | V2/V3/V5样本SQLite mmap；不控制NumPy映射 |
| `samples.matrix_memory_bytes` | 268435456，整数≥0，字节 | 单行业独立矩阵超过此值用受管文件映射；0允许强制映射 |
| `samples.sequence_cache_bytes` | 33554432，整数≥0，字节 | 每个V5序列reader的已完成块缓存；另有原256窗在途批次 |
| `samples.sequence_writer_codes` | 8192，整数≥1，证券 | 序列writer保留尾部的证券数；逐出只减少压缩机会，不改变值 |
| `samples.v1_sqlite_cache_bytes` | 16777216，整数≥1024且为1024倍数，字节 | V1独立临时样本连接页缓存 |
| `samples.v1_batch_rows` | 4096，整数≥1，行 | V1具名矩阵有序流每次取行；跨批排序、query和负零不变 |
| `samples.tcn_batch_memory_bytes` | 268435456，整数≥0，字节 | 单头训练/早停TCN批次超过此值映射；0允许强制映射 |

`infra/settings/training.py`是根schema和资源解析唯一owner；仅训练启动及明确训练诊断读取，不让推荐或只读Web因训练资源错误而失效。不可变资源对象由bootstrap显式传至准入、训练请求及fanout，无消费者自行重读资源JSON。配置改后下次训练作业生效，不热切换运行中的作业。普通模型请求2线程及V5的`tcn.compute_threads/batch_size`仍是原拟合合同，未以运行批次覆盖；纯资源段不加入模型/样本参数身份。本次源码变化仍会改变既有宽范围implementation identity，不自动重训、发布或改写旧工件。线程设置可能影响实际数值路径，候选调值须核对数值/质量与D实测，不承诺任意调值逐位相等。

阶段345接续将八项样本I/O运行参数按原值迁入现有资源段，由唯一解析器生成不可变`TrainingSampleSettings`，训练组合根显式传到四档、缓存及所有读写者；普通生产构造不补默认值。完整`hdd/ssd`段都须符合schema，缺字段、未知字段、bool/小数冒充整数及非法字节单位明确拒绝；本批实际验收仅在当前HDD，不改变SSD那台PC的验收进度或冒称已测最优值。训练诊断从同一配置边界注入，历史对照worker只按明确选定的冻结checkout及其已声明配置schema使用对应API，不向产品安装兼容分支。

纯I/O参数不加入拟合参数或样本语义身份；配置变值不会单独强制重训，训练源码变化仍沿既有implementation identity生效。删除训练合同hash中原固定的两项SQLite缓存常量，实际八项值在`selected_training_result.sample_resources`按白名单报告。任何改值仍需数值、质量与完整资源/性能对照；软件内存预算不是OS隔离，不保证任意增大缓存或批次均满足RSS门。

下表区分已迁运行参数、保留安全估计及格式/算法常数，不能将参数接线视为性能通过。

| 实现与现值 | 实际消费者/作用 | 给A的要求及身份边界 |
| --- | --- | --- |
| CPU现值留1、原生基线2 | `TrainingCpuBudget`及训练启动环境/原生上下文 | 已由cpu资源段注入；普通拟合请求2仍保留独立算法合同 |
| 每档队列2、公共批次256行 | `_TrainingFanout`发送/接收与取消清空 | 已由fanout资源段注入；不是V5张量batch_size |
| 拟合容量最多3GiB、预留1.5GiB、宿主低水位256MiB | `FittingMemoryCapacity → TrainingMemoryBudget` | 已由memory资源段注入；实际cgroup各级上限继续收紧，约束不可核验时拒绝 |
| `engine.py`TCN预约512MiB，普通模型256MiB＋最大行业矩阵估计×4 | 拟合前内存预约 | 先实测估计与RSS误差；不能因常量小就直接降低预约 |
| `sample_index.py`页缓存/mmap、独立矩阵阈值 | 样本index及行业独立矩阵 | 已由samples前三字段注入；共享行业矩阵仍按原设计始终映射，不被此阈值改成驻内存 |
| V1 `samples.py`页缓存及具名批读 | V1临时样本及最多8矩阵 | 已由samples.v1两字段独立注入；矩阵仍按原设计映射 |
| `sample_sequences.py`reader缓存及writer尾部证券数 | V5重叠序列读写 | 已由samples.sequence两字段注入所有入口；256窗在途上限为批次安全边界，保留 |
| `profile/v5/batch_cache.py`训练/早停矩阵阈值 | 单头TCN批次缓存 | 已由samples.tcn_batch_memory_bytes注入；取消及异常均关闭映射 |
| 序列块64步、float64小端；序列60×6 | 样本codec/特征合同 | 格式/算法合同，暂不列为自由运行参数；变化需重建身份和全部消费者 |
| 序列查询每组400对键；每批最多256窗 | SQLite变量可移植性及在途内存 | 安全/结构边界，保留原值，不开放为绕过999变量上限的参数 |
| `cpu_admission.py/memory_admission.py`预约心跳0.25秒，fanout队列轮询0.1秒 | 等待与取消 | 停止响应约束；是否开放配置须有用途和等待证据 |

### 6.3 预约顺序与资源生命周期

当前 `engine.py` 在样本检查完成后，先预约CPU，再估计并等待内存，预约持有到双头行业拟合及后续准备范围退出；这是源码顺序，不是等待耗时实测。可用CPU为4时容量3、原生基线2，每项至少预约2，两个拟合无法同时入场；V5请求1也不绕过原生基线预约。限制原生线程有正确性作用，不能只删门让四档竞争宿主。

| 资源 | 创建/唯一owner | 消费与退出 |
| --- | --- | --- |
| 执行器 | `bootstrap.ExecuteSelectedTraining`，按所选档数创建 | entrypoint启动；取消后停止、等待，不在工作任务中同步提交并等待同池任务 |
| CPU/原生线程限制 | bootstrap创建 `TrainingCpuBudget`；`TrainSelectedProfiles`统一上下文 | 全部worker结束后才恢复原生库；reserve的finally归还额度 |
| 内存额度 | bootstrap创建 `TrainingMemoryBudget` | context退出归还，超预算明确拒绝；预约前/等待中进度回调检查取消 |
| 固定历史/训练锁 | `TrainSelectedProfiles`的ExitStack | 所选profile锁和历史维护锁覆盖作业，退出释放；不随新发布切换输入 |
| 每档有界队列 | `_TrainingFanout` | 消费者退出后停止派发；异常等future退出，finally清空遗留批次 |
| V2/V3/V5样本/矩阵 | `TrainingSampleCache`及 `SQLiteTrainingSampleIndex` | 已封存样本可复用，临时矩阵上下文关闭；异常不能把未封存样本当成功 |
| V1临时库/矩阵 | `TemporaryTrainingSamples`与V1 ExitStack | 映射关闭后清理临时目录，不承诺跨作业样本恢复 |

Linux cgroup路径由training内部`ReadTrainingCgroupScopes`唯一解析，CPU和内存共用membership、mountinfo、挂载根及可见父级，支持v1/v2和挂载路径转义；不受无关bind mount影响，不越过可见挂载根猜测宿主限制。CPU无法核验仍按1核保守运行；内存约束无法映射、读取或解析时抛出`training_memory_limit_unavailable`并保留原因，不退回宿主3GiB预算。内存容量和预约都要求正整数，拒绝bool、小数及非有限值。

内存v1读取`memory.limit_in_bytes`，v2读取`memory.max`及`memory.high`，沿可见父级按每个有限限制减去配置预留（默认仍1.5GiB），再收紧现有拟合预算；没有启用控制器或明确无限值不增加限制。`memory.high`用于保守预算，不是硬隔离，语义见[Linux cgroup v2文档](https://docs.kernel.org/admin-guide/cgroup-v2.html)；v1字段见[Linux内存控制器文档](https://docs.kernel.org/admin-guide/cgroup-v1/memory.html)。预约顺序、常量和调度策略未改变，线程/RSS峰值与性能仍由D实测。

测量必须分别输出请求、授权和实际原生线程数，预约等待、持CPU等内存时长、实际并行区间、取消至归还时间和最终在途数。`model_fit` 心跳把多种等待放在同一阶段，不能据该阶段总时长反推CPU或内存等待各占多少；现有作业计量按第6.4节分开报告预约与队列区间。

### 6.4 作业耗时的实际计量口径

来源：`training/application/resources.py`的不可变计量类型、`infra/execution_timing.py::TrainingExecutionMeasurements`和 `entrypoints/training_timing_projection.py`的白名单投影。每次所选档位作业一份计量对象，注入单调时钟；不增加后台采样线程、不读取活动数据库，也不参与资源准入决策。

`selected_training_result.timing`在成功、失败和取消报告中提供观测。纯编排测试或没有返回计量的调用保留null；尚未开始的档位起止为null，不能以0秒冒充已完成。现有统一训练诊断把该结果保留在 `summary.initial.execution`和 `summary.repeat.execution`，没有增加第二个入口或改A的公共分派。

| 字段路径，相对timing | 单位与含义 | 不能据此推断 |
| --- | --- | --- |
| `source_read_seconds` | 秒；共享流每批取数的累计耗时，含价格读取、解码、日历对齐 | 不等于纯磁盘I/O，未包含打开历史和前期复用检查 |
| `peak_cpu_reservations` | 同时进入CPU预约体的观测峰值，次数 | 不等于实际同时拟合的模型数或原生线程数 |
| `peak_memory_reservations` | 同时进入内存预约体的观测峰值，次数 | 不等于实际RSS或宿主硬隔离 |
| `profiles[].profile` | v1/v2/v3/v5，保持请求顺序 | 不按完成顺序重排档位身份 |
| `started_after_seconds` | 秒；该档worker相对计量对象创建时点的开始偏移，可空 | 不包括该档worker开始前的准备工作 |
| `finished_after_seconds` | 秒；worker退出偏移，可空；异常/取消也记录 | 不等于全部作业的收尾/提醒完成时点 |
| `queue_send_seconds` | 秒；向该档队列发送的累计区间，含阻塞、轮询及终止通知 | 不全是纯阻塞时间，不能当特征计算时间 |
| `queue_receive_seconds` | 秒；该档取下一批的累计区间，含空队列轮询 | 不等于源读取时间；不同worker之间会重叠 |
| `cpu.wait_seconds`、`memory.wait_seconds` | 秒；进入原预约端口至授权的耗时，失败等待同样累计 | 包含快速路径、心跳与进入开销，不只统计Condition阻塞 |
| `cpu.hold_seconds`、`memory.hold_seconds` | 秒；已授权预约体的累计持有区间，退出体时结束 | 不含底层释放收尾，CPU持有可能包含等内存和矩阵准备 |
| `cpu.acquired_count`、`memory.acquired_count` | 次；成功授权数 | 不是拟合行业数或新训练次数 |
| `cpu.failed_acquisitions`、`memory.failed_acquisitions` | 次；进入预约时未获授权即失败或取消 | 拟合体内失败不计为预约失败 |
| `cpu.inflight_count`、`memory.inflight_count` | 次；快照时尚未退出的预约体数 | 归零不单独证明原生线程、映射和底层资源都已释放；仍需生命周期回归 |
| `cpu_held_while_memory_wait_seconds` | 秒；该档已持CPU时进入内存预约的累计等待，包含失败等待 | 不能把CPU持有整段都当这类等待 |

等待、持有、任务、队列和来源区间会相互重叠，不能把所有项相加当墙钟。全作业墙钟仍看 `selected_training_result.elapsed_seconds`；实际请求/授权线程沿用CPU预算字段，真实原生线程及RSS另测。计量只在批次和预约边界读时钟，不逐历史行采样。

计量继续包住原 `TrainingCpuAdmission/TrainingMemoryAdmission`，原请求、授权、心跳、异常及释放语义不变；V1和其它三档都接入。全部工件复用时没有源批读或拟合预约，必须同时看到扫描0和预约授权0，不能只看耗时很短。

当前 `TrainingImplementationIdentity`覆盖training全部源码；加入计量也会改变该实现摘要。这是现有身份范围的实际限制，不代表特征、标签或参数改变，不会自动重训或改写旧工件。复用验收要在同一源码身份内做，是否将纯观测从语义身份中分离另行核对全部消费者后裁决。

### 6.5 调度策略的隔离对照

P53候选仅由既有`training_alignment`worker的`--scheduling-candidate`显式选择，默认baseline；生产组合根及训练准入不导入该诊断模块。fixed使用每档份额上限，long_task_first在当前已就绪的等待者中按D提供的拟合耗时降序授权，borrowing允许就绪者一次借用当前空闲容量；都不抢占运行中的拟合，不把尚未就绪的长任务当作必须等待的任务。

份额为max(原生基线, 总CPU预算/本次档数的整数部分)，是每档上限而非各自独立的一份总预算。实际授权始终≤算法请求且≤原CPU容量，原生基线必需预约仍扣除；借用可以在只剩部分空闲容量时少于原请求，不能超请求加线程，模型拟合中不动态升降线程。原`TrainingCpuBudget`仍最终预约及管理原生限制，原CPU后内存顺序和内存预算不变。默认普通请求2/V5请求1及原生基线2可能让策略没有可用收益，不能因此删除约束或宣称已提速。

三个非baseline候选都必须另给`--scheduling-evidence <对应PC证据.json>`。输入根字段严格为schema_version、source_hash、configuration_hash、implementation_hash、medium、available_cpus、cpu_capacity、native_threads及profiles；schema_version固定training-scheduling-evidence，三个摘要分别匹配本次快照、既有training/features组合摘要与训练实现身份。medium必须是本次已确认hdd/ssd，未知介质拒绝。profiles按本次请求顺序列出唯一profile及有限正数fit_seconds（拟合耗时秒），由D在该PC相同输入上实测提供，不用猜测排序或另一台机器的数据填充。CPU三项在实际端口创建时再次比较，不能跨机器预算或在同一计量对象中偷换预算。

候选输出原计量与请求/实际授权/原生扣额/超固定份额借用数，以及规范化证据摘要、候选源码摘要、排队/预约残留。全部复用仍零扫描/零拟合预约；结束时排队和预约不归零不计完整通过。finally恢复计量端口的原绑定；非法模式、错误证据、取消、拟合失败不得使候选留在后续正常作业中。baseline及历史版本比较不导入新候选模块，保留原比较能力。

D可在原内部worker命令追加`--scheduling-candidate fixed|long_task_first|borrowing --scheduling-evidence <证据>`，其它历史、四档、样本复用与工件复用参数沿用e9.1。需要在HDD/SSD两台PC各自比较全作业关键路径、数值/质量、资源峰值与取消收尾，单个fixture或单档回归不签四档性能。当前只交付隔离候选与功能证据，production_enabled/performance_evidence保持false；D达标后再由C在原P53记录完成唯一生产owner及全部消费者的原子切换。

## 7. 缓存、复用、检查与工件发布

正常复用检查版本、字段、维度、封存元数据和身份，不重算下载全库内容或每次做整文件摘要。完整内容审计单独显式运行。训练、早停、校准、评价的隔离、标签成熟、特征顺序和目标对齐必须保留。

特征公式、历史端点、单位、列顺序和缺失掩码由同一合同维护，训练、在线和回放不能各写一份近似版本。到期判断看成熟标签和真实语义修订；文档提交、纯物理压实和无变化重跑不能伪装成新标签或已重新拟合。

当前已有同档双头行业共享读取、4096行批读、较大矩阵临时映射、V5重叠序列块和统计小表；下一步核对是否实际命中及退出释放。V1/V5模型、样本和决策身份独立，不借别档补工件。

每档先写候选双头和报告，再检查完整工件组、输入/配置/特征身份和工程资格，通过才切换。取消、单头失败、校准拒绝或落盘失败均保留上一有效模型；复用不刷新拟合日期，不把复用标成新训练。

训练工件字典至少说明：档位、策略头、算法、样本/特征/数据身份、日期范围、拟合/早停/校准区、行业覆盖、质量状态、模型文件及摘要、发布版本。定义和全部消费者由训练负责，不由Web推断。

### 7.1 封存样本与模型文件

以下是持久化边界字段，不是内部业务对象要求自行序列化。V2/V3/V5样本缓存的 `complete.json`由 `TrainingSampleCache.seal`写、`inspect/audit`读；六个字段均必需，不允许空值。

| 字段 | 类型/单位 | 作用 |
| --- | --- | --- |
| `identity` | string，摘要 | 当前历史、split、特征与存储语义身份 |
| `sha256` | string，64位十六进制 | 首次封存计算的全文件摘要；显式audit重新计算 |
| `sample_format` | string | 命名列schema/特征和序列布局合同 |
| `split_hash` | string，SHA-256 | 日期分区身份，与库内BLOB解码值一致 |
| `size_bytes` | int，字节 | 封存大小，普通复用检查 |
| `modified_ns` | int，纳秒 | 文件修改戳，普通复用检查；不是训练业务时间 |

普通复用比较身份、布局、split、大小、修改戳及库内合同；首次封存做fsync和全文件摘要，显式 `audit()`再次逐字节读。不能将“普通复用不重算摘要”写成“从不校验内容”，也不能把未封存库当可用缓存。

模型路径固定为 `data/training/models/<profile>/{tomorrow,d25}/{model.json,metadata.json}`。V2/V3/V5由 `engine.py`准备、`PublishProfileBundles`切换；V1由 `PublishLearnedRankPair`切换。每档双头共用本次作业身份但保持独立模型和目标；单头候选不能单独替换正式目录。读取路径分别为 `LoadHeadBundle/InspectPublishedProfileBundles` 与 `ReadLearnedRankPair`，推荐在启动时通过 `profile_factory.LoadScoringProfile`采用。

V2/V3/V5的 `metadata.json`顶层白名单如下，JSON字段类型及非空要求由 `model_bundles/bundle_codec.py`校验；除表内明确说明外均必需。

| 字段 | 类型/中文含义 | 来源和消费者 |
| --- | --- | --- |
| `schema_version` | string，档位头metadata格式 | 必须匹配档位 |
| `profile_id` | string，v2/v3/v5 | 不跨档读取 |
| `model_id` | string，具体算法头身份 | 必须匹配 `TrainedHeadContract` |
| `strategy_head` | string，tomorrow/d25 | 不按目录名擅自重解释内容 |
| `feature_ids` | 有序string数组 | 与该头manifest完全一致 |
| `feature_units` | 有序string数组 | 与列一一对应，V5序列含ratio/log_shares/log_yuan |
| `exposure_contract` | object，市场/板块/行业/对数均额及顺序 | 残差化语义，decoder严格核对 |
| `algorithm` | string，lightgbm/ridge/tcn | 决定行业模型decoder |
| `dependencies` | object，库名→版本string | V2为numpy/lightgbm，V3为numpy，V5为numpy/torch |
| `training_input` | object，训练输入凭证 | 见下表，不能混入另一历史或头 |
| `validation_report` | object，工程评价报告 | 切分/目标/模型身份及实际覆盖、预测诊断；收益资格独立 |
| `industry_sample_counts` | object，行业→计数 | 每行业 `training_rows/validation_rows`，与报告预测覆盖交叉核对 |
| `model_hash` | string，SHA-256 | 与model文档content_hash一致 |
| V5 `training_configuration` | object，fit_dates/parameters/content_hash | 完整TCN参数及架构摘要，非V5不能带此字段 |
| `content_hash` | string，SHA-256 | metadata自身规范化内容摘要 |

`training_input`的嵌套字段按持久化白名单核对如下。表中摘要均为64位十六进制string，日期为ISO日期string，计数为int；不含可随意补零的缺失字段。

| 字段 | 中文含义/约束 |
| --- | --- |
| `schema_version` | 该档头训练输入格式 |
| `profile_id`、`strategy_head` | 所属档位、策略头，各自独立校验 |
| `training_input_scope` | 固定 `complete_manifest` |
| `training_input_hash` | 完整训练输入身份 |
| `training_run_hash` | 同档本次作业身份，双头一致 |
| `label_cutoff` | 标签截止，早于source_cutoff |
| `source_identity_hash` | 已发布历史源身份 |
| `calendar_hash` | 固定市场日历身份 |
| `source_cutoff` | 已发布历史资料截止 |
| `requested_sessions` | 请求历史日期数，1250–2000；不是fit_dates |
| `input_descriptor_hash` | 输入描述身份 |
| `training_input_codes`、`training_universe_codes` | 实际输入数与固定总体数，均>0且相等 |
| `codes` | 唯一、排序的六位代码数组，长度等于输入数 |
| `feature_manifest_hash` | 有序特征与单位合同 |
| `split_hash` | 日期分区身份 |
| `training_contract_hash` | 该档模型训练合同 |
| `label_target` | 该头目标定义，与合同一致 |
| `training_cost_bps` | 固定0，标签未预扣成本 |
| `validation_scope` | 固定 `daily_close_engineering_proxy` |
| `production_authority` | bool，固定false，工程文件不能授权收益或自动生产切换 |
| `content_hash` | 输入文档自身摘要 |

`model.json`由 `industries`及自身 `content_hash`组成。每行业必须有逐列 `transformer_means/transformer_scales`与 `calibration_intercept/calibration_slope`，标准差尺度>0、向量维数匹配；V2另有 `lightgbm_model/lightgbm_best_iteration`，V3另有 `ridge_intercept/ridge_coefficients`，V5另有 `tcn_network`。TCN网络记录contract、architecture、hidden_channels、compute_dtype、dropout及具名权重的shape/values；不能只取一个能加载的权重文件就当完整工件。

V1的两个文件使用独立白名单：model包含 `schema_version/profile_id/strategy_head/pair_content_hash/head_content_hash/components/content_hash`；metadata除共同身份外还含 `model_id/score_scale/model_hash/contract_hash/feature_ids/algorithm/training_input/validation_report`。其中pair摘要保证双头同组、head摘要保证各头独立；components保存排序器、独立收益门、标准化和校准等具名分量。V1输入记录完整settings、训练/源/日历身份、资料截止、分区及头输入证据；报告明确 `feature_anchor=previous_completed_session` 与 `label_anchor=signal_close_proxy`。字段细节以 `profile/v1/models.py`与domain类型为准，不能用通用metadata解码器猜测。

评价报告的逐项预测指标由 `model_bundles/evaluation_codec.py`维护，研究准入归c第15章；本节不复制第二份指标公式。报告必须保留工程结构、预测诊断和 `daily_close_proxy_not_point_in_time`的收益缺口；人工生产授权不改写这些证据。

### 7.2 临时样本和拟合映射的中断恢复

临时对象仍由 `training/infra/sample_cache.py`负责，调用者在整个使用与恢复期间持有该档训练锁。正常完成或取消先退出样本连接、映射和资源上下文，再清理临时目录；进程硬退出由操作系统释放连接、映射和锁，下次显式训练在取得同一锁后恢复遗留对象，不依赖旧进程执行清理。

V1临时样本位于 `data/training/samples/v1/.temporary-training-v1/training-<32位十六进制标识>/`，仅识别 `samples.sqlite3`及其journal、`matrices/0`至`matrices/7`下的 `features.bin/targets.bin`。空命名空间和部分创建的上述对象也可恢复；重启后重新建样本，不把临时样本升级为可复用封存缓存。清理前先核对全部受管工作目录，发现未知文件、目录或符号链接就拒绝清理并保留全部现场；旧版或用户自有文件不能仅因位于v1目录被删除。

共用样本index的拟合矩阵由`sample_index.py`创建，恢复仍由`TrainingSampleCache`负责。`sample-fit-<8位标识>`只包含`training/early_stopping/calibration`各自的`-features.bin/-labels.bin`，`sample-shared-<8位标识>`只包含`common-features.bin/common-labels.bin`；标识仅小写字母、数字及下划线。空目录或固定文件的部分创建可恢复，名称不符合、未知文件、子目录或符号链接均拒绝清理。目录名只有前缀相似不能证明归属。

V5大行业的 `tcn-batches-*`目录仅包含只读拟合缓存 `training/early_stopping`，属于封存样本之外的临时映射。`TrainingSampleCache.open()`在锁内先核对固定缓存路径，再统一检查fit、shared及TCN三类残留，全部通过后才开始清理；预检发现任何一类异常都保留其它类现场。合法遗留映射可清理后重开原封存样本，不改写 `samples.sqlite3/complete.json`。小行业的内存缓存沿用同一上下文释放语义；成功、填充中取消或拟合中取消均关闭本轮映射。

固定缓存路径`complete.json`、`samples.sqlite3`及其`-journal`在检查、打开、审计和清理入口共用普通文件/符号链接预检。清理先核对全组路径，随后仍按完成标记优先的原顺序删除；预检发现DB或journal为符号链接、目录等异常类型时，完成标记和其它临时映射都保持原样。实际删除遇I/O错误或再次硬退出仍可能留下已核对布局的部分残留，下次按同一合同恢复。不增加数据库行或完整内容扫描；实现与验证证据只记g对应阶段。

上述临时样本恢复不处理模型发布目录；模型发布使用下节独立边界。全市场训练的实际资源峰值、SSD和性能对照仍需各自验证，不能用恢复夹具代替。

### 7.3 双头模型目录发布的中断恢复

显式训练在持有该档训练锁时，于复用检查之前恢复中断的目录发布；直接模型发布也执行同一恢复检查。恢复操作另持有该档发布锁，唯一实现位于`model_bundles/profile_publication.py`，V1使用自己的双头decoder，V2/V3/V5使用各自训练合同。推荐、HTTP/Web及普通模型加载保持只读，不通过加载触发恢复。

正式档位目录缺失时，只接受唯一的`.<profile>.previous.<32位小写十六进制标识>`。先核对受管路径、两头固定文件和实际模型内容及同组身份，再将旧目录整体移回；不把`.<profile>.publication.*`中的候选提升为正式模型。正常训练中的`.bundle-staging.<8位受管标识>`可以随旧目录保留，但只允许普通目录和固定模型文件；未知条目、符号链接、多份旧目录、半套旧头或模型损坏均保留现场并明确失败，不选择某个目录猜测恢复。

首次安装若只留下空目录或合法暂存工作目录，恢复该工作目录后仍无可复用模型，训练按原规则重新构建。新目录已经完成移入时保留该新组及其身份，不回滚到旧组；本轮不自动清理硬退出留下的其它候选或旧目录。恢复旧组后，配置、源资料、训练实现和日期分区仍须通过原复用合同；不匹配时正常重新拟合，不改写旧模型身份或收益资格。进程硬退出回归与SSD/HDD性能实测分别记录在g，不能互相替代。

### 7.4 公共计算与校验的隔离候选

P52/P55的候选由既有`sample_comparison_worker --construct-features`或完整作业`training_alignment`注册worker显式选择，生产训练不导入`computation_candidates.py`。`--computation-candidate`为`baseline/indexed_anchors/schema_cache/combined`，缺省baseline；样本worker离开特征构建模式、完整worker处于inputs-only模式均不能启用候选。C后台从同一冻结checkout、配置和输入在当前PC运行，数值摘要一致后再比较整作业成本；两台PC进度分别保留，不能把单轮报告或fixture当性能通过。

- indexed_anchors在同一次纯计算中构造一次只读lag索引，再调用现有`CalculateProfileQfqAlpha`，不重写公式，也不跨股票、日期、总体或档位复用原始特征。V1独立完成日输入不切换，V2不同窗口及V3/V5不同总体不会被合并。
- schema_cache最多保留64个完全相同的不可变列序/序列布局校验摘要；实际摘要仍由`SampleFeatureHash`首次校验/计算。列序、布局值与标量类型均区分，异常不缓存。库内schema、feature/split身份、样本形状/值、标签成熟和防泄漏检查全部保留，不把“曾校验”当作跳过逐行或外部内容审计的资格。
- 候选只允许在隔离worker主线程安装；完整作业的四档线程沿原执行器运行并在退出候选作用域前收尾，计数加锁，LRU允许并发首次计算同一键。样本构建/拟合过程中可按原语义失败或取消；finally恢复三个原调用绑定并清空本轮缓存。唯一白名单投影列出索引调用、schema请求/实际计算和缓存容量，production_enabled及performance_evidence均为false；计数锁与钩子开销包含在候选耗时内，完整对照另行裁决。

使用既有统一入口的内部worker参数，在外部输出目录运行：`python3 -m scripts.diagnose_runtime --internal-worker scripts.checks.training.sample_comparison_worker -- --checkout <冻结版本> --root <全新仓库外目录> --size 64 --sessions 2000 --construct-features --computation-candidate combined --runtime-config <对应PC配置>`。baseline及两种单候选分别运行，不能只测组合后将收益归给某一项；该fixture最大128代码，不替代真实完整负载。

完整三负载沿用同一个注册worker：`python3 -m scripts.diagnose_runtime --internal-worker scripts.checks.training.training_alignment -- --history-root <本机合法历史> --work-root <全新仓库外目录> --profiles v1,v2,v3,v5 --product-checkout <冻结版本> --runtime-config <冻结且绑定本机资料的配置> --published-history-read-only --sample-cache-transfer hardlink --computation-candidate combined`。同一候选覆盖首次全训练、全工件复用及样本复用重拟合，保留每次资源计量、数值/指标指纹、源只读守卫和身份核对；V1样本复用仍明确不适用。baseline不安装候选钩子，单候选及combined分别与同输入baseline比较；这里只提供诊断选择，不授权生产启用、活动模型切换或调整10轮方案。

## 8. 质量和收益的边界

工程可读、预测诊断合格、收益验证通过、人工允许生产是四种结论。训练报告不能把Rank IC或拟合成功写成赚钱证明。样本外研究、成本、风险及一次终端留出统一按c第15章及待迁入的现行收益合同。

研究/回溯的字段和存储字典由其实际owner核对：训练拥有样本与工件，推荐拥有决策及结果评价，不为了文档分篇搬动业务owner。历史研究身份、参数和留出记录不能改写成新候选通过。

## 9. 先测再实施的执行顺序

1. 固定真实历史、八头参数、代码、硬件和介质，测首次全训练、样本缓存复用、工件全部复用三种负载。
2. 拆出扫描、对齐、队列阻塞、每档特征、样本写入/封存、矩阵读取、CPU/内存预约等待、逐行业双头拟合、校准、评价、发布与收尾。
3. 找出最后完成的一档和真正瓶颈，量化重复计算占比。先做低风险、可证明相同的共享，再比较资源调度和存储候选。
4. 同输入、覆盖、参数、质量、输出语义做同介质交替或夹测对照；HDD与SSD分别测，不能只改配置名。
5. 达标后原子切换实现和全部消费者，再做必要回归和总耗时复测。验证取消、资源归还、失败保留、部分工件、重启复用。

历史记录：阶段289有V1读取回退5.97%，Parquet没有接生产；阶段297仅部分V5质量组完成，不能签完整质量门。旧台账另记四档一次完整真实作业5822.446秒、7249083行一次扫描；这是一次运行事实，不是同输入串并行性能对照，也不说明90分钟目标或SSD已通过。

窗口、线程或调度候选须经同输入实测后再决定；本篇的字段和计量不表示已取得真实全量性能结果。全部计划和证据状态只记g的P49–P60。

长任务进度还要面向日常操作：当前阶段、真实行/日期/行业/头的单位、已完成工作、预约等待和心跳。未知总量不显示假百分比；软件资源目标和实际宿主硬限制分别报告。

### 9.1 三种负载与可复用的测量入口

以下是三负载测量合同，实际运行状态和结果只记g对应阶段。每次在同机独占测量窗口固定代码提交、配置摘要、发布快照/日历/总体、模型与样本状态、实际磁盘类型和宿主限制；配置写ssd不是设备证明。历史默认从仓库外冻结副本读取；显式选择下述已发布历史只读模式时，可以直接使用配置指向的历史，不复制整套价格文件。训练输出始终使用新建且与历史不嵌套、在项目和活动数据之外的仓库外目录，不清空活动数据或当前模型。

已有`training_alignment`内部worker支持`--published-history-read-only`和`--sample-cache-transfer hardlink`，统一入口的参数整合仍交A按P61处理。前者只接受配置指向的已发布历史，真实训练继续取得原历史/档位锁；诊断边界强制源目录的SQLite连接使用`mode=ro`，将到期提醒查询的`persist`改为false，实际拟合函数保持原样。整次测量前后核对源快照及catalog/价格/参考文件、非空WAL和`.sha256`发布凭证的身份、大小、mtime与ctime；等长改写后恢复mtime仍算变化，凭证为符号链接或非普通文件明确拒绝。变化后报告不可比，不写源库或切换模型。只读模式的这两项守卫在报告中明确标记，不把它写成无诊断干预的产品整链。

硬链接选项只连接本轮隔离准备目录中的合法封存样本，不能连接活动样本或模型；失败明确报告，不隐藏回退为复制。目标执行真实重新拟合及成功清理，前后用流式摘要核验保留的源缓存不变；单列`linked_bytes`，`copied_bytes`必须为0，V1仍不适用。成功核验后仅释放本轮受管准备样本，报告保留前后摘要与释放标记，下一轮不累积整套缓存；发现未知对象先全量拒绝，不删除任何准备样本。该选项减少诊断额外副本，不改变生产样本生命周期或模型参数。资源报告采样文件系统开始及最小可用字节；它包含同盘其它进程的占用，不能冒称本作业精确文件峰值。

隔离历史复制通过SQLite在线备份携带已提交WAL内容，只复制catalog、年度价格及catalog绑定的`company_st.sqlite3`参考，不要求下载准备使用的`industry.sqlite3`缓存。复制前验证源catalog与价格库已有的发布凭证，缺失或损坏直接拒绝；复制后仅为本轮新建副本生成与备份字节匹配的凭证，不补签源文件。前后核对源文件签名及发布/参考身份，副本必须能由当前训练读取者读取；复制、源校验和副本封存共用同一deadline，报告分别列出数据库和凭证数量。

| 负载 | 输入状态 | 必须证明 |
| --- | --- | --- |
| 首次全训练 | 新输出目录，合法历史和原参数完整 | 必要原始扫描1次，四档八头结束时点、质量、总墙钟和峰值RSS |
| 样本复用后拟合 | V2/V3/V5相同有效封存样本，模型确需训练 | 仅从封存样本读取，单列矩阵/序列/拟合成本；V1记不适用。准备该负载需显式隔离布局，不能删除活动模型 |
| 全工件复用 | 同配置/输入，八头工件合法 | 原始价格扫描0次、拟合0次、原工件身份/拟合时点不刷新 |

现有 `diagnose_runtime.py --profile training-performance`在 `build_commands`中专门调用 `BuildTrainingBenchmarkCommands`，继而运行 `training_alignment`；不能仅看profile映射就判成无完整训练。它支持串行前/并行/串行后夹测，或指定隔离baseline checkout作基线前/候选/基线后；子进程的 `_train_once`调用 `ExecuteSelectedTraining`，初次与重复训练会检查八头可读、复用、数值/指标指纹及源身份未变。

生产训练成功后会清理封存样本，样本复用负载对应的是失败后合法保留的缓存。该入口在首次及全工件复用后，用独立准备目录构造V2/V3/V5的这一状态：仍调用真实训练编排，仅在准备期间于拟合入口注入明确失败，核对三档均完成样本封存及覆盖检查、各自恰好到达一次拟合入口、没有生成模型，并恢复原拟合函数。准备报告明确标为故障注入，不能算作正常训练成功，也不计入首次或复用拟合耗时；取消、缺依赖或未到拟合入口不能当成准备完成。

随后把准备目录的`samples.sqlite3`与`complete.json`复制到新的隔离子目录，核对摘要、大小及修改时间，不复制模型，调用恢复后的真实训练用例。报告分别保留准备运行、复制核对和重新拟合的耗时及失败原因；复用拟合必须证明原始价格扫描及读取均为0、模型确实重新拟合、输入身份和模型数值/验证指标一致。准备目录的源封存样本保持不变，复用目标样本在成功后按生产合同清理，不要求成功后仍保留缓存。V1没有封存样本缓存，明确记为不适用，不补造样本命中。

每次训练调用另外采样当前进程的RSS与操作系统线程数，并记录进程CPU时间及`/proc/self/io`物理读写字节差。采样周期0.25秒，峰值是观测下界；线程数包含一个诊断采样线程，不能证明原生线程实际忙碌数量。三个负载及额外准备在同一进程内执行，后续RSS可能包含前一负载保留的内存；整进程峰值单列，不能当成各负载独立峰值。操作系统缓存未主动清空，准备扫描、复制及摘要核对会预热输入和样本，报告保留这一条件。

CPU/内存预约、队列与源批读已有第6.4节计量，但真实全量分段基线、实际拟合重叠及原生线程利用率仍须实测；`stage_seconds`或采样线程数不能替代。重放的工件/报告指纹一致也不等于独立输入上的全部预测回放一致。历史准备复用同一诊断模块的SQLite只读在线备份能力，镜像catalog、价格分区及关联资格/行业库，备份前后核对发布输入身份；备份超时或来源变化不产生成功凭据，不能将目录存在当作冻结完成。实现与实测状态只记g中的对应阶段。

### 9.2 P61训练检测清单与A交接

下表是静态清点和去留建议，未实施搬移或删除。公共入口、报告白名单、`DiagnosticOptions`、Makefile与README由A拥有；训练模块的移动、参数和退出码在A确定统一合同后由C原子切换。新接口拟采用b第9章的业务/模块/检查选择，当前调用仍是现有profile体系，不能把拟定命令写成已可执行。

| 现有路径，均在scripts下 | 功能/输入输出与副作用 | 消费者及去留建议 |
| --- | --- | --- |
| `runtime_diagnostics/training_alignment.py` | 隔离历史全训练/复用/工件加载；也含只读V1 model-inputs；训练模式写外部模型/样本 | `diagnose_runtime.py`、`training_performance.py`及对应测试；保留功能，按输入与整作业检查分组，不能误删model-inputs |
| `runtime_diagnostics/training_performance.py` | 生成串并行/隔离checkout子进程命令，比较总耗时、身份、复用；无网络，子进程写隔离输出 | 公共入口及alignment的复用检查、`test_training_performance.py`；保留编排与唯一比较实现，不能删除仍被alignment调用的函数 |
| `runtime_diagnostics/training_coverage.py` | 格式2样本统计、标签覆盖，只读；不重训 | 公共入口、`test_training_coverage.py`；保留，不将T+1观察投影当完整D25结果 |
| `runtime_diagnostics/training_resources.py` | 合成64×64矩阵，CPU预约/原生限制/取消归还；不读历史、不训练 | 公共入口、`test_training_resources.py`；保留资源冒烟，不作为真实四档并行基线 |
| 当前`scripts/checks/training/training_process_measurement.py`、`training_source_guard.py`（325新增/扩展） | 内部资源采样及已发布历史只读守卫；无独立入口，源SQLite只读、提醒不持久化，退出恢复函数/采样线程 | training_alignment及其定向测试；沿338的当前目录交付，保留显式模式、空间口径和守卫标记 |
| `runtime_diagnostics/training_samples.py` | 五轮样本布局/SQLite–Parquet读取对照，外部checkout及新目录；生成合成库/报告 | 公共入口、`test_training_samples.py`；保留显式实验，不接生产Parquet |
| `runtime_diagnostics/sample_comparison_worker.py` | 指定checkout的样本构建/读取工作进程；写外部夹具 | training_samples子进程；保留为内部worker，随模块路径一起切换 |
| `runtime_diagnostics/training_read_comparison.py` | 已发布历史夹具及SQLite/Parquet准备、对齐、哈希和读测；写外部夹具 | training_samples导入/子进程；保留内部能力，避免与history布局检查重复建夹具 |
| `runtime_diagnostics/v5_samples.py` | 合成或显式只读源子集，宽表/重叠块等价及耗时；写外部样本 | 公共入口、`test_v5_samples.py`；保留序列存储专项，不能当整行业双头训练 |
| `runtime_diagnostics/v5_training.py` | 最多40股隔离导出及有界行业拟合/加载；写外部历史和模型 | 公共入口、`test_v5_diagnostic.py`；保留有界验证，不能关闭最大行业验收 |
| `runtime_diagnostics/v5_kernels.py` | V5数值核对/计时，不读活动历史或生产工件 | v5_training的显式kernel模式、`test_v5_diagnostic.py`；保留内部纯计算检查 |
| `runtime_diagnostics/v5_lite.py` | 配置候选计算及指定样本行业双头质量对照，写外部副本/报告 | 公共入口、`test_v5_lite.py`；保留既有质量门，不把缺少样本的计算结果写质量通过 |
| `check_tomorrow_training_memory.py` | 实为V3隔离重排训练/重复一致性/RSS及报告，显式history/train/sample/output路径；导入时设置原生线程环境 | `test_check_tomorrow_training_memory.py`直接导入；保留有效能力并入训练资源检查后删除独立入口，改掉误导名称；导入副作用须随迁移消除 |

还要与A共同核对 `database_layout_comparison.py`的训练样本/窄扫描负载、`history_sqlite_performance.py`的training_projection，以及 `history_parquet.py`的共享投影，owner不能仅凭文件名判定。前两者是跨业务检查，不由C擅自搬走；Parquet仅供现有诊断比较，不能因生产未采用就删除有效研究能力。

内存检查的间接消费者还包括 `scripts/check_refactor_quality.py::TOP_LEVEL_SCRIPT_MANIFEST`、`scripts/README.md`的工具表、`tests/conftest.py::_TRAINING_SCRIPT_PATHS`。其输出schema `tomorrow_training_memory_gate`被 `training/infra/history/repack_training_evidence.py::ReadTomorrowTrainingMemoryEvidence`用于重排证据核验；该证据消费者必须同步保留字段、摘要和身份要求，不能只更新入口命令就删旧报告合同。

A需接收的具体接口需求如下：

1. 独立train继续使用 `ExecuteSelectedTraining(runtime, scoring_profiles, *, history_root=None, training_root=None)`；它返回聚合退出码，所选档位和资源由唯一组合根组装。切换CLI时保留主线程取消及原有档位选择，不添加自动下载fallback。
2. 基线 `e69d3e02`的 `ExecuteDownloadTrain`活动消费者有 `entrypoints/cli.py::_run_history_download`、`test_profile_selection.py::test_training_selection_prepares_history_once_and_runs_only_selected_profiles`、`test_entry_contract.py::test_train_stops_before_training_on_history_failure`；还须清理CLI中训练专用的下载日历错误投影。两项旧测试期待“先下载”，不能直接当独立训练合同的通过证据。A完成调用者切换时同批删除旧组合函数和旧断言。
3. C接收A的作业级不可变资源类型，字段/单位覆盖第6.2节运行参数；A改公共解析与bootstrap注入，C改训练消费及必要回归。`features.json`由A修改，C只提交第3.5节列序、单位和缺失需求；不改变公式owner。
4. 统一检测需要保留原history/config/样本根、所选profile、隔离输出目录、baseline checkout、候选配置、超时与取消语义；显式标记纯检查、合成写入和真实隔离拟合。当前每项报告的失败/取消/超时/不适用不能压成一个空结果；最终参数与白名单由A统一裁决。

独立train验收至少包含：供应商调用为0、下载断点不变、资料缺失明确失败、所选档位及八头工件按合同、取消退出与资源归还、单头/校准/部分落盘失败保留旧模型。这里只列验收和消费者，没有运行这些测试或业务命令。

### 9.3 V1/V5专项数值与相对质量验收

V1以具名列的float64位模式、负零、每头缺失标签、日期/代码/行业顺序及按日query分组为数值合同；同一次双头读取各有可写矩阵，改动一头不得影响另一头。跨4096行批次的取消或非法值失败由调用者ExitStack关闭全部映射，连接仍可用于下一次合法读取。V5宽列与重叠序列、共享行业读取及TCN批次缓存分别检查数值、顺序、独立双头和异常/取消关闭；这些功能证据不替代D的完整负载性能。

当前`checks/training/v5_lite.py`注册worker支持`--quality-only`跳过内核性能探针，使用显式`--reference-config`及`--candidate-config`固定两组配置。两组拟合日和行业行数门相同，按双方合格行业的Tomorrow训练行数选择最小/中位/最大行业，对每个行业的两头各比较一次；六组全部通过才形成完整相对质量证据。原RMSE不超过参考1.05倍、日期平均RankIC下降不超过0.01的门和失败即停、三小时质量预算保持。数值、确认区、门槛不因结果而调整，不授予收益或生产资格；原确认区不用于反复搜索参数。

用户最新明确默认和后台都用10轮，沿用早停耐心3、相对阈值0.005和1线程；61日、5000行、8通道、float32及batch256保持。此前短暂选择的30轮恢复/参考运行已撤销，未启动真实30轮作业；旧30轮报告只读保留。10轮方案在C27 D25未过原相对质量门的结论不变，不能用10对10补签为通过，也不在同一确认区调参搜索。数值一致性/生命周期、预测相对质量和扣成本后的荐股收益分别判断；较多轮数不构成荐股更好的证据。本次用户恢复C并说明换到HDD PC，已授权新建独立HDD诊断样本；旧SSD输入缺失时保留原失败及身份，不将本机新输入视作其恢复。

已有诊断封存样本直接以--sample-root配合--quality-only使用，不传--prepare-samples，不从完整历史重新生成，也不改旧清单/生产者身份以迎合新实现或拟合配置。显式输入仍按原SampleFormatId、split、源清单摘要及只读复制保护核验，报告保留原来源身份；这不授予新普通训练缓存的复用资格。双副本和缓存使用仓库外独立目录并核对实际设备/文件系统；/tmp是否tmpfs必须逐机核实，不以路径名判介质。质量结果和完整全负载性能分别记录，争用观测不当性能通过。

同一已注册worker的`--largest-only --candidate-config <training.json> --sample-root <封存目录> --work-root <新仓库外目录>`只执行最大双头合格行业的独立数值/生命周期验收。按Tomorrow训练行数及行业名取最大，两头都须满足原行业覆盖门；仅单行业合格也可验。每头按所选配置拟合一次，通过生产模型文档owner序列化、严格TCN解码后逐批核对全部确认区预测精确相等且有限；核对实际预测行数与覆盖，输出独立模型文档，不调用正式工件发布。

该模式显式跳过内核探针及相对质量比较，`quality_evidence/performance_evidence=false`、`models_published=0`。`--cancel-after-batches <正整数>`改为两头各在指定训练批次完成后协作取消，未抵达取消点明确失败，报告`fit_completed=false`，不与成功双头拟合合并成通过；两模式均检查批次目录退出、恢复Torch设置并复核源清单/数据库的文件身份与摘要。输出目录保留报告及独立模型，两个确切快照在入口finally删除；未知文件不清理。不得混用`--quality-only`、`--reference-config`或`--prepare-only`，不得用本模式补签原六组质量门、全工件发布或CPU/内存预约验收。

缺少封存输入时显式使用`--prepare-samples --sample-root <配置samples下的独立子目录>`；`--prepare-only`只准备输入，并明确`quality_evidence=false`。`v5_quality_samples.py`复用同一生产日历/对齐/特征/标签/切分owner，只读已发布历史，在历史及V5训练锁内构建独立诊断身份的封存缓存，保留`samples.sqlite3/complete.json/quality-input.json`供后续复核。正式窗口前的因果预热也报告物理行进度并检查预算，所有源迭代器由调用者关闭。已有目录身份或摘要不符时保留现场并拒绝；合法封存缓存可显式审计后零扫描复用。

质量比较只在新外部工作目录创建两份快照，先检查空间、源文件/完成标记身份、修改时间与ctime，校验两份摘要后再按相同日期读取；源样本及活动模型保持只读。Torch设置在比较退出时恢复，快照仅按确切文件名清理，源缓存和报告保留。执行仍经过唯一`diagnose_runtime.py`入口的已注册内部worker；高层参数解析/统一投影由A拥有，本批不改公共路由。实际命令、冻结版本、输入及配置身份、功能和真实质量结果只记g阶段344。
