• # 完整优化计划

  结论：采用“公共评分内核 + V1/V2/V3 档位目录 + Today/Tomorrow/D25 三个策略头 + V3 可组合模型”的架构。

  这里严格区分：

  - V1/V2/V3：评分生产档位。
  - Today/Tomorrow/D25：三个并列荐股策略。
  - D25：一个完整的“未来2–5日可能上涨”策略，不拆成多个生产预测头。
  - T+2 至 T+5：只作为 D25 训练标签、验证观察点和风险指标。

  当前工作树存在一整批未提交的全仓稳定命名迁移。实际实施前必须先闭合该批次，再以新 HEAD 开始评分模块化，不能混在同一个提交里。

  ## 一、方案选择

  不采用仅把 tomorrow_production_model.py 拆成三个文件的局部方案。那样虽然目录好看，但以下耦合仍然存在：

  - 公共评分服务仍判断 V1/V2/V3。
  - build_scored_local() 仍只认识 Tomorrow 模型。
  - 运行时仍专门为 Tomorrow 注入模型。
  - Today/D25 训练结果以后进入 V3 时仍需再次修改公共链。
  - V3 训练和在线特征不一致的问题没有解决。

  选择系统性方案：

  评分档位（V1/V2/V3）
          ↓
  策略头（Today/Tomorrow/D25）
          ↓
  标准模型信号
          ↓
  档位内部组合
          ↓
  公共成本和base_score映射
          ↓
  公共风险、DeepSeek、动作、排名、冻结

  ## 二、目标模块结构

  ### 1. 领域层：公共纯计算

  src/trader/domain/recommendation/model_scoring/
  ├── profile_identity.py
  ├── head_contract.py
  ├── prediction.py
  ├── residualization.py
  ├── utility_scoring.py
  └── feature_contracts/
      └── tomorrow.py

  后续真正开发 Today、D25 训练头时再增加：

  feature_contracts/
  ├── today.py
  └── d25.py

  不提前创建空模块或 TODO。

  职责：

  - profile_identity.py
      - 定义 ScoringProfileId = Literal["v1", "v2", "v3"]
      - 定义唯一允许值集合和解析函数
      - 配置、CLI、Server 共用，不再各写一遍白名单

  - head_contract.py
      - 定义三个且只有三个策略头：Today、Tomorrow、D25
      - 直接复用有类型 Strategy
      - 定义输入锚点、特征合同、最少历史长度和覆盖规则

  - prediction.py
      - 定义不可变的 HeadPrediction
      - 输出股票代码、策略头、预测收益、模型分歧、模型身份和输入身份
      - 不包含 JSON 投影方法

  - residualization.py
      - 实现市场、板块、行业、成交额暴露处理
      - 使用类型化 ExposureContract 控制各档位差异
      - 训练与生产共同使用

  - utility_scoring.py
      - Amihud 成本
      - 净预期收益
      - 正净效用横截面 0–100 映射
      - 模型置信度计算
      - 确定性排序

  领域层不得导入配置、文件、JSON、NumPy、LightGBM、研究模块或时钟。

  ### 2. 应用端口：模型能力边界

  把当前 Tomorrow 专属端口泛化为：

  src/trader/application/ports/model_scoring.py

  定义以下类型：

  ScoringHeadInput
  HeadPredictorPort
  HeadPrediction
  ProfileCombinerPort
  LoadedScoringProfile
  ScoringProfileRuntimeStatus

  其中 LoadedScoringProfile 由多个独立类型组成，避免形成大而全的 God Object：

  LoadedScoringProfile
  ├── ProfileIdentity
  ├── HeadRuntime[]
  ├── ProfileCombinerPort
  └── ProfileEvidence

  分工：

  - HeadPredictorPort 只预测，不负责加载文件。
  - ProfileCombinerPort 只组合模型信号，不扣成本、不扣风险。
  - ProfileEvidence 只保存历史状态、启用依据和失败原因。
  - LoadedScoringProfile 只聚合已经构造完成的能力。

  V2 专属的 TomorrowHistoricalP2ModelArtifact 移出公共应用端口。

  ### 3. 应用层：通用模型评分编排

  src/trader/application/recommendation/model_scoring/
  ├── score_batch.py
  ├── scoring_service.py
  ├── score_override.py
  └── scoring_router.py

  职责：

  - score_batch.py
      - ModelScoreBatch
      - ModelDiagnostics
      - 缺失、拒绝和覆盖结果

  - scoring_service.py
      - 构造规范输入
      - 调用策略头
      - 调用档位组合器
      - 执行公共成本和横截面分数映射
      - 返回模型 base_score 覆盖

  - score_override.py
      - 明确管理规则评分与模型评分的关系
      - 没有模型头时使用正式规则评分，是声明式能力路由，不是异常 fallback
      - 模型已声明支持但工件损坏时必须失败关闭，不能回落旧模型

  - scoring_router.py
      - 按 Strategy.TODAY/TOMORROW/D25 选择评分能力
      - 不知道 V1/V2/V3 的内部算法
      - 不导入基础设施

  公共服务禁止出现：

  if profile_id == "v1"
  if profile_id == "v2"
  if profile_id == "v3"

  ## 三、V1/V2/V3 档位目录

  src/trader/infra/scoring/
  ├── profile_factory.py
  ├── artifact_hashing.py
  └── profiles/
      ├── v1/
      │   ├── profile.py
      │   ├── artifact_codec.py
      │   └── heads/
      │       └── tomorrow/
      │           └── predictor.py
      ├── v2/
      │   ├── profile.py
      │   ├── artifact_codec.py
      │   └── heads/
      │       └── tomorrow/
      │           └── predictor.py
      └── v3/
          ├── profile.py
          ├── bundle_codec.py
          ├── bundle_locator.py
          ├── composition.py
          └── heads/
              └── tomorrow/
                  └── predictor.py

  未来有真实模型工件时扩展为：

  profiles/v3/heads/
  ├── today/
  │   └── predictor.py
  ├── tomorrow/
  │   └── predictor.py
  └── d25/
      └── predictor.py

  ### 目录内聚规则

  每个档位目录只保存差异：

  - 模型结构
  - 工件类型和解析
  - 特征合同选择
  - 覆盖规则
  - 历史证据
  - 激活依据
  - 档位内部信号组合

  以下内容不得进入档位目录：

  - 硬过滤
  - DeepSeek 调用
  - 本地风险扣减
  - 68/32 融合
  - 动作门槛
  - Top6和集中度
  - 冻结
  - 持久化
  - API/Web投影

  ### 文件职责

  - profile.py
      - 组装该档位
      - 不解析 JSON
      - 不执行预测
      - 不访问目录

  - artifact_codec.py / bundle_codec.py
      - JSON边界到不可变类型对象
      - schema、字段、hash校验
      - 不执行推理

  - bundle_locator.py
      - 只定位 V3 工件路径
      - 不解析内容、不选择模型

  - predictor.py
      - 只接受类型化工件和类型化输入
      - 执行批量推理
      - 不读取文件、不生成状态

  - composition.py
      - 只组合预风险模型信号
      - 不扣成本、不做0–100映射、不扣风险

  ## 四、三个策略头的定义

   策略头      业务目标             决策锚点    生产输出
  ━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━
   Today       今天上涨潜力         11:20       一个Today模型信号
  ──────────  ───────────────────  ──────────  ──────────────────────
   Tomorrow    明天上涨潜力         14:50       一个Tomorrow模型信号
  ──────────  ───────────────────  ──────────  ──────────────────────
   D25         未来2–5日上涨潜力    14:50       一个D25模型信号

  D25 内部可以使用：

  - T+2、T+3、T+4、T+5实际收益
  - 2–5日算术平均收益标签
  - 四个观察点方向稳定性
  - 最大回撤
  - 严重亏损
  - 容量与换手

  但这些只是 D25 训练和验证维度。生产边界只能输出一个 D25HeadPrediction。

  新增架构契约应直接禁止出现 T2Head、T3Head、T4Head、T5Head 等生产头。

  ## 五、V3 的正确演进方式

  ### 当前 V3

  当前 V3 仍然只有 Tomorrow 头：

  TomorrowHead
      ↓
  SingleHeadCombiner
      ↓
  Tomorrow目标预测

  这保持当前业务行为，不虚假声明 Today/D25 已接入。

  ### 未来 V3

  Today和D25训练模型通过验证后：

  TodayHead ─────┐
  TomorrowHead ──┼→ V3ProfileCombiner → V3目标预测
  D25Head ───────┘

  这里的“参与 V3”必须遵守：

  1. 不能直接组合三个策略的最终0–100分数。
  2. 不能把已经扣过风险的 local_score 再作为模型输入。
  3. 不能重复扣交易成本。
  4. 不能重复执行横截面映射。
  5. 每个头先输出原始预测收益、分歧和证据。
  6. 跨周期信号必须先校准到本次决策目标的统一单位。
  7. V3组合器输出一个目标预测。
  8. 公共层再执行一次成本、一次分数映射、一次本地风险和一次DeepSeek融合。

  因此正确链路是：

  Today/Tomorrow/D25预风险模型信号
  → V3目标校准与组合
  → 目标预测收益
  → 成本扣减一次
  → base_score映射一次
  → 本地风险扣一次
  → 固定68/32融合
  → 动作、Top6、冻结

  ### V3组合器接口

  当前批次只实现真实使用的 SingleHeadCombiner，但公共端口允许以后替换为经过验证的多头组合器。

  不提前实现假设性的平均权重。未来组合方式可能是：

  - 固定权重
  - 线性校准
  - 预注册元模型
  - 条件门控

  必须由独立历史研究、终端留出和用户授权决定，不能在模块化重构中偷偷选定。

  ## 六、训练与生产隔离

  训练实现继续属于：

  src/trader/domain/research/
  src/trader/application/research/
  src/trader/infra/research/

  生产 V1/V2/V3 目录不得导入研究代码。

  两侧只能共享：

  - 纯特征计算
  - 类型化特征合同
  - 模型工件线格式规范
  - 内容hash算法

  正确边界：

  研究训练
  → 生成并原子封存JSON模型工件
  → 生产codec重新解析为独立类型对象
  → 生产predictor推理

  禁止：

  - 生产直接实例化研究训练类
  - 生产读取训练 evidence
  - 研究对象直接注入生产
  - pickle
  - 绝对路径
  - 自动 promotion
  - 自动重训或回退

  ## 七、V3特征一致性修复

  这是确认存在的问题，必须纳入本次目标架构。

  ### V1/V2合同

  残差动量处理：

  市场
  → 板块
  → 板内对数20日平均成交额

  ### V3 Tomorrow合同

  根据权威策略：

  市场
  → 稳定板块
  → 历史有效行业
  → 板内对数20日平均成交额

  实施要求：

  - 训练和在线调用同一个纯残差化函数。
  - V3训练样本补充20日平均成交额。
  - 行业和成交额缺失时按V3合同失败关闭。
  - 特征顺序、单位、暴露合同和hash进入模型工件。
  - 旧V3工件缺少新合同字段时失败关闭。
  - 不修改V1/V2工件内容或hash。
  - 不自动启动V3训练或切换生产 profile。

  ## 八、运行时与组合根调整

  把当前 Tomorrow 专用注入：

  TomorrowProductionModelScoringService

  替换为：

  ModelScoringRouter

  组合根负责：

  1. 读取集中解析后的档位。
  2. 调用唯一 profile_factory。
  3. 构造一个不可变 LoadedScoringProfile。
  4. 构造通用评分服务。
  5. 注入 ModelScoringRouter。
  6. 只启动现有评分 worker，不新增线程。

  当前能力矩阵：

   档位       Today           Tomorrow         D25
  ━━━━━━  ━━━━━━━━━━  ━━━━━━━━━━━━━━━━━  ━━━━━━━━━━
   V1      规则评分             V1模型    规则评分
  ──────  ──────────  ─────────────────  ──────────
   V2      规则评分             V2模型    规则评分
  ──────  ──────────  ─────────────────  ──────────
   V3      规则评分    V3 Tomorrow模型    规则评分

  未来 Today/D25 训练头通过并被明确授权后，只修改 V3 profile 的能力组成；公共应用链不再修改。

  ## 九、配置与公开契约

  当前批次保持：

  tomorrow_scoring_profile = v1 | v2 | v3
  默认 v1

  原因是当前生产模型仍只替换 Tomorrow 评分。不能为了未来可能性提前改变公开配置语义。

  只有未来 V3 真正同时改变 Today、Tomorrow、D25 的生产评分时，才另立高风险批次决定是否改为中性的 scoring_profile。届时必须：

  - 不保留双字段
  - 不做隐藏兼容
  - 同步配置契约、入口、状态、测试、两份权威文档和 Changelog
  - 获得用户明确授权

  当前 API/SSE JSON schema 不变。内部类型可以扩展，但外部 adapter 仍按现有字段白名单投影。

  ## 十、实施顺序

  ### 计划项1：闭合当前工作树批次

  - Review当前稳定命名迁移
  - 验证、提交、推送
  - 核对 HEAD == @{upstream}
  - 重新记录评分重构基线和文件范围

  完成前不得开始评分模块修改。

  ### 计划项2：契约与失败测试先行

  新增或更新：

  - 模块依赖AST契约
  - V1/V2/V3 profile目录隔离
  - 三个且只有三个策略头
  - 公共服务无profile分支
  - 配置/CLI允许值唯一来源
  - 生产不导入research
  - JSON只在codec边界
  - 模型工件缺失/损坏失败关闭
  - V3训练—在线特征一致性
  - V1/V2固定样本黄金结果
  - API状态和模型身份不变
  - 冻结记录绑定真实模型hash
  - 固定融合结果 83.40

  ### 计划项3：提取领域公共计算

  - 残差化
  - 成本
  - 净效用
  - 分数映射
  - 置信度
  - 排序
  - 特征合同

  只移动真实共性，不为减少文件行数机械拆分。

  ### 计划项4：建立通用应用端口与路由

  - 新建通用模型评分端口
  - 建立 ModelScoringRouter
  - 将 Tomorrow 专用参数替换为通用路由
  - Today/D25 当前继续走明确规则评分
  - 删除公共服务中的版本判断

  ### 计划项5：迁移V1

  - 拆出V1工件codec
  - 拆出线性predictor
  - 迁移状态元数据
  - 保持模型内容、ID、hash和评分结果不变

  ### 计划项6：迁移V2

  - 将P2工件类型从应用端口移出
  - 拆出V2 codec与predictor
  - 保持历史身份、资源内容、hash和评分结果不变

  ### 计划项7：迁移并修正V3

  - 拆出locator、codec、predictor和单头组合器
  - 统一训练与在线残差化
  - 增加完整特征合同校验
  - 验证行业覆盖和失败关闭
  - 当前仍只装配Tomorrow头

  ### 计划项8：迁移消费者并删除旧链

  同步调整：

  - bootstrap.py
  - 配置loader和settings类型
  - CLI、Server、performance入口
  - scored projection
  - input runtime
  - 状态聚合
  - Web/API adapter
  - 基线身份审计
  - 研究侧V1/V2工件消费者
  - 包资源测试

  删除旧模块，不保留转发 facade、双实现或隐藏 fallback。

  ### 计划项9：文档和Changelog

  更新：

  - docs/02_工程设计.md
      - 模块结构
      - 依赖方向
      - 组合根
      - 三策略能力矩阵
      - 生产/研究隔离

  - docs/01_评分逻辑.md
      - V1/V2/V3差异
      - Today/Tomorrow/D25三头定义
      - D25是单一2–5日策略
      - V3当前单头及未来组合边界
      - V3训练—在线特征合同

  - CHANGELOG.md
      - 用户诉求
      - 确认根因
      - 模块调整
      - 行为变化
      - 验证证据
      - 剩余风险

  ## 十一、后续独立批次

  模块化完成后，后续工作必须独立交付。

  ### Today训练头批次

  - 定义Today点时输入和标签
  - 训练、确认、终端留出
  - 封存无生产权限工件
  - 不自动接入V3
  - 不改变11:20冻结

  ### D25训练头批次

  - D25保持单一模型头
  - 训练标签覆盖未来2–5日综合表现
  - T+2至T+5仅作为内部验证维度
  - 封存无生产权限工件
  - 不改变14:50冻结和收盘fallback

  ### V3三头组合研究批次

  - 冻结Today、Tomorrow、D25三个父工件及hash
  - 预注册有限组合方式
  - 做目标单位校准
  - 训练、确认和终端留出严格隔离
  - 验证成本后收益、严重亏损、换手、容量、Rank IC、Q5-Q1和分层稳定性
  - 失败时保持当前单Tomorrow头V3
  - 不继续自动搜索组合

  ### V3三头生产启用批次

  只有用户明确批准唯一模型包后：

  - 更新V3 bundle
  - 绑定确切组合模型和hash
  - 更新决策输入身份
  - 核对三个策略的冻结边界
  - 更新状态/API/Web公开内容
  - 保持V1/V2不变
  - 禁止自动激活、重训和回退

  ## 十二、风险匹配验证

  该重构触及评分、配置、组合根、入口、研究工件、状态和包资源，属于高风险批次。

  完整门禁：

  make format-check
  make lint
  make type-check
  make test
  make package

  专项验证：

  - V1/V2黄金评分结果
  - V3特征合同和训练—在线一致性
  - D25单头架构契约
  - 固定融合 83.40
  - 风险只扣一次
  - 三策略候选、动作线、Top6不变
  - 11:20与14:50冻结恢复
  - 模型ID/hash传播与first-wins
  - V3损坏/缺失工件失败关闭
  - 配置、CLI、Server三档选择
  - create_app()无副作用
  - API/SSE字段白名单
  - 热路径性能门禁
  - 仓库外wheel安装和模型资源读取
  - git diff --check
  - 暂存区只包含本任务文件

  浏览器布局没有行为变化时，不重复做三分辨率发布验收；但状态/API消费者契约和JS测试必须运行。若实现过程中公开状态或Web行为发生变化，则升级到完整桌面浏览器验收。

  ## 十三、完成条件

  只有全部满足才算完成：

  - 公共评分代码没有V1/V2/V3条件分支。
  - V1/V2/V3差异只存在于对应profile目录。
  - 生产系统只有Today、Tomorrow、D25三个策略头。
  - D25没有被拆成T+2至T+5生产头。
  - 当前V3只启用Tomorrow头。
  - 未来新增Today/D25头不需要修改公共风险、融合、排名和冻结链。
  - V1/V2模型内容、ID、hash和冻结行为不变。
  - V3训练与在线特征完全一致。
  - 生产不导入研究模块。
  - 没有兼容facade、双实现、隐藏fallback或空占位模块。
  - 全部适用门禁通过。
  - 完整diff两轮Review零已知发现。
  - 更新两份权威文档和Changelog。
  - 只创建一个Conventional Commit。
  - 推送成功并确认本地HEAD与上游一致。

