# A 股实时强势股推荐看板

本项目是一个本地 Flask 看板，用 AKShare 拉取 A 股实时行情、热度和新闻舆情，按短线强势逻辑给沪深主板、创业板、科创板股票打分并推荐 Top N。

结果仅供研究，不构成投资建议。

## 功能

- 支持主板、创业板、科创板，默认排除北交所、ST、退市、停牌和低流动性股票。
- 每 60 秒刷新短期 10 支和长期 10 支推荐列表。
- 短期评分偏行情动能、量价强度、人气热度和实时舆情。
- 长期评分偏 60 日/YTD 趋势、流动性、行业强度、舆情质量和风险过滤。
- AlphaLite 历史因子增强：3/5/10/20 日动量、均线偏离、成交额放大、20 日突破、20 日波动率。
- TopK-Dropout 稳定榜单：展示新进、留存、连续上榜次数，减少刷新噪声。
- SQLite 历史 K 线缓存：减少重复请求免费行情接口。
- 滚动回测：按 AlphaLite 信号做多期 TopK 组合验证，输出胜率、累计收益、最大回撤和扣成本收益。
- 点击股票可查看相关新闻、电报、关键词命中和舆情分。
- 行情优先使用 AKShare；配置 `TUSHARE_TOKEN` 后可尝试 Tushare 降级。

## 启动

一键运行：

```bash
chmod +x run.sh
./run.sh
```

默认打开 `http://127.0.0.1:5000`。可用环境变量改端口：

```bash
PORT=5050 ./run.sh
```

手动运行：

依赖当前 AKShare 版本，需要 Python 3.9 及以上；推荐 Python 3.11。若已有旧的 Python 3.8 虚拟环境，请先删除后重建。

```bash
rm -rf .venv .venc
/home/c/.local/bin/python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

打开 `http://127.0.0.1:5000`。

## 可选配置

```bash
export TUSHARE_TOKEN=你的token
export REFRESH_SECONDS=60
export DEFAULT_TOP_N=20
export MIN_TURNOVER=50000000
export MAX_RECOMMENDED_GAIN=18.5
export HISTORY_FACTOR_LIMIT=40
export HISTORY_CACHE_PATH=.runtime/history_cache.sqlite3
export HISTORY_CACHE_FRESHNESS_HOURS=18
```

## 接口

- `GET /api/recommendations?top_n=10&market=all`
- `GET /api/sentiment/<code>?name=<股票名>`
- `GET /api/backtest?codes=600000,000001&top_k=10&holding_days=3&mode=rolling`
- `GET /api/backtest?codes=600000,000001&top_k=10&holding_days=3&mode=snapshot`
- `GET /api/health`

`market` 可选值：`all`、`main`、`chinext`、`star`。

`/api/recommendations` 会返回：

- `recommendations.short_term`：短期 Top 10。
- `recommendations.long_term`：长期 Top 10。
- `data`：兼容字段，等同于短期 Top 10。

运行时会创建 `.runtime/recommendation_state.json` 保存稳定榜状态。

运行时会创建 `.runtime/history_cache.sqlite3` 保存日线历史数据缓存。
