# 工程 Review 问题合并优化计划

  ## Summary

  - 优先解决三类问题：策略可靠性、工程边界、可观测性能。
  - 单用户场景下不做多客户端 SSE 重构；保留现有推送机制，只优化单用户响应和行情新鲜度。
  - 长期 Tab 改造成后端独立观察池，重点服务“低估值 + 国产替代/卡脖子/政策扶持 + 龙头质量”。
  - 今早策略按新规范保留为 09:30–14:00 可执行策略，补齐展示、测试和验证口径一致性。

  ## Key Changes

  - 策略与收益闭环：
      - 以 docs/strategy_and_prediction.md 为唯一策略规范，清理与旧 plan.md 口径冲突的说明。
      - 保持三大生产策略：today_term、tomorrow_picks、swing_picks。
      - 长期池只做观察，不进入执行、不写验证收益、不改变三策略 Top K。
      - 禁止一次性打开 expected-return、Meta、Regime、Factor IC 等多个收益增强开关；真实前瞻样本不足前全部维持影子或关闭。
      - 补健康门控：真实交易日数、有效样本数、主池净收益、回撤、unfilled、unknown、样本类型必须在验证页明确展示。

  - 长期推荐池重构：
      - 后端新增 long_term_watch 生成逻辑，前端只展示后端结果。
      - 候选来源合并：三策略候选并集、卡脖子/国产替代龙头名单、低估值高质量基本面候选。
      - 综合评分：估值 35%、龙头质量 25%、战略支撑 25%、成长质量 15%。
      - 入选要求：估值低或合理、命中国产替代/卡脖子/政策扶持等战略方向、具备龙头或准龙头特征、风险不过高。
      - 降权/过滤：短期涨幅过热、极端高估、基本面质量低、黑名单、ST/退市风险、亏损严重。
      - 页面列名改为“长期观察依据”，不展示交易动作。

  - 今早策略收口：
      - 保留 09:30–14:00 买入窗口和 execution_window_status。
      - 主池满足 TODAY_RECOMMENDATION_MIN_SCORE 才允许 execution_allowed=true。
      - 备选池始终 execution_allowed=false、仓位为 0，只作观察。
      - 验证回填使用明日/后日动态退出净收益，不再用同日收盘延续替代。

      i - 架构重构：
      - 拆 AppServices：推荐、验证、预测、回测、健康服务分离。
      - 拆 validation_repository.py：signal、outcome、candidate、tuning、research、prediction repository 分离。
      - 拆 providers.py：实时行情、历史行情、新闻事件、基本面 provider adapter 分离。
      - 整理泛命名文件：app_support.py、app_runtime_support.py、app_response_support.py 改为按职责命名。
      - 先做行为等价拆分，不改变三策略排序、版本、schema 和收益口径。

  - 性能与数据库：
      - 单用户下保留 SSE + 60 秒 HTTP 兜底。
      - 优化目标改为：推荐池最终股票行情快速刷新、接口不等待慢外部源、后台刷新失败不阻塞旧快照。
      - 增加 API 耗时、行情年龄、缓存命中率、DB 查询耗时、后台任务状态到健康 payload。
      - 补 EXPLAIN QUERY PLAN 回归，覆盖验证页、最新推荐、策略信号查询、收益回填查询。
      - SQLite 保留 WAL、busy_timeout、外键配置；避免新增散落 sqlite3.connect。

  - 前端优化：
      - 修复 recommendation-app.js 缩进和长期池本地评分逻辑。
      - 长期 Tab 改为后端数据驱动。
      - 状态栏减少噪音，突出：行情时间、推送状态、样本健康、策略门控。
      - 保留股票列/最新价列宽度回归。
      - 增加窄屏和桌面布局契约测试，长期池空状态测试。

  ## Test Plan

  - 策略测试：
      - 今早主池/备选池执行权限、时间窗口、行业上限、动态备选阈值。
      - 长期池低估值龙头入选、高估热门股降权、无关键词但龙头名单命中可入选。
      - 长期池不改变三策略排序、不写验证收益、不生成执行记录。
      - 收益增强开关在真实样本不足时不能影响生产排序。

  - API/数据库测试：
      - 推荐 payload 包含 long_term_watch 和长期评分拆解。
      - 验证统计按策略、样本类型、snapshot phase 分组。
      - EXPLAIN QUERY PLAN 确认关键查询走索引。
- 重复请求同日推荐结果稳定，缓存命中不触发慢外部源。

  - 前端测试：
      - 长期 Tab 只使用后端结果。
      - 长期列文案为“长期观察依据”。
      - 不显示长期交易动作。
      - 股票列 88px、最新价列 60px 回归继续通过。
      - 桌面和窄屏不重叠、不溢出。

  - 回归测试：
      - 现有推荐、快照、验证回填、前端契约、性能契约测试全部运行。
      - 对架构拆分阶段增加 payload 深度等价测试，确保拆分前后输出一致。

  ## Rollout Order

  - P0：修正长期池口径和前端展示，补测试；修复 JS 缩进和长期本地评分残留。
  - P1：补健康指标、DB query plan 回归、策略门控展示。
  - P2：行为等价拆 AppServices 和长期池服务；不改业务结果。
  - P3：拆 repository/provider 大文件；保留兼容导入 shim。
  - P4：真实前瞻样本达到门槛后，再逐个验证 expected-return、Factor IC、Meta、Regime，不同时启用。

  ## Assumptions

  - 当前是单用户本地系统，不按多用户并发服务设计。
  - 长期池目标是低估值、未来潜力大、国产替代/卡脖子/政策扶持方向的行业龙头。
  - 长期池只作观察，不作为第四个执行策略。
  - docs/strategy_and_prediction.md 是唯一策略口径来源。
