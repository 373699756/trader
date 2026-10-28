# 公司风险数据库轻量化与启动加载改造方案

本文件记录本次重构的完整设计和执行顺序，不作为第二份进度台账。实施状态、实际验证结果和未完成项只记入《工程实施》阶段281。本文中的目标结构尚需由实现和验收证明，不能视为已经完成。

评分、风险有效期和准入以[评分逻辑](1_评分逻辑.md)为准；运行和边界以[工程设计](2_工程设计.md)为准。按用户要求，本批暂不修改[下载训练](4_下载训练.md)。行为切换时同步必要的工程设计说明。

## 1. 要解决的问题

此前只读核算的 company 目录约850 MiB，其中当前 company_facts.sqlite3 约631 MiB、退役 evidence.sqlite3 约219 MiB，另有约900 KiB的 eligibility.sqlite3。当前主库混有公告目录、正文、分页成员、续传和正式发布投影，不能把全部空间除以4401理解成单条公司风险记录的体积。

此前样本中的公告目录约60.8万条，正文已检查约2.5万篇；原 published 投影本身约21.5 MiB，并有事件、解禁和待核实原因的JSON数组。仅清理完成分页或执行VACUUM，不能解决正式库职责和字段设计问题。

目标是提取可计算的事实，填入明确的类型字段。正文、标题、URL、供应商响应、失败详情和续传过程全部属于采集资料；正式库只保留数值、日期、状态、枚举及必要的短标识。未知值保持未知，不用0、空串或“没有风险”填满字段。

另一个已观测问题是 history_preload_deadline，边界 require_projection:362，历史预加载预算为300秒。它需要独立定位和实测，不能承诺拆公司库就自动修复，也不能通过放大deadline掩盖瓶颈。

## 2. 文件布局与所有权

```text
data/
  market/
    reference/
      historical_st.sqlite3       # 既有历史ST事实唯一持久化来源
      industry.sqlite3            # 既有行业资料
      inputs.json                 # 既有已发布参考快照，启动读取
      ...                         # 其它既有参考资料，按原合同维护
  company/
    company_facts.sqlite3         # 新的正式结构化事实库
    raw/
      company_collection.sqlite3  # 公告定位索引、结构化输入、分页及重试；不保存正文
      text/<分片>/<内容摘要>.txt.gz   # 当前处理批次的临时正文，发布后删除
```

- download拥有原始采集和正式发布，bootstrap显式注入正式库、采集索引及原始文件目录，不由适配器猜测路径。
- recommendation启动只读取正式库和已发布参考快照，V1/V2/V3共用一次公司事实预加载；评分过程中只用内存对象。
- 原始公告的标题、链接、披露日期和提取状态只在显式查看证据时按股票及证据ID查询；不读取全文，不触发下载。
- HTTP/Web只调用推荐应用层只读用例，不直接访问数据库。原始库缺失时公司事实仍可加载，查看证据明确返回不可用。
- 删除独立eligibility.sqlite3的活动所有者、配置路径、创建逻辑和读写消费者；ST不另存入company。非ST永久排除进入正式库的company_exclusion。
- 不迁移旧company事实、正文、游标或排除记录。实现和隔离验证完成后，停止持有文件的进程，清空本次管理的旧company文件，从零重新采集。既有market/reference的ST资料继续由其原所有者维护，这不是复用旧company数据。

## 3. 正式库的统一存储协议

### 3.1 类型、空值与时间

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

### 3.2 枚举编号

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

### 3.3 待核实与不具备奖励资格原因

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

## 4. 正式库：12张表及全部字段

下列每行对应一个字段。“否”表示NOT NULL；“是”表示允许NULL。表内不保存自由文本、JSON数组或原始公告列表。事件子表保留确有用途的多条事实，因此4401只股票对应4401条主记录，不意味着整个数据库只能有4401行。

### 4.1 company_facts：每只股票一条当前事实主记录

