# 软件设计与运行说明

本文档说明这个软件当前的整体结构、页面组成、数据流、接口、异步刷新和验证保存逻辑。

## 目录

- [关键文件索引](#关键文件索引)
- [1. 软件目标](#1-软件目标)
- [2. 页面结构](#2-页面结构)
- [3. 核心模块划分](#3-核心模块划分)
- [4. 推荐页数据流](#4-推荐页数据流)
- [5. 明天 / 2-5天单独接口](#5-明天--2-5天单独接口)
- [6. 策略验证页数据流](#6-策略验证页数据流)
- [7. 保存逻辑](#7-保存逻辑)
- [8. 数据与备份](#8-数据与备份)
- [9. 常用接口](#9-常用接口)
- [10. 启动与环境变量](#10-启动与环境变量)
- [11. 当前实现原则](#11-当前实现原则)
- [12. 常见坑](#12-常见坑)

## 关键文件索引

后端入口与编排：

- [stock_analyzer/app.py](/home/c/linux/trader/stock_analyzer/app.py:1)
- [stock_analyzer/app_runtime_support.py](/home/c/linux/trader/stock_analyzer/app_runtime_support.py:1)
- [stock_analyzer/recommendation_runtime_support.py](/home/c/linux/trader/stock_analyzer/recommendation_runtime_support.py:1)
- [stock_analyzer/validation_runtime_support.py](/home/c/linux/trader/stock_analyzer/validation_runtime_support.py:1)

策略与预测：

- [stock_analyzer/scoring.py](/home/c/linux/trader/stock_analyzer/scoring.py:1)
- [stock_analyzer/prediction.py](/home/c/linux/trader/stock_analyzer/prediction.py:1)
- [stock_analyzer/stock_optimization.py](/home/c/linux/trader/stock_analyzer/stock_optimization.py:1)
- [stock_analyzer/deepseek_client.py](/home/c/linux/trader/stock_analyzer/deepseek_client.py:1)

页面与前端：

- [templates/index.html](/home/c/linux/trader/templates/index.html:1)
- [static/app.js](/home/c/linux/trader/static/app.js:1)
- [static/styles.css](/home/c/linux/trader/static/styles.css:1)

## 1. 软件目标

本项目是一个本地 Flask 看板，用公开行情数据生成 A 股推荐候选，并把推荐结果保存到验证库里做持续复盘。

结果只用于研究，不构成投资建议，也不保证盈利。

当前保留功能：

- 三类荐股：今天推荐、明天推荐、2-5 天推荐
- 每类最多展示 18 支；如果没有满足条件的股票，可以空推荐
- 策略验证：按日期查看历史保存批次、样本表现、股票明细
- 自动保存：交易日 14:30 后按配置间隔保存当天三类推荐，15:00 后使用收盘价作为锚点
- DeepSeek：参与三类推荐的候选复核、风险降权、剔除理由、每日复盘和影子调参建议
- 个股预测：输入股票代码，返回本地预测和 DeepSeek 优化建议

## 2. 页面结构

当前 UI 只有两个主页面：

- 推荐池
- 策略验证

模板文件：

- [templates/index.html](/home/c/linux/trader/templates/index.html:1)

### 2.1 推荐池

推荐池页面当前结构：

- 顶部状态条
- 顶部动作汇总区 `recommendationActionSummary`
- 周期切换按钮：
  - `今天`
  - `明天`
  - `2-5天`
- 三组表格：
  - `shortTermBody`
  - `tomorrowBody`
  - `swingBody`

### 2.2 策略验证

策略验证页当前结构：

- 顶部策略切换
- 统计窗口选择
- 顶部工具区
- 当前批次一句话结论
- 统计卡片
- 保存批次分页
- 股票明细表

其中顶部工具区已经改成双列：

- 左列：
  - 股票代码输入
  - `预测`
  - `DeepSeek 优化建议`
- 右列：
  - 共享结果区 `toolResultPane`

也就是说，预测结果和优化建议统一显示在同一个结果区域，不再拆成两个独立结果卡。

## 3. 核心模块划分

### 3.1 路由与页面入口

文件：

- [stock_analyzer/app.py](/home/c/linux/trader/stock_analyzer/app.py:1)

职责：

- Flask 应用初始化
- HTTP 路由
- 推荐页数据入口
- horizon 接口入口
- 策略验证接口入口
- 个股预测接口入口

### 3.2 推荐编排层

文件：

- [stock_analyzer/recommendation_runtime_support.py](/home/c/linux/trader/stock_analyzer/recommendation_runtime_support.py:1)

职责：

- 三类本地策略调用
- DeepSeek rerank 挂接
- 推荐结果分 horizon 组织
- 推荐 meta 汇总

### 3.3 运行时支撑层

文件：

- [stock_analyzer/app_runtime_support.py](/home/c/linux/trader/stock_analyzer/app_runtime_support.py:1)

职责：

- DeepSeek rerank 接入与降级
- DeepSeek 验证复盘接入与降级
- 个股预测优化接入与降级
- 风险黑名单摘要等 runtime 级辅助逻辑

### 3.4 本地预测与个股优化

文件：

- [stock_analyzer/prediction.py](/home/c/linux/trader/stock_analyzer/prediction.py:1)
- [stock_analyzer/stock_optimization.py](/home/c/linux/trader/stock_analyzer/stock_optimization.py:1)

职责：

- 本地个股预测聚合
- 个股 DeepSeek 优化建议

### 3.5 DeepSeek 客户端

文件：

- [stock_analyzer/deepseek_client.py](/home/c/linux/trader/stock_analyzer/deepseek_client.py:1)

职责：

- DeepSeek 运行时配置
- 缓存与 JSON 解析
- 候选池 rerank
- 策略验证复盘

### 3.6 前端

文件：

- [static/app.js](/home/c/linux/trader/static/app.js:1)
- [static/styles.css](/home/c/linux/trader/static/styles.css:1)

职责：

- 页面切换
- 推荐池按钮切换
- 推荐表格渲染
- 策略验证页异步加载
- 顶部工具区交互
- 共享结果区渲染

## 4. 推荐页数据流

### 4.1 总入口

接口：

- `GET /api/recommendations?top_n=18&market=all`

主链路：

```text
/api/recommendations
  -> _recommendations_payload()
  -> _build_recommendations_payload(include_deepseek=False 或 True)
  -> build_recommendation_horizons()
  -> 返回推荐结果和 meta
```

### 4.2 为什么推荐页能先快返回

推荐总入口不是每次都同步等 DeepSeek。

当前逻辑是：

1. 如果已有缓存 / 快照，先直接返回
2. 后台异步刷新真正带 DeepSeek 的结果
3. 首次没有 ready 结果时，先用 `include_deepseek=False` 返回本地结果
4. 再调度后台刷新 DeepSeek 版结果

也就是：

```text
先返回本地结果
  -> 后台异步算带 DeepSeek 的推荐
  -> 下次请求命中 ready 缓存
```

## 5. 明天 / 2-5天单独接口

接口：

- `GET /api/tomorrow-picks?top_n=18&market=all`
- `GET /api/swing-picks?top_n=18&market=all`

主逻辑在 [stock_analyzer/app.py](/home/c/linux/trader/stock_analyzer/app.py:821) 的 `_horizon_payload(...)`。

当前返回优先级：

1. 先读内存缓存
2. 没有缓存则立即调度后台刷新
3. 如果有最近保存的验证记录，则先返回保存结果
4. 如果保存结果也没有，则返回空列表 + `async_refresh_pending`

因此这两个接口允许：

- 先显示最近保存结果
- 后台再异步刷新
- 没数据时先空占位，但不中断页面

当前 fallback/source 可能出现：

- `memory_cache`
- `saved_snapshot`
- `async_refresh_pending`

## 6. 策略验证页数据流

### 6.1 验证对象

只验证三类荐股策略：

- `short_term`
- `tomorrow_picks`
- `swing_picks`

页面顶部三个按钮切换策略后，以下内容都必须联动：

- 日期批次
- 统计卡片
- 股票明细
- DeepSeek 复盘

### 6.2 顶部工具区

顶部工具区当前是共享结果模式：

- 左侧两个动作入口：
  - `预测`
  - `DeepSeek 优化建议`
- 右侧统一结果区：
  - `toolResultPane`

这部分的目的是：

- 避免重复卡片
- 让预测结果和优化建议都落在同一个阅读区域
- 保持顶部区域稳定，不因结果类型切换出现明显跳动

### 6.3 异步加载要求

策略验证页的性能目标：

- 页面刷新进入策略验证时，先轻量加载日期批次
- 指标、明细、DeepSeek 复盘异步加载
- 切换今天、明天、2-5 天不应重新跑荐股流
- 几十条验证数据不应出现 30 秒级等待

当前还允许：

- 明天 / 2-5 天如果实时结果尚未完成，先显示最近保存结果或空占位，并标记后台刷新中

## 7. 保存逻辑

验证库只保存三类策略：

- `short_term`
- `tomorrow_picks`
- `swing_picks`

保存逻辑：

- 14:30 后自动按配置间隔保存三类推荐快照
- 同一天同策略只保留最后一次批次
- 每类最多保存 18 支；不满足条件可以保存 0 支空批次
- 15:00 后运行时，使用当天收盘价作为锚点
- 如果 15:00 后无法取得收盘锚点，拒绝保存为真实回溯锚点
- 保存成功后自动备份验证数据库

## 8. 数据与备份

- 验证数据库：`.runtime/strategy_validation.sqlite3`
- 自动备份目录：`.runtime/backups`
- 备份列表：`.venv/bin/python -m stock_analyzer.daily_job --list-validation-backups`
- 还原备份：`.venv/bin/python -m stock_analyzer.daily_job --restore-validation <backup-file>`

## 9. 常用接口

- `GET /api/recommendations?top_n=18&market=all`
- `GET /api/tomorrow-picks?top_n=18&market=all`
- `GET /api/swing-picks?top_n=18&market=all`
- `GET /api/stock-prediction/<code>`
- `GET /api/strategy-validation?strategy=tomorrow_picks`
- `GET /api/strategy-validation/tuning?strategy=tomorrow_picks`
- `POST /api/strategy-validation/tuning?strategy=tomorrow_picks`

其中 `market=all` 表示 A 股主板+创业板+科创板。

## 10. 启动与环境变量

启动：

```bash
chmod +x run.sh
./run.sh
```

默认地址：

```text
http://127.0.0.1:5000
```

常用环境变量：

```bash
PORT=5050 ./run.sh
ENABLE_HISTORY_FACTORS=1 ./run.sh
VALIDATION_AUTO_SNAPSHOT_TIME=15:00 ./run.sh
VALIDATION_AUTO_UPDATE_START_TIME=14:30 ./run.sh
VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS=600 ./run.sh
ENABLE_DEEPSEEK_RUNTIME=1 ./run.sh
```

DeepSeek 接口约定：

- `DEEPSEEK_BASE_URL`
- `DEEPSEEK_MODEL`
- `DEEPSEEK_PRO_MODEL`

当前仅使用 DeepSeek v4 系列（`deepseek-v4-flash`、`deepseek-v4-pro`）。

## 11. 当前实现原则

这套软件当前遵循三条原则：

1. 本地策略先出结果，DeepSeek 只做增强层
2. 页面优先可用，异步补齐慢结果
3. 验证与调参只做影子建议，不自动改正式策略

## 12. 常见坑

- 前端 `res.json()` 报错，不一定是前端问题。很多时候是后端抛异常后返回了 HTML 500 页面，前端把 HTML 当 JSON 解析才报错。
- 策略验证页如果出现 `JSON.parse` / `unexpected character`，先直接检查对应接口是否真的返回 `application/json`。
- `app.py` 里如果删掉类似 `storage_strategy_name` 这类规范化函数的导入，常见表现不是页面白屏，而是某个接口 silently 变成 Werkzeug 调试页。
- 推荐页和验证页很多地方是“先轻量返回，再异步刷新”，所以“先看到空占位 / 最近保存结果”不一定是错，先看接口返回里的 `snapshot.source` / `fallback`。
- DeepSeek 相关问题优先区分三层：
  - runtime 是否开启
  - 是否命中缓存 / fallback
  - 是候选 rerank 问题，还是验证复盘问题，还是个股优化问题
