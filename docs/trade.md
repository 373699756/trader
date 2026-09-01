# Tomorrow 训练方案迁移索引

本文件原有的日线数据规模、训练切分、统一模型、分层及个股残差、V1/V2/C3 联合、收益门禁、工件、
主程序交互和离线命令已经合并到
[`recommendation-strategy.md` 第 15.1.35–15.1.37 节](recommendation-strategy.md#15135-tomorrow-日线收盘代理训练分支)。

候选、过滤、标签、评分、风险、融合、动作、排名和策略验收只以 `recommendation-strategy.md` 为权威；
产品架构、运行、配置、API、发布和运维只以 `software-business-design.md` 为权威。本文件不再维护第二套
训练规则、任务状态、参数或生产授权，避免重复文档漂移。

当前状态仍为：V3 研究工程执行中，生产 profile 仍只接受 V1/V2，默认 V1。任何训练或历史报告都固定
`production_authority=false`；只有日线代理留出与独立 14:50 点时终端留出均通过，并在用户另行授权的
高风险生产批次完成模型资源、配置 schema、机器契约和发布门禁后，V3 才能成为可选生产 profile。