主键code。共24列。它提供启动加载和组件完整性判断，不承担公告归档或历史评分快照。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| code | TEXT | 否 | 六位股票代码；主键及各事实子表的归属键 |
| financial_report_date | INTEGER | 是 | 最新可用财报报告期；判断报告期和有效性 |
| financial_disclosed_at | INTEGER | 是 | 财报首次可用披露时刻；防止使用决策时点之后的信息 |
| financial_state | INTEGER | 否 | 财务组件data_state；区分缺失、部分及完整 |
| basic_eps | REAL | 是 | 基本每股收益，元/股；财务与风险规则输入 |
| book_value_per_share | REAL | 是 | 每股净资产，元/股；净资产相关判断 |
| revenue_growth_pct | REAL | 是 | 营收同比增长百分比；成长规则输入 |
| net_profit_growth_pct | REAL | 是 | 归母净利润同比增长百分比；成长规则输入 |
| core_profit_growth_pct | REAL | 是 | 扣非净利润同比增长百分比；成长规则输入 |
| roe_pct | REAL | 是 | 净资产收益率百分比；盈利质量规则输入 |
| parent_net_profit | REAL | 是 | 归母净利润，人民币元；盈亏及财务规则输入 |
| core_net_profit | REAL | 是 | 扣非净利润，人民币元；主营盈利及财务规则输入 |
| pledge_ratio_pct | REAL | 是 | 当前质押比例百分比，0—100；质押风险计算 |
| pledge_as_of | INTEGER | 是 | 质押统计日期；判断来源年龄 |
| pledge_state | INTEGER | 否 | 质押组件data_state；只有完整来源才能将明确无质押写成0 |
| unlock_checked_at | INTEGER | 是 | 解禁范围最近完整核验时刻；用于时效判断 |
| unlock_coverage_end | INTEGER | 是 | 已核验解禁前视范围截止日；判断当前决策日的前视覆盖 |
| unlock_state | INTEGER | 否 | 解禁组件data_state；不能只因没有事件行就判完整 |
| pending_document_count | INTEGER | 否 | 待提取或待检查的相关正文数，非负；解释资料待补 |
| announcement_coverage_start | INTEGER | 是 | 本次事实使用的公告覆盖起日；供覆盖判断 |
| announcement_coverage_end | INTEGER | 是 | 本次事实使用的公告覆盖止日；供覆盖及年龄判断 |
| announcement_state | INTEGER | 否 | 公告风险组件data_state；包含初始风险和待核实约束 |
| checked_at | INTEGER | 否 | 当前事实所依赖资料的核查时刻；展示来源年龄，不等于发布时刻 |
| published_at | INTEGER | 否 | 该股票事实发布时刻；内容不变时保留其原发布时间 |

### 4.2 company_risk_event：已识别风险事件

id为内部主键，UNIQUE(code,event_key)，code外键引用company_facts。风险是否在某个决策日有效仍由领域规则结合状态和时间判断，存储层不另存计算结论。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| id | INTEGER | 否 | 内部事件主键；供减持计划和证据关联引用 |
| code | TEXT | 否 | 股票代码；外键及按股读取索引 |
| event_key | TEXT | 否 | 原有稳定事件身份；跨发布关联同一事件，不以标题作为身份 |
| risk_type | INTEGER | 否 | risk_type编号；选择相应风险规则 |
| subject_type | INTEGER | 否 | subject_type编号；区分上市公司和相关股东主体 |
| risk_level | INTEGER | 否 | 1—4风险等级；与risk_type组合必须满足领域约束 |
| case_number | TEXT | 是 | 正式案号或可核验案件标识；用于同案关联及解除核验 |
| first_disclosed_at | INTEGER | 否 | 首次正式披露时刻；确定事件最早可知时点 |
| last_disclosed_at | INTEGER | 否 | 最近关联披露时刻；保留后续进展时点 |
| effective_at | INTEGER | 否 | 事件生效时刻；不得早于首次披露 |
| status | INTEGER | 否 | risk_status编号；区分有效、解除和撤销 |
| resolved_at | INTEGER | 是 | 解除或撤销披露时刻；非active状态必须提供 |
| expires_at | INTEGER | 是 | 有证据或明确规则支持的失效时刻；NULL不表示已经失效 |

### 4.3 company_reduction_plan：减持事件的额外字段

一条减持事件至多一条，risk_event_id为主键及外键。不能把任意减持公告都解释为已实施减持。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| risk_event_id | INTEGER | 否 | 引用company_risk_event.id，且类型必须为大股东减持 |
| planned_start_date | INTEGER | 是 | 计划减持开始日期；用于计划有效范围 |
| planned_end_date | INTEGER | 是 | 计划减持结束日期；与开始日期校验顺序 |
| planned_ratio_pct | REAL | 是 | 计划减持比例百分比，0—100；保留计划量化事实 |
| completion_disclosed_at | INTEGER | 是 | 完成或终止的正式披露时刻；供既有尾部风险期限计算 |

### 4.4 company_positive_event：已识别利好事件

id主键，UNIQUE(code,event_key)，code外键引用company_facts。只存实际测量和证据状态，不持久化最终加分。不同事件不适用的测量列必须为NULL。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| id | INTEGER | 否 | 内部利好事件主键 |
| code | TEXT | 否 | 股票代码，外键及按股读取索引 |
| event_key | TEXT | 否 | 原有稳定事件身份；关联计划、实施、进展和取消 |
| event_type | INTEGER | 否 | positive_type编号；决定测量字段和分级规则 |
| subject_type | INTEGER | 否 | subject_type编号；明确实施主体 |
| status | INTEGER | 否 | positive_status编号；仅有计划不等于可奖励 |
| first_disclosed_at | INTEGER | 否 | 首次可知时刻 |
| last_disclosed_at | INTEGER | 否 | 最近关联披露时刻 |
| cancelled_at | INTEGER | 是 | 取消披露时刻；cancelled状态必须提供 |
| positive_level | INTEGER | 否 | 0—3已核实级别；非0级别必须有对应milestone |
| ineligible_reason | INTEGER | 是 | 不具备奖励资格的原因编号；NULL表示没有该原因，不等于一定可执行 |
| actual_ratio_pct | REAL | 是 | 实际回购或增持比例百分比，0—100；计划比例不能代入 |
| repurchase_purpose | INTEGER | 是 | 回购用途枚举；仅回购事件适用 |
| contract_amount_cny | REAL | 是 | 已核实合同金额，人民币元；仅合同事件适用 |
| reference_annual_revenue_cny | REAL | 是 | 可比年度营收，人民币元；计算合同相对规模 |
| reference_report_date | INTEGER | 是 | 可比营收报告期；核验测量口径 |
| reference_disclosed_at | INTEGER | 是 | 可比营收披露时刻；防止使用未来信息 |
| amount_to_revenue_pct | REAL | 是 | 合同金额/可比年度营收的百分比；可超过100，不套用持股比例上限 |
| binding_confirmed | INTEGER | 是 | 合同约束性是否核实，0/1；非合同事件为NULL |
| amount_basis | INTEGER | 是 | 金额可比口径枚举；非合同事件为NULL |

