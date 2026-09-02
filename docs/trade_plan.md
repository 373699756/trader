
  # Tomorrow 申万一级行业独立 Ridge + LightGBM 训练与评分接入计划

  ## 1. 方案概要

  - 使用最近 3000 个交易日的 OHLC、成交量、成交额、历史申万一级行业和点时资格数据。
  - 按申万一级行业分别训练独立参数；每个行业使用 Ridge 50% + LightGBM 50% 固定集成，不训练逐股模型。
  - 被当日点时硬过滤拒绝或过滤证据不完整的股票不生成训练样本。
  - 仅用于 Tomorrow；模型输出直接替换 base_score，随后继续执行本地风险、固定 68/32 融合、动作门、Top6 和 14:50 冻结。
  - 因为没有历史 14:50 数据，研究身份固定为 daily_close_proxy。允许人工越权生产接入，但默认档位继续为 V1，状态必须持续公开
    point_in_time_parity=false。

  - 模型不自动训练、更新、激活或回退，保持 automatic_model_update=false。

  ## 2. 数据、训练与验证

  ### 数据人口

  - 导入最近 3000 个完整交易日，必需字段包括股票代码、原始/前复权 OHLC、成交量、成交额、申万一级行业代码及生效区间、上市/退市、ST、停牌、交易限制和
    永久资格事实。

  - 股票只有在决策日当时通过一级永久资格、二级硬过滤且六项模型特征完整时才进入训练。
  - 行业归属必须满足 effective_at <= trade_date，禁止使用当前行业回填历史；行业变更后股票自动进入新行业模型人口。
  - 数据、股票池、日期、行业映射、过滤规则和来源分别生成 SHA-256 manifest；相同内容幂等，不同内容冲突。
  - 一个行业必须具备至少 1000 个有效交易日、开发段至少 600 日、确认段和终端留出各至少 200 日、开发训练行至少 20,000 行且点时字段覆盖率不低于 95%。下
    载到 3000 日上限后仍不足则该行业为 historical_data_insufficient，线上不获得模型分，不使用全局或相邻行业回退。

  ### 标签与特征

  主标签为收盘代理 T+1 毛超额收益：

  gross_excess_return =
      close[D+1] / close[D] - 1
      - 当日合格全市场等权收益

  模型固定使用六项 Alpha：

  1/3/5日收益
  20/40/60日 skip-5 残差动量

  残差动量依次去除市场、板块、申万一级行业和板内对数成交额暴露。波动、下行方差、ATR、回撤和 Amihud 只用于风险、成本、容量与诊断，不作为预期收益加
  分。股票代码不得作为特征；每个行业每日样本总权重固定为 1，避免股票数量多的日期支配损失函数。

  ### 行业模型

  每个合格行业分别封存：

  - 特征均值、尺度和顺序；
  - Ridge 截距和六个系数；
  - LightGBM 全部树、叶子值和最佳迭代；
  - 行业仿射校准参数；
  - 训练日期、行数、父 manifest 和模型 hash。

  LightGBM 参数固定为：

  regression_l2
  learning_rate=0.05
  max_depth=3
  num_leaves=7
  min_data_in_leaf=20
  num_boost_round=200
  early_stopping_rounds=20
  max_bin=63
  deterministic=true
  num_threads=1

  不执行超参数搜索。训练同时产生两套完整系统候选：

  1. 所有行业使用 expanding 历史窗口；
  2. 所有行业使用 rolling_1500 窗口。

  禁止按行业自由选择不同窗口。开发段通过五折 expanding walk-forward 比较两套完整系统；只有通过预注册收益、风险和换手门禁的系统才能进入确认段。两套都
  通过时，选择20bp/50bp绝对净超额及相对 V1 配对增量四项 bootstrap 下界中的最小值更高者；完全相同时选择 rolling_1500。

  ### 时序验证

  - 有效日期按最旧60%、随后20%、最新20%切分，两个边界各保留5日 embargo。
  - 开发段执行五折时序 walk-forward；确认段只评价一次；确认通过后，以开发段和确认段重新拟合唯一冻结候选，再一次性开启终端留出。
  - 最终拟合中，末20个训练日只用于 LightGBM 早停，之后20日只用于行业仿射校准；二者均早于终端留出。
  - 所有行业校准后的输出统一为预期毛超额收益，再扣成本并进行全市场统一排名。
  - 通过门禁包括20bp/50bp绝对净超额和相对当前 V1 的配对增量及区块下界均大于0、严重亏损不劣化、换手增量不超过5个百分点、Rank IC与Q5-Q1为正、容量与集
    中度合格。任一主要门禁失败即 historical_rejected。

  ## 3. 参数如何进入生产评分

  新增非默认配置档位：

  tomorrow_scoring_profile = "sw1_industry_v1"

  模型推理链明确为：

  zᵢ = (xᵢ - μindustry) / σindustry

  ridgeᵢ =
      intercept_industry
      + βindustry · zᵢ

  lightgbmᵢ =
      Σ tree_industry,m(zᵢ)

  predicted_gross_excessᵢ =
      calibrate_industry(
          0.5 × ridgeᵢ + 0.5 × lightgbmᵢ
      )

  estimated_costᵢ =
      0.002 × (1 + 当批Amihud20全市场分位)

  utilityᵢ =
      predicted_gross_excessᵢ - estimated_costᵢ

  base_scoreᵢ =
      utilityᵢ <= 0 ? 0 : 全市场utility稳定分位 × 100

  因此离线训练得到的均值、尺度、Ridge 参数、LightGBM 树和校准参数都会直接决定 base_score，不是仅作展示。

  随后沿用现有链：

  local_score =
      clamp(base_score - local_risk_penalty, 0, 100)

  final_score =
      ROUND_HALF_UP(
          clamp(
              local_score × 0.68
              + deepseek_score × 0.32
              - deepseek_risk_penalty,
              0,
              100
          ),
          2
      )

  生产行为固定如下：

  - 线上使用14:50当前价格和累计成交额构造特征，但模型训练锚点是15:00收盘，状态和决策身份必须记录 training_anchor=daily_close、runtime_anchor=14:50、
    point_in_time_parity=false。

  - 模型只覆盖 Tomorrow base_score，不与旧基础分二次加权；风险仍只扣一次。
  - 各行业预测校准后执行全市场统一排名，继续限制每行业最多2只、单板不超过60%、最终Top6。
  - 未知行业、行业模型不合格或单股字段缺失时，该股票记录 industry_model_unavailable 或 production_model_features_missing 并退出评分。
  - 选择该档位时，任何工件缺失、bundle hash错误或行业模型篡改都拒绝启动；运行期整批推理异常不发布新决策，保留最近同日有效决策并标记降级。
  - 模型 bundle 作为 wheel 资源一次加载；所有候选组成一个批次，再按行业分组调用 Booster，不逐股加载模型。
  - 默认配置保持 V1；人工通过 --profile sw1_industry_v1 启动。回退通过正常重启恢复 V1，不修改既有正式或冻结记录。

  ## 4. 接口、工件与执行批次

  ### 新增类型和接口

  - 新研究工件 score_tomorrow_sw1_industry_ensemble_v1，包含行业模型映射、统一特征契约、窗口模式、校准身份、父 manifest 和 bundle hash。
  - 新研究命令：
      - research-h1-import --input <dataset-root>：导入3000日点时数据；
      - research-tomorrow-industry-train：只读取开发段；
      - research-tomorrow-industry-confirm --run-id：一次性确认；
      - research-tomorrow-industry-holdout --run-id：一次性终端留出。

  - 状态白名单新增活动 profile、bundle hash、合格/不足行业计数、人工授权依据、训练/运行锚点、点时不一致和 automatic_model_update=false；不公开行业模
    型树或股票级训练载荷。

  - 研究报告继续 production_authority=false；生产打包和人工启用必须是后续独立高风险批次。

  ### 交付顺序

  1. 先修复当前空 Git 对象，恢复可读取的 HEAD 和上游；否则禁止开始提交批次。
  2. 按权威路线分别完成第15.1.23节基线身份审计和第15.1.24节热链等价门禁。
  3. 单独更新策略契约和机器测试：当前第15.1.29节禁止训练新模型、H1上限为1600日，必须先显式授权3000日申万一级行业模型。
  4. 实现H1导入、点时行业/资格审计和不可变存储。
  5. 实现行业训练、确认、终端留出及防篡改工件。
  6. 历史终端留出通过后，另立生产接入批次，增加新 profile、wheel 模型 bundle、状态投影和评分覆盖接缝。
  7. 每个章节独立 Review、更新 Changelog、创建一个提交、推送并核对 HEAD == @{upstream}；不得合并相邻章节。

  ## 5. 测试、验收与默认假设

  ### 关键测试

  - 硬过滤拒绝和证据不完整股票从未进入训练；
  - 行业生效时间晚于决策日时失败关闭；
  - 不同行业确实生成不同 Ridge/LightGBM 参数；
  - 3000日上限、时序切分、embargo、标签成熟和终端留出不可提前读取；
  - expanding与rolling_1500只能按冻结的系统级规则选择；
  - 行业校准后跨行业预测可比较，最终执行全市场统一排名；
  - 不足行业、未知行业和缺字段股票不发生隐藏回退；
  - 固定输入、种子和顺序产生相同模型及报告hash；
  - 工件、父manifest或bundle篡改拒绝；
  - local_score_overrides确实使用行业模型结果替代旧base_score；
  - 本地风险只扣一次，固定融合向量仍为83.40；
  - V1默认启动、新档位显式启动、正常重启回退和冻结记录隔离；
  - 上午热运行、午间冷启动、14:50边界、15:00后热运行/冷恢复和正式记录命中均保持原冻结语义。

  ### 门禁

  研究批次运行对应 research unit/component/contract、Ruff和公共类型mypy。生产接入属于高风险评分、配置、入口、包资源和状态变更，必须执行：

  make format-check
  make lint
  make type-check
  make test
  make package

  同时完成仓库外wheel安装、模型资源/hash验证、生产性能门禁、真实服务重启后的 research、runtime 和 full 诊断，以及适用的三档桌面验证。

  ### 已锁定假设

  - 行业口径为带历史生效日期的申万一级分类，来源由用户已有历史数据源提供。
  - 下载最近3000个交易日；不无限追溯全部历史。
  - 每行业独立模型，但所有行业使用相同特征、参数规范和统一窗口模式。
  - 模型家族固定为Ridge/LightGBM各50%，不做逐股模型或自动择模。
  - 仅影响Tomorrow；Today、D25、Long和DeepSeek行为不变。
  - 没有历史14:50数据，生产接入属于用户明确的人工越权，不能宣称点时一致或历史验证通过。
  - 不足行业失败关闭，不使用全市场、相邻行业或旧评分隐藏回退。
  - 默认V1不变；行业模型只通过显式档位人工启用。


