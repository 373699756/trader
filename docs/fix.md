整体判断：核心架构已经比较专业，最大的问题不是 src/trader，而是少数模块过大、根目录有工具/生成物痕迹，以及部分职责还可以进一步收敛。不建议为了“看起来少一层”而重命名整个 Python 包。

  我建议按下面优先级整理。

  优先级最高：先治理结构性问题

  1. 保留 src/trader
     这是标准 src 布局和正式包命名空间。当前 pyproject.toml:40、所有入口和测试都依赖 trader.*，不值得迁移。

  2. 拆分超大模块
     当前 src/trader/infra/research/baostock_daily.py 已超过 1200 行，应该按职责拆成：

     infra/research/
     ├── baostock_gateway.py
     ├── baostock_serialization.py
     ├── baostock_partition_archive.py
     ├── baostock_catalog.py
     └── baostock_daily.py        # 只保留门面和公共协议

     保持对外类型和调用方式不变，避免继续把网关、SQLite、manifest、校验和合并逻辑堆在一个文件中。

  3. 清理根目录中的仓库/工具痕迹
     当前存在 docs/.git、docs/.agents、docs/.codex，需要确认是否是误复制的嵌套仓库。若不是明确用途，应移除；否则应在文档中说明所有权和用途。


  优先级中等：让数据和构建产物更整洁

  建议固定为：

  data/
  ├── history/       # 运行时下载，永不提交
  └── train/         # 可提交的批准训练工件

  .egg-info/         # 隐藏构建元数据，忽略
  dist/              # wheel/sdist，忽略
  build/             # 临时构建目录，忽略

  当前 data/history/ 已被忽略，data/train/ 没有忽略规则，方向是正确的。建议只提交 data/train/ 中明确批准的模型工件，不要把特征、缓存、Parquet 中间结果和训练日志全部提交进去。

  优先级中等：测试和文档收敛

  现有测试分类已经不错：

  unit/
  component/
  integration/
  contract/
  performance/
  js/

  后续可以补：

  - pytest markers，例如 unit、component、integration、slow
  - make test-unit、make test-contract、make test-release
  - wheel 外安装、CLI、资源读取作为独立发布测试
  - 把历史流水线报告统一放入 docs/archive/
  - 保留 docs/software-business-design.md 和 recommendation-strategy.md 作为唯一权威文档，不再把产品规则拆到多个说明文件里

  不建议做的事

  - 不要删除 src/trader
  - 不要把 domain、application、infra 合并成一个 core
  - 不要新建模糊的 common/、utils/ 大杂烩目录
  - 不要为了目录短而改变 trader.* 导入命名空间
  - 不要保留旧包名兼容层和新包名双实现

  最合适的整理顺序是：

  1. 审计并清理嵌套仓库和重复脚本。
  2. 拆分 BaoStock 超大模块。
  3. 固化 data/history、data/train 和构建产物规则。
  4. 增加分层测试命令和 wheel 发布测试。
  5. 最后整理历史文档。

  这样能明显提升整洁度和维护性，同时不破坏当前运行包、CLI、Web 和已安装环境。











  • 结论：目前不是完整的“三步闭环”。当前代码可以完成“下载 → 训练研究工件”，但“训练结果 → V3 生产评分”还没有打通。

   步骤          当前情况                                                                                                    结论
  ━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   1. 下载       ./run.sh download_history --sessions 2000 写入 data/history/baostock-daily/sessions-2000/                   可以执行
  ────────────  ──────────────────────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────
   2. 训练       ./run.sh train-tomorrow 读取默认历史目录，输出 data/train/tomorrow-v3/<run_id>/report.json 和 model.json    可以执行，但必须返回 status=validated
  ────────────  ──────────────────────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────
   3. V3 评分    ./run.sh --profile v3                                                                                       当前不能正常运行

  目前存在的断点

  1. run.sh 接受 v3，但服务器入口只允许 v1/v2，所以启动时会直接参数错误：src/trader/entrypoints/server.py:47
  2. 生产加载器只读取打包资源 trader.resources.models/tomorrow_v3_model.json：src/trader/infra/tomorrow_production_model.py:258。当前该文件不存在，实测会抛出 FileNotFoundError。
  3. 训练结果写在 data/train/.../model.json：src/trader/infra/research/tomorrow_v3_training.py:61，但生产加载器不会读取这个目录。因此训练完成后，模型不会自动参与评分。
  4. 下载如果使用自定义 --runtime-dir，训练仍然固定读取项目根目录 data/history：src/trader/entrypoints/research_commands.py:248。只有使用默认目录时两步才匹配。
  5. run.ps1 的帮助文字写了支持 v3，但实际校验仍只允许 v1/v2：run.ps1:67。

  正确的完整流程应该是

  1. 下载：

  ./run.sh download_history --sessions 2000

  必须确认输出包含：

  - state=completed
  - coverage_status=coverage_ready
  - v3_dataset_status=dataset_ready

  如果是 completed_with_failures，不要训练。

  2. 训练：

  ./run.sh train-tomorrow

  必须确认：

  status=validated

  并检查：
  data/train/tomorrow-v3/<run_id>/report.json
  data/train/tomorrow-v3/<run_id>/model.json

  3. 生产接入：
  - 将训练模型转换为打包资源；
  - 增加 tomorrow_v3_model.json；
  - 修复 server/profile 的 v3 支持；
  - 完成独立人工授权和发布门禁。

  这是当前策略文档要求的独立高风险批次，不会由 train-tomorrow 自动完成。

  4. 接入完成后，才运行：

  ./run.sh --profile v3

  并检查 /api/v2/status 中：

  tomorrow_model.profile_id = v3
  tomorrow_model.active = true
  tomorrow_model.model_hash = 训练模型 hash

  还要注意：V3 线上评分不是直接读取 data/train 中的历史行，而是读取训练生成的模型参数；运行时使用实时行情构造特征，再由该模型参与 Tomorrow 评分。

  所以，当前可以先执行前两步生成研究工件，但不能把第三步当成已经衔接完成。下一步应该先修复“训练工件晋级到 V3 生产资源”的接缝，再进行正式下载、训练和 V3 评分。