### 4.5 company_positive_milestone：首次达到利好级别的证据

复合主键(positive_event_id,level)。达到不同级别的首次时间分别保存，不能每次进展公告都重启奖励窗口。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| positive_event_id | INTEGER | 否 | 外键引用company_positive_event.id |
| level | INTEGER | 否 | 1—3首次达到的级别 |
| qualified_at | INTEGER | 否 | 首次满足该级别的披露时刻；计算交易日奖励窗口 |
| measurement_value_pct | REAL | 否 | 当时达到门槛的实际百分比数值；非负，保留审计依据 |
| evidence_id | INTEGER | 否 | 外键引用company_evidence_ref.id；绑定达标证据 |

### 4.6 company_unlock：已知解禁安排

复合主键(code,unlock_date)。完整前视范围存在主表；没有明细行不能单独证明无解禁。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| code | TEXT | 否 | 外键引用company_facts.code |
| unlock_date | INTEGER | 否 | 解禁日期；计算相对决策日的前视风险 |
| ratio_pct | REAL | 否 | 当日解禁比例百分比，0—100；风险阈值输入 |

### 4.7 company_review：当前待核实事项

id主键；(code,scope,reason,evidence_id)去重，NULL证据也去重。只保留当前有作用的原因，历史采集失败详情属于raw。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| id | INTEGER | 否 | 内部待核实记录主键 |
| code | TEXT | 否 | 外键引用company_facts.code |
| scope | INTEGER | 否 | review_scope编号；确定受影响组件 |
| reason | INTEGER | 否 | 待核实原因编号；决定降级说明和相应动作约束 |
| evidence_id | INTEGER | 是 | 外键引用company_evidence_ref.id；组件整体缺失时可无单篇证据 |
| detected_at | INTEGER | 否 | 该待核实事项第一次被发现的时刻 |

### 4.8 company_evidence_ref：正式事实实际引用的证据键

id主键，UNIQUE(source,source_key)。只保存被风险、利好、达标节点或待核实事项引用的证据，不保存全量公告索引。发布后清理没有任何引用的键。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| id | INTEGER | 否 | 紧凑内部证据主键，供关系表使用 |
| source | INTEGER | 否 | evidence_source编号；与source_key共同定位原始资料 |
| source_key | TEXT | 否 | 供应商稳定记录键；原始标题、URL和正文全部留在raw |

### 4.9 company_risk_evidence：风险事件与证据的关系

复合主键(risk_event_id,evidence_id)，UNIQUE(risk_event_id,position)。保留现有证据顺序，避免“最近证据”语义改变。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| risk_event_id | INTEGER | 否 | 外键引用company_risk_event.id |
| evidence_id | INTEGER | 否 | 外键引用company_evidence_ref.id |
| position | INTEGER | 否 | 从0开始的证据序号；按序恢复领域对象的证据元组 |

### 4.10 company_positive_evidence：利好事件与证据的关系

复合主键(positive_event_id,evidence_id)，UNIQUE(positive_event_id,position)。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| positive_event_id | INTEGER | 否 | 外键引用company_positive_event.id |
| evidence_id | INTEGER | 否 | 外键引用company_evidence_ref.id |
| position | INTEGER | 否 | 从0开始的证据序号；保留事件进展证据顺序 |

### 4.11 company_exclusion：非ST永久排除事实

id主键，UNIQUE(code,reason,source,source_key)。该表无需依赖company_facts存在相同股票：已退市或不在当前采集人口中的股票仍可能需要保留排除事实。不保存ST历史，不保存一般公司风险的第二份结论。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| id | INTEGER | 否 | 内部排除事实主键 |
| code | TEXT | 否 | 六位股票代码；判断永久排除 |
| reason | INTEGER | 否 | exclusion_reason编号；仅人工永久黑名单及已核实退市排除 |
| effective_at | INTEGER | 否 | 排除事实生效时刻；支持按观察时刻读取 |
| source | INTEGER | 否 | exclusion_source编号；明确事实来源 |
| source_key | TEXT | 否 | 配置版本或来源证据键；用于幂等和审计 |
| evidence_hash | BLOB | 否 | 32字节证据摘要；识别内容及检测冲突 |

download应用层提供明确的非ST排除写入用例，组合根将其注入运行时；recommendation通过类型化端口提交新观察事实，不直接操作此表。人工黑名单从现有配置重新生成；旧eligibility内容不导入。写入失败保留内存中的立即限制并显式标记持久化失败，不允许因为落盘失败而放行已知排除股票。

