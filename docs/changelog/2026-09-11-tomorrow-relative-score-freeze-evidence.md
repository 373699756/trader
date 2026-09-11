# Delivery Record: Tomorrow Relative-Score Freeze Evidence and Web Explanation

## User Request

用户要求停止历史下载，并修复 `run.sh` 页面中 Tomorrow 最高分长期高于 Today/D25、达到 90 多分却不进入
观察池所造成的评分基准和准入含义混淆。历史下载已确认没有运行进程，本批不删除已有历史文件，也不修改下载链。

## Cause

- Tomorrow V1 把同批预测超额收益的稳定分位映射为 0–100 相对信号；Today/D25 则使用不同输入和权重的规则综合分。
  三者原本就不能直接横向比较，但页面卡片统一写成“评分最高/最高最终分”，没有说明策略内口径。
- `model_net_utility_non_positive` 是独立成本门：Tomorrow 即使相对分很高，预测超额扣除约 20–40bp 成本后不为正，
  也不得进入 DeepSeek、观察池或正式池。旧页面顶部卡片没有消费该空结果原因，仍误写成普通门槛不足。
- 正式冻结会按契约删除观察和不可执行股票明细，却保留原选择诊断的最高分；公开 coverage 又用冻结后 `items` 长度
  计算已评分数，因而形成“最高分 97.44、已评分 0”的矛盾。该形状复现了既有
  `tomorrow-relative-score-cost-gate-copy` 缺陷族的剩余边界。

## Added

- `SelectionDiagnostics` 新增可选、类型化的已评分聚合数量。新决策在评分完成时写入该数量，冻结正式记录只保留聚合，
  不保存被成本、风险或观察规则拦截的股票明细。
- 新增冻结空仓、持久化往返、Decision coverage、旧记录兼容、Tomorrow 成本门顶部卡片及旧矛盾冻结记录的回归测试。

## Changed

- Decision coverage、GET、SSE 完整替换和运行诊断统一优先读取选择诊断中的已评分聚合；普通 current 与没有该字段的
  旧记录继续回退到现有 `items` 数量，旧持久化载荷保持原 hash 可读。
- Web 卡片改名为“策略内评分最高”，明确分数仅在当前策略内比较。Tomorrow 的
  `no_positive_net_utility` 使用“最高相对信号分/未通过成本门”；旧冻结记录若已评分数为 0，则显示
  “暂无可核验评分/已评分证据不完整”，不再把遗留最高值与 73/78 门槛比较。

## Fixed

- 修复 Tomorrow 相对排名被展示成可与 Today/D25 横向比较的普通最终分，以及高相对分被误解释为达到观察或正式
  门槛的问题。
- 修复新冻结记录因删除非正式股票而丢失已评分数量，进而触发
  `no_positive_net_utility_without_scored_candidates` 的覆盖语义矛盾。

## Removed

- 未删除评分、冻结、历史或下载数据。未修改 Tomorrow 模型、20–40bp 成本、73/78 门槛、固定 68/32 融合、
  DeepSeek、风险、TopK 或观察窗口。

## Verification

- 修复前主机运行诊断复现：服务使用 V1、处于 `after_close`，Tomorrow 冻结最高分 97.44、已评分 0、正式/观察均为 0，
  并报告 `no_positive_net_utility_without_scored_candidates`；沙箱 loopback 不可达后按诊断规范在主机网络复跑成功。
- 新增测试先分别因缺少 `SelectionDiagnostics.evaluated_count` 和页面仍输出“最高分 97.44”失败；实现后评分、冻结、
  持久化、查询、SSE、运行诊断、Web/架构合同及 JavaScript 定向测试通过；Today/D25 普通分数不足文案和旧冻结
  codec 邻接回归保持通过。
- 完整高风险门禁通过：`make format-check`、`make lint`、`make type-check`、`make test`、`make package`。首次格式
  检查发现并修正一个 dataclass 换行；首次打包仅因沙箱禁止隔离构建访问 PyPI 失败，获准在主机网络用同一命令
  重跑后成功。固定融合 `83.40`、架构 AST、`create_app()` 副作用、预算并发、SSE 恢复和冻结恢复均包含在完整测试中。
- 仓库外 wheel 安装通过：包可导入、公开 CLI 可运行，10 个模板、CSS、JavaScript、图标、模型和自动化资源可读。
- Firefox 三档桌面门禁通过：1280x720、1440x900、1920x1080 无白屏、重叠或页面级横向溢出；10 次 DOM 刷新
  P50 0.998 秒、P95 1.048 秒，11 次 patch-to-paint P95 34 毫秒且决策 patch 成功应用。
- 本批只增加 O(1) 聚合读取和文案分支，不改变候选、预测、融合或渲染结构，因此独立生产评分性能门禁不适用。
  真实旧服务收到 SIGTERM 后曾处于 `folio_wait_bit_common` 磁盘等待，等待其自行安全退出且未强杀；随后由
  `./run.sh` 启动默认 V1，新 PID 94891，历史下载进程为 0。主机网络三次 runtime API 采样均成功，运行身份为
  `runtime_sha256_197276bf3e395b2cd060+strategy_sha256_6493c5e53c1425b6a2f0`，状态仅保留旧冻结记录与既有数据质量
  的受控降级；真实服务返回的 HTML/JavaScript 已包含“策略内评分最高”“最高相对信号分”和“暂无可核验评分”。

## Residual Risks

- 今日已经冻结的旧记录不可覆盖，也没有保存可恢复的原始已评分数量；新页面会诚实显示证据不完整。下一交易日新冻结
  记录才会携带完整聚合计数。
- 三策略的数值口径仍按权威评分契约保持不同。本批通过策略内标注消除误比较，不宣称找到了一套有历史收益证据的统一
  绝对分标尺；若要改变模型评分公式，必须另立研究批次完成点时样本外验证。
- `Regression-Key: tomorrow-relative-score-cost-gate-copy`。
