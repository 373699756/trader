# youhua G1 CONTRACT_BASE 发布记录

状态：G1 已发布；阶段 2 可从 `CONTRACT_BASE` 开始，但各 Codex 仍只能执行各自 owner 范围。

## 1. CONTRACT_BASE

| 项 | 值 |
| --- | --- |
| CONTRACT_BASE | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` |
| contract_commit_subject | `docs: freeze youhua A1 contracts` |
| published_by | Codex A |
| published_in | 本报告提交 |
| upstream_check | 发布前 `HEAD == @{upstream}` |
| stage_gate | G1 |

`CONTRACT_BASE` 指向已推送的 A1 契约冻结提交，包含 `youhua_contract_base_v1`、双层内存
契约、公共接缝版本、owner 边界和 A1 公共失败测试。B/C/D 阶段 2 worktree 必须从该 commit
或包含该 commit 的等价后续基线开始，不得从阶段 1 的
`777e73d445f88c165126d1a09d02b833453b9d3e` 继续实现。

## 2. 阶段 1 报告收齐状态

| Codex | 报告路径 | base_commit | ready_for_gate |
| --- | --- | --- | --- |
| B | `tests/fixtures/market_data/youhua_b1/report_to_a.md` | `777e73d445f88c165126d1a09d02b833453b9d3e` | yes |
| C | `tests/fixtures/deepseek/youhua_c1/report_to_a.md` | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` | yes |
| D | `docs/reports/youhua-d1-p6-web.md` | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` | yes |

B 的阶段 1 报告基于 A1 前一提交完成，但只包含盘点报告和 fixture，不包含生产代码或公共
接口修改；B2 必须重新基于 `CONTRACT_BASE`。

## 3. 唯一 owner 与 schema 清单

| 接缝 | 冻结版本 | 公共 owner | 生产 owner |
| --- | --- | --- | --- |
| P3 -> P4 feature/change envelope | `p3_p4_feature_snapshot_market_change_set_v1` | A | B producer，A/P4 consumer |
| Market change set | `market_change_set_v1` | A | B producer |
| P4 -> P5 high-value review input | `p4_p5_high_value_review_manifest_v1` | A | A/P4 producer，C consumer |
| DeepSeek V4 facts | `deepseek_v4_review_facts_v1` | A | C implementation |
| Evidence manifest | `evidence_manifest_v1` | A | C computes/consumes |
| Review owner strategy/cache identity | `review_owner_strategy_identity_v1` | A | C consumer |
| P4/P5 -> P6 projection event | `p4p5_p6_projection_event_v1` | A | A/P4/P5 producer，D consumer |
| P6 overlay event | `p6_overlay_event_v1` | A | A producer，D consumer |
| P6 resync reason | `p6_resync_reason_v1` | A | D consumer |

三方不得创建第二套公共 schema/version；需要公共签名变化时提交 amendment 申请，由 A 修改
公共文件并更新本清单。

## 4. 接口申请归并结果

- B1 的 P3 -> P4 envelope 和 `MarketChangeSet` 字段进入 A2.1 公共类型范围；A2.2 提供只记录
  identity/count/dirty 范围的 P4 consumer 替身。
- C1 的高价值复核输入、V4 facts、证据 manifest、价格反映桶和 owner strategy 进入 A2.1
  公共类型范围；A2.2 提供 `MarketChangeSet` 与复核输入替身，long 集合必须为空。
- D1 的 projection event、overlay event、patch schema、CAS/version 和 resync reason 进入
  A2.1 公共 event 范围；A2.2 提供 projection/overlay producer 替身，不接真实 Web DOM。

## 5. 合并顺序

1. A2：公共类型、schema/version 校验、配置/状态双层内存契约、测试替身和集成骨架。
2. B2：P1-P3 列式和 dirty，只修改 B owner 路径，基于 A2 公共 P3 -> P4 接缝。
3. C2：DeepSeek V4 facts、路由、预算和缓存，只修改 C owner 路径，基于 A2 review 接缝。
4. D2：P6、SSE 和浏览器增量，只修改 D owner 路径，基于 A2 projection/overlay 接缝。
5. G2：四个实现包分别通过单域测试后，A 再按 B -> C -> D 进入阶段 3 集成。

## 6. 已知门禁风险

- 当前仓库全局 `make lint` 仍因既有严格债务计数漂移失败。
- 当前仓库全局 `make test` 仍有 5 个既有失败，集中在 application port 边界、bootstrap、
  历史当前报价 overlay 和 final candidate cadence。
- G1 只证明阶段 1 报告和公共契约收齐，不宣称 G2 或生产实现完成。