### 4.12 company_publication：正式库发布身份

只允许singleton=1的一行，与事实变更在同一个正式库事务中提交。

| 字段 | SQLite类型 | 可空 | 含义及作用 |
| --- | --- | --- | --- |
| singleton | INTEGER | 否 | 主键，CHECK=1；保证唯一活动发布身份 |
| schema_version | INTEGER | 否 | 明确的结构协议版本，初始为1；旧混合库不作为该版本读取 |
| extraction_hash | BLOB | 否 | 32字节提取身份摘要；包含codec/提取协议及相关规则参数 |
| content_hash | BLOB | 否 | 32字节确定性内容摘要；绑定本次正式事实及排除事实 |
| published_at | INTEGER | 否 | 本次完整事务的发布时刻；与各股票资料年龄分开 |

内容摘要按固定表序、稳定业务键、明确类型和长度编码计算；不能依赖无序SELECT、Python对象repr或自增id分配。排除事实的独立写入也必须原子更新内容身份，避免事实和版本脱节。

## 5. 原始采集库：字段与用途

文件为data/company/raw/company_collection.sqlite3。采用“临时压缩正文＋SQLite采集索引”：PDF由适配器有界下载到内存，解析结束即释放，不落盘归档；压缩正文暂存在raw/text。SQLite负责按股票、日期、来源定位、去重、分页续传、失败重试和受影响股票队列，不保存正文，不参与推荐启动的常规扫描。

**硬盘占用采用滚动批次限制：下载一部分、提取一部分、正式提交一部分、清理一部分，再进入下一批。raw不是长期保存全部公告附件和正文的归档。** 正常路径默认每批最多50篇，沿用有界并发；正文临时目录上限256 MiB，另留64 MiB文件系统余量。派发前按正文字符上限的UTF-8最坏字节量计算本批容量，剩余空间不足时缩小批次；崩溃恢复时先处理已保存正文，仍不足则明确停止新下载。该额度不包括正式事实、采集索引和其它业务数据，不能理解为整个data目录上限。

正式提交成功并核实该篇公告所影响的本次采集人口内全部股票已经发布后，提交回执并删除正文，仅保留必要的来源记录键、URL、标题、披露时间、内容摘要、处理身份和发布回执。无关公告不长期保存完整目录项；待处理项保留，正式事实仍引用的证据定位保留，已完成范围保留紧凑覆盖证明。解析失败只保留原因及重试计划，后续确需重试时重新下载。后续增加股票或改变提取规则时必须重新验证来源，不能将旧回执当作新规则已处理。

正文按内容SHA-256定位，写入临时文件、刷盘并原子替换后，才能将摘要和已检查状态提交到索引；中途崩溃至多留下未引用文件，不能产生“已检查但文件未写完”的事实。读取校验摘要、解压上限及UTF-8。尚未发布且应有的文件缺失/损坏必须回到待补；已有发布回执而按策略删除的文件不算损坏，也不能当作新空正文重新提取。既有确认事件和待核实项从正式库继续保留；先遇到结案/进展公告而尚未找到对应旧事件时保留待核实，之后发现原事件则重新获取关联公告核验。现有分页、重试和完整性约束保留。

以下为保留的采集结构以及新增的company_population。日期TEXT沿用ISO日期，时刻TEXT沿用带时区ISO格式，避免为采集资料增加与正式库无关的格式迁移。除标注“可空”外均非空；组合键、外键及幂等规则在表说明中列出。

| 表.字段 | SQLite类型 | 含义及作用 |
| --- | --- | --- |
| contract.identity | TEXT | 主键，固定company_collection；拒绝把旧混合库当作新采集库打开 |
| disclosures.id | INTEGER | 公告/新闻内部主键；供关联、正文要求和重试引用 |
| disclosures.source | TEXT | 原始来源标识；与source_id组成唯一键 |
| disclosures.source_id | TEXT | 来源内稳定记录ID；与正式证据键关联 |
| disclosures.kind | TEXT | announcement或news；区分公告和新闻，不将新闻直接当官方结论 |
| disclosures.title | TEXT | 原始标题；用于选择待提取正文及展示证据 |
| disclosures.published_at | TEXT | 原始披露时刻；窗口查询和点时过滤 |
| disclosures.url | TEXT | 原始公告/文档链接；下载与显式证据查询 |
| disclosures.digest | TEXT | 采集记录内容摘要；检测原公告修订及拒收旧正文结果 |
| disclosures.body_hash | BLOB，可空 | 正文UTF-8内容的32字节SHA-256；定位raw/text压缩文件并核对内容，未提取时NULL |
| disclosures.body_checked | INTEGER | 0/1，正文是否已完成检查；不能把下载失败记成已检查 |
| disclosures.body_retained | INTEGER | 0/1，当前是否仍保有待发布正文文件；正式提交并清理后为0，不能单凭此列判断提取成功 |
| document_publications.disclosure_id | INTEGER | disclosures外键；与code组成主键，记录该来源版本对每个股票的正式发布回执 |
| document_publications.code | TEXT | 已成功发布的关联股票；多股票公告全部受影响股票完成后才允许删正文 |
| document_publications.digest | TEXT | 已发布的来源记录版本；公告修订后旧回执不能用于清理新文件 |
| document_publications.extraction_hash | BLOB | 32字节提取身份；规则改变后不得继续使用旧提取完成状态 |
| document_publications.published_at | TEXT | 确认正式库提交后的回执时刻；供恢复与清理核验 |
| disclosure_codes.code | TEXT | 股票代码；复合主键(code,disclosure_id) |
| disclosure_codes.disclosure_id | INTEGER | disclosures外键，删除公告时级联；支持一篇公告关联多股 |
| partitions.market | TEXT | 市场范围；与start,end组成分区主键 |
| partitions.start | TEXT | 分区查询起日 |
| partitions.end | TEXT | 分区查询止日 |
| partitions.next_page | INTEGER | 下次应请求页码，初始1；用于续传 |
| partitions.expected_rows | INTEGER，可空 | 来源声明的总行数；未获得时未知 |
| partitions.received_rows | INTEGER | 已接收且校验的行数；用于完整性核对 |
| partitions.complete | INTEGER | 0/1，分区是否完整；未完成不能推进完整覆盖 |
| partitions.checked_at | TEXT，可空 | 分区核查时刻 |
| partitions.failure | TEXT | 原始失败码/详情；成功时为空，正式库只保存归类后的枚举 |
| partition_rows.market | TEXT | 所属市场；复合主键的一部分 |
| partition_rows.start | TEXT | 所属分区起日 |
| partition_rows.end | TEXT | 所属分区止日 |
| partition_rows.source_id | TEXT | 本轮已接收来源ID；参与幂等核对 |
| partition_rows.code | TEXT | 本轮来源ID关联的股票；与前四列组成主键 |
| coverage.market | TEXT | 完整覆盖市场；与start,end组成主键 |
| coverage.start | TEXT | 已证明完整的范围起日；不能仅保留滚动窗口而丢失初始覆盖证据 |
| coverage.end | TEXT | 已证明完整的范围止日 |
| coverage.checked_at | TEXT | 覆盖证明核查时刻 |
| financials.code | TEXT | 股票代码；与report_date组成主键 |
| financials.report_date | TEXT | 来源财报报告期 |
| financials.published_at | TEXT | 来源财报披露时刻 |
| financials.basic_eps | REAL，可空 | 来源基本每股收益，元/股 |
| financials.book_value_per_share | REAL，可空 | 来源每股净资产，元/股 |
| financials.revenue_growth_pct | REAL，可空 | 来源营收同比百分比 |
| financials.net_profit_growth_pct | REAL，可空 | 来源归母净利润同比百分比 |
| financials.core_profit_growth_pct | REAL，可空 | 来源扣非净利润同比百分比 |
| financials.roe_pct | REAL，可空 | 来源净资产收益率百分比 |
| financials.parent_net_profit | REAL，可空 | 来源归母净利润，人民币元 |
| financials.core_net_profit | REAL，可空 | 来源扣非净利润，人民币元 |
| ratios.component | TEXT | pledge或unlock；与code,day组成主键 |
| ratios.code | TEXT | 来源股票代码 |
| ratios.day | TEXT | 质押统计日或解禁日，由component区分 |
| ratios.ratio_pct | REAL | 来源比例百分比，0—100 |
| component_checks.component | TEXT | 被检查组件；与scope组成主键 |
| component_checks.scope | TEXT | market或具体股票等检查范围 |
| component_checks.checked_at | TEXT，可空 | 最近核查时刻 |
| component_checks.failure | TEXT | 来源失败细节；向正式事实转换时归为固定原因 |
| structured_progress.component | TEXT | 结构化来源组件；与scope组成主键 |
| structured_progress.scope | TEXT | 续传范围 |
| structured_progress.start | TEXT | 当前结构化任务范围起日 |
| structured_progress.end | TEXT | 当前结构化任务范围止日 |
| structured_progress.next_page | INTEGER | 下一待请求页码 |
| structured_progress.expected_rows | INTEGER，可空 | 来源声明总数 |
| structured_progress.received_rows | INTEGER | 本轮已确认数量 |
| structured_members.component | TEXT | 当前结构化任务组件；与code,day组成主键 |
| structured_members.code | TEXT | 本轮已确认股票 |
| structured_members.day | TEXT | 本轮记录对应日期 |
| structured_members.ratio_pct | REAL，可空 | 当前批次比例；完整核验后进入ratios |
| ratio_snapshots.component | TEXT | 主键，已完整发布的比例组件 |
| ratio_snapshots.start | TEXT | 该完整快照覆盖起日 |
| ratio_snapshots.end | TEXT | 该完整快照覆盖止日 |
| ratio_snapshots.checked_at | TEXT | 该完整快照核查时刻 |
| document_retries.disclosure_id | INTEGER | 主键及disclosures外键，随公告删除级联 |
| document_retries.digest | TEXT | 本次重试对应的公告摘要；正文变更后旧重试失效 |
| document_retries.policy | TEXT | 重试/解析策略身份；策略变化可重新调度 |
| document_retries.failure | TEXT | 本次具体失败原因；不写入正式库自由文本列 |
| document_retries.attempted_at | TEXT | 最近尝试时刻 |
| document_retries.retry_after | TEXT | 最早允许再次尝试时刻；控制退避 |
| document_retries.attempts | INTEGER | 已尝试次数；用于有界重试 |
| disclosure_query_policy.singleton | INTEGER | 主键且等于1，查询策略唯一活动行 |
| disclosure_query_policy.identity | TEXT | 查询拆分策略身份；检查续传是否仍匹配 |
| disclosure_queries.market | TEXT | 子查询所属市场；主键及父分区外键组成部分 |
| disclosure_queries.start | TEXT | 所属分区起日 |
| disclosure_queries.end | TEXT | 所属分区止日 |
| disclosure_queries.scope | TEXT | 子查询范围键；与前三列组成主键 |
| disclosure_queries.plate | TEXT | 供应商板块筛选参数 |
| disclosure_queries.securities | TEXT | 供应商证券筛选参数；仅采集协议，不进入正式库 |
| disclosure_queries.next_page | INTEGER | 子查询下次页码 |
| disclosure_queries.expected_rows | INTEGER，可空 | 子查询来源声明总数 |
| disclosure_queries.received_rows | INTEGER | 子查询已验证接收总数 |
| disclosure_queries.complete | INTEGER | 0/1，子查询完整状态 |
| disclosure_queries.failure | TEXT | 子查询失败细节 |
| disclosure_query_rows.market | TEXT | 所属子查询市场 |
| disclosure_query_rows.start | TEXT | 所属子查询起日 |
| disclosure_query_rows.end | TEXT | 所属子查询止日 |
| disclosure_query_rows.scope | TEXT | 所属子查询范围；前四列外键引用disclosure_queries |
| disclosure_query_rows.source_id | TEXT | 已接收来源ID |
| disclosure_query_rows.code | TEXT | 关联股票；前六列组成主键 |
| disclosure_query_rows.record_key | TEXT | 供应商原始记录唯一键；校验重复/分页边界 |
| partition_retries.market | TEXT | 重试分区市场 |
| partition_retries.start | TEXT | 重试分区起日 |
| partition_retries.end | TEXT | 重试分区止日；前三列引用父分区 |
| partition_retries.scope | TEXT | 重试子范围；与前三列组成主键 |
| partition_retries.retry_after | TEXT | 最早可重试时刻 |
| company_dirty_codes.code | TEXT | 主键，原始输入变化后需要重新编译的股票；不是正式事实副本 |
| company_document_requirements.disclosure_id | INTEGER | 主键及disclosures外键；仅索引需要正文核验的公告 |
| company_work_policy.singleton | INTEGER | 主键且等于1 |
| company_work_policy.identity | TEXT | 待提取正文筛选规则身份；变化时重建工作索引 |
| disclosure_query_contracts.market | TEXT | 查询市场 |
| disclosure_query_contracts.start | TEXT | 查询起日 |
| disclosure_query_contracts.end | TEXT | 查询止日 |
| disclosure_query_contracts.scope | TEXT | 查询子范围；与前三列组成主键 |
| disclosure_query_contracts.page_size | INTEGER | 固定30；校验分页合同 |
| disclosure_query_contracts.filter_identity | TEXT | 固定all_announcements；证明查询未漏筛公告类别 |
| company_population.code | TEXT | 主键，当前采集工作人口；用于标记受影响股票，不保存风险或发布状态 |

raw中不再有published、publication或company_facts的第二份正式表。正式发布日期、版本和已发布人口从正式库取得；company_population只是采集任务范围。

已完成且形成覆盖证明的分区，可释放partition_rows和disclosure_query_rows的逐页去重成员；未完成分区必须保留。释放成员不能绕过正文正式发布检查、丢失coverage证明或把未完成分区标为完成。结构化批次暂存成员同样按其完整提交合同清理。采集索引本身也必须清理已完成过程项并回收可复用页，不能只删PDF却放任索引、WAL和重试历史无限增长。

清理必须保留正式事件、达标节点和待核实事项引用的证据定位及摘要，无需长期保留它们的原始附件和全文。跨年持续事件及其未解决的待核实原因由正式事实继续保存，不能随正文删除或超出365日查询窗口而消失。后续新公告基于已发布结构化状态增量归并；提取规则升级或确需重核旧文时按证据定位重新下载。清理只影响原始/过程资料，不回写或重新解释旧冻结推荐记录。

## 6. ST与永久排除的单一来源

1. 历史ST继续由data/market/reference/historical_st.sqlite3及既有参考发布流程负责；启动读取inputs.json中对应的已发布资格事实和身份。
2. company_facts不增加is_st、ever_st或ST历史表；company_exclusion也不保存ST记录。
3. 当前报价或证券名称新出现ST时，运行内存立即限制该股票；已有参考维护流程负责后续持久化核查，不能为此在company建一份ST历史。
4. 退市和人工永久黑名单保留现有有效排除语义，移入company_exclusion；普通处罚、造假等公司风险继续由公司事件和评分规则负责，不能意外恢复旧的永久排除规则。
5. eligibility的CLI列表、状态投影、静态筛选、报价筛选、诊断、测试及bootstrap必须一起切换。外部状态字段如继续保留，必须是新事实的只读投影，不能隐藏读取旧库。

## 7. 编译、原子发布与并发

### 7.1 正常发布

1. 采集器将公告元信息及结构化来源写入raw索引，附件和正文写入raw文件，再提交正文摘要并标记受影响股票；20页/50篇等现有有界批次和取消机制继续生效。
2. 固定raw读取快照和当前正式发布父身份，按股票读取原始输入；使用真实不可变类型编译财务、风险、利好、解禁和待核实事项。
3. 从raw解析来源和证据键，将说明文本转换成明确原因枚举。提取规则或阈值身份变化时重建受影响的完整正式人口，不混合两种提取身份。
4. 在正式库事务外完成耗时解析和准备；写入前检查原始输入版本和正式父版本。正式库只在短事务内更新主表、子表、证据引用和发布身份。
5. 同一次事务保证读者看到旧完整版本或新完整版本。单个股票事实及其证据关系不能半更新；失败、取消或进程中断保留最近有效版本。
6. 正式提交后再清理raw的dirty标记。两库不宣称存在分布式事务：正式提交后、清理前崩溃会导致幂等重复编译，不能导致漏编译。清理须确认输入版本仍未变化，否则保留dirty。
7. 独立非ST排除写入与发布共用正式写入所有者及并发父版本检查；发生冲突重新读取并重建，禁止用旧全库快照覆盖新排除事实。
8. 对本批正文登记与来源版本、提取身份绑定的逐股发布回执；全部关联股票完成后删除相应附件和正文，再获取下一批。发布失败不清理未提交输入，发布成功但清理中断则在恢复时幂等清理；重复启动不重复下载已核实且来源版本未改变的正文。

数据源不完整可以发布明确partial/unavailable的事实，但不得宣称“全量风险已核验”。365日公告完整并不证明较早上市公司的初始持续风险已知，缺少上市初始状态证据时保留原因15。

### 7.2 读取与资源边界

- 只读加载固定一次发布身份，在同一个正式库快照内分批读取12张表所需内容，使用JOIN/批量查询，避免逐事件N+1查询。
- 同一进程三个档位复用一个公司事实内存快照；各档评分仍保持原策略隔离。
- 启动、普通刷新及/api/status不得扫描raw、全文或分页表。证据详情单独有界查询，最多32个证据ID，查询时保留股票归属校验。
- schema_version或extraction_hash不匹配时明确拒绝使用，提示重新同步；不双读旧JSON、不猜测格式、不自动迁移。
- 新库缺失或来源失败时保持已有合法只读快照，并呈现待补原因；不能把未知解释成无风险。

## 8. 体积估算和测量方式

4401行主表的数值字段、短代码和SQLite行开销预计约1—2 MiB以内；事件、证据关系、待核实项和索引取决于实际数量。正式库整体以约3—10 MiB作为初始工程估算，不作为承诺或硬性容量上限。

若大量股票确有多个有效事件或待核实事项，必须如实保存，不能为了体积删除必要事实。正式库若仍达到数百MiB，应逐表检查是否错误纳入全量公告、重复证据、长文本或已失效过程状态。

验收分别报告正式库、raw采集索引、附件、压缩正文、WAL/SHM和目录总量，列出行数、dbstat表及索引空间、空闲页和每股平均正式占用。目录仍可能因原始资料较大而增长，关键是启动只依赖轻量正式库。VACUUM只能回收已空闲页面，不能替代本次结构重构。

## 9. 执行顺序与代码切换范围

以下是同一阶段281内的执行步骤，不另立阶段或进度记录。

1. 完成新schema、明确枚举、日期/时刻/摘要codec及类型校验。替换旧company_facts_codec的JSON数组编码，保留领域事实和评分合同。
2. 拆分company_evidence_index的采集职责与正式发布职责；调整company_pipeline_queries，使其只依赖采集工作状态，不再访问raw中的published。
3. 改造company_facts_reader，以关系表批量恢复类型对象；原始证据查询使用单独注入的raw路径。全部运行消费者切换到新正式读取边界。
4. 在download增加非ST排除写入用例和正式表适配器；切换bootstrap、静态筛选、行情观察、CLI、健康状态及诊断，删除独立SQLiteIssuerEligibilityIndex与DataPaths.eligibility活动路径。
5. 更新DataPaths、storage_runtime、同步入口及既有诊断工具中的正式/原始路径。所有写入入口由bootstrap装配；不添加隐藏客户端或第二组合根。
6. 在仓库外临时目录创建全新测试库，验证类型、关系、提取、发布、故障恢复及三个档位共用加载。不能在活动旧库上边试验边改表。
7. 完成后执行下节已授权的真实清空重建，实测体积、公司加载和历史启动。
8. 行为切换时更新工程设计和阶段281证据；完整Review、适用门禁通过后，只提交本批文件，按仓库规则提交并推送Afuture。保留其它协作者的配置、模型和文档修改。

需要同步检查的消费者包括：公司同步应用及进度统计、新闻采集人口、正式发布版本、风险与利好编译、PublishedCompanyResearch、详情证据API、永久排除端口及CLI、运行健康状态、统一诊断及其子模块、数据布局/离线迁移工具和相关回归。旧路径的历史说明可保留，但活动代码不得保留旧库fallback。

## 10. 清空旧company并从零重建

用户已明确要求删除之前company下载的数据后全部重跑。这里是后续执行步骤，写入本方案本身不表示已经删除或重建。

1. 先完成隔离验证并列出受管理文件的明确清单，检查真实解析路径及符号链接，避免清理扩散到其它数据目录。
2. 正常停止当前同步和持有company文件的推荐进程，等待线程、事务和文件锁释放；核实不存在仍在写入的句柄。保留市场、训练和冻结推荐数据。
3. 删除data/company内旧company_facts.sqlite3、evidence.sqlite3、eligibility.sqlite3及对应WAL、SHM、journal、已确认的公司专属旧锁和检查点；原有公司raw采集状态如存在也从零重建。未知用户文件先列明，不以无边界目录删除代替清单。
4. 使用新实现初始化company_facts.sqlite3和raw/company_collection.sqlite3；不导入旧JSON事实、公告、正文、游标、重试或eligibility记录。
5. 执行`./run.sh sync company`。既有market/reference提供证券、ST等参考人口；若参考缺失或过期，按现有参考维护合同报告或维护，不复制ST进company。
6. 采集任务不设整轮人为超时；请求、PDF解析、重试和取消仍必须有界。发生中断时只从本次新库形成的新断点恢复。
7. 输出实际处理股票数、各组件完整/部分/不可用数量、待补正文及原因、发布身份、文件大小。不能用命令退出0或存在4401行代替覆盖验证。
8. 完成同步后正常启动`./run.sh v1,v2,v3`，验证使用的是新进程和新正式发布身份，三个档位共享公司加载，最近有效结果及降级状态可读。

## 11. 历史启动deadline的独立修复

通过scripts/diagnose_runtime.py及其既有子模块测量完整历史预加载，将读取、分区证明、行解码、共享派生计算及缓存提交耗时分开。

待验证的优化包括：使用匹配代码/日期的索引避免临时排序；同一有效发布序列的年度分区流式读取；月度证明不重复读取相同年度内容；发布目录缓存必须绑定数据库及WAL变化身份，更新后立即失效。不能跳过内容证明、混用不同发布序列或泄漏未发布修订。

以实际新进程完整加载4401股及V1/V2/V3所需窗口为验收对象，确认在既有300秒预算内完成，且没有history_preload_deadline反复刷新。若失败，继续定位未解决耗时；HTTP 200、Long可显示或少量股票fixture不能算启动通过。

## 12. 验证与交付条件

### 12.1 结构与语义

- 正式库只有约定的12张业务表；无原始公告、正文、title、url、summary、JSON事件数组和续传表。
- 逐表校验列类型、NULL、枚举范围、外键、唯一键、证据顺序、字段单位、时间精度及摘要长度；未知编号拒绝。
- 风险解除、撤销、减持尾期、持续风险、利好首次达标及交易日窗口保持原规则；按类型恢复的摘要不影响数值评分。
- 未知财务、未完成正文、初始风险状态不明、解禁前视覆盖不足均保留明确待补，不因清空重建自动变成合格。
- ST只来自既有参考；非ST排除完整传递，旧eligibility活动引用清零；旧冻结记录不重写。

### 12.2 发布与故障

- 提取中取消、事务中异常、正式提交后raw清理前崩溃均不会出现半发布或丢失待处理任务。
- 真实多批处理记录临时占用峰值，验证下载→提取→发布→清理循环；多股公告不会提前删除，失败文件不无限累积，低磁盘时停止新下载，已提交风险及待核实项不会因删除正文丢失。
- 并发原始输入改变、并发永久排除写入及提取身份改变会触发冲突处理；不覆盖新事实。
- 正式库缺失、损坏或身份不匹配时显式降级；raw暂不可读时正式启动和评分仍可使用有效快照，证据详情独立报错。
- PRAGMA integrity_check与foreign_key_check通过；引用证据无悬空，未引用证据可回收，原始持续风险资料不会误删。

### 12.3 性能与真实运行

- 记录4401股正式加载耗时、峰值内存、SQL查询数及真实文件大小；证明未访问raw并只共享加载一次。
- 新同步的覆盖率、待补原因和失败分类真实可查；正文提取失败不能被统计成已完成。
- 真实`./run.sh v1,v2,v3`在既有历史预算内完成加载；检查状态和合法结果/降级行为，不把空荐股一概解释为故障或成功。
- 定向回归覆盖新schema、发布恢复、排除来源、评分语义和历史序列隔离；受影响Python运行Ruff，公共类型及复杂边界运行mypy。其余门禁按AGENTS.md风险选择，不将定向测试称作全量回归或发布验收。

交付报告必须区分已实现、隔离测试、真实同步、真实启动和未完成项。正式库体积估算、旧批次测试结果、模拟样本及HTTP成功均不能替代本次实测。只有本阶段完整实现、验证和单提交推送完成后才将阶段281标记completed。
