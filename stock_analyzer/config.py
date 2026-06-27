import os


REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "30"))
DEFAULT_TOP_N = int(os.getenv("DEFAULT_TOP_N", "20"))
MIN_TURNOVER = float(os.getenv("MIN_TURNOVER", "50000000"))
MAX_RECOMMENDED_GAIN = float(os.getenv("MAX_RECOMMENDED_GAIN", "12"))
MAX_BUYABLE_GAIN_MAIN = float(os.getenv("MAX_BUYABLE_GAIN_MAIN", "6.5"))
MAX_BUYABLE_GAIN_GROWTH = float(os.getenv("MAX_BUYABLE_GAIN_GROWTH", "10"))
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
ALLOW_SLOW_QUOTE_FALLBACK = os.getenv("ALLOW_SLOW_QUOTE_FALLBACK", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
STATE_PATH = os.getenv("STATE_PATH", ".runtime/recommendation_state.json")
QUOTE_SNAPSHOT_PATH = os.getenv("QUOTE_SNAPSHOT_PATH", ".runtime/latest_quotes.json")
QUOTE_SNAPSHOT_MAX_AGE_SECONDS = int(os.getenv("QUOTE_SNAPSHOT_MAX_AGE_SECONDS", "21600"))
QUOTE_SNAPSHOT_MIN_ROWS = int(os.getenv("QUOTE_SNAPSHOT_MIN_ROWS", "50"))
VALIDATION_DB_PATH = os.getenv("VALIDATION_DB_PATH", ".runtime/strategy_validation.sqlite3")
HISTORY_FACTOR_LIMIT = int(os.getenv("HISTORY_FACTOR_LIMIT", "40"))
HISTORY_CACHE_PATH = os.getenv("HISTORY_CACHE_PATH", ".runtime/history_cache.sqlite3")
HISTORY_CACHE_FRESHNESS_HOURS = int(os.getenv("HISTORY_CACHE_FRESHNESS_HOURS", "18"))
VALIDATION_TRADE_COST_PCT = float(os.getenv("VALIDATION_TRADE_COST_PCT", "0.25"))
EXIT_STOP_LOSS_PCT = float(os.getenv("EXIT_STOP_LOSS_PCT", "5.0"))
EXIT_TAKE_PROFIT_PCT = float(os.getenv("EXIT_TAKE_PROFIT_PCT", "8.0"))
EXIT_TRAILING_STOP_PCT = float(os.getenv("EXIT_TRAILING_STOP_PCT", "4.0"))
ENABLE_HISTORY_FACTORS = os.getenv("ENABLE_HISTORY_FACTORS", "0").lower() in ("1", "true", "yes", "on")
ENABLE_INLINE_SENTIMENT = os.getenv("ENABLE_INLINE_SENTIMENT", "0").lower() in ("1", "true", "yes", "on")
ENABLE_MARKET_NEWS = os.getenv("ENABLE_MARKET_NEWS", "0").lower() in ("1", "true", "yes", "on")
ENABLE_HOT_RANKS = os.getenv("ENABLE_HOT_RANKS", "0").lower() in ("1", "true", "yes", "on")
ENABLE_INDUSTRY_STRENGTH = os.getenv("ENABLE_INDUSTRY_STRENGTH", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
EASTMONEY_TIMEOUT_SECONDS = float(os.getenv("EASTMONEY_TIMEOUT_SECONDS", "1"))
EASTMONEY_PAGE_SIZE = int(os.getenv("EASTMONEY_PAGE_SIZE", "500"))
EASTMONEY_MAX_PAGES = int(os.getenv("EASTMONEY_MAX_PAGES", "1"))
EASTMONEY_SORT_FIELD = os.getenv("EASTMONEY_SORT_FIELD", "f6")

# 回测校准脚本（calibrate.py）写出的权重覆盖文件；scoring/backtest 启动时若存在则加载。
WEIGHTS_OVERRIDE_PATH = os.getenv("WEIGHTS_OVERRIDE_PATH", ".runtime/weights.json")

# 小市值策略的流通市值下限（元），过滤纯壳/退市风险股。默认 8 亿。
SMALLCAP_MIN_FLOAT_CAP = float(os.getenv("SMALLCAP_MIN_FLOAT_CAP", "800000000"))

SUPPORTED_PREFIXES = (
    "600",
    "601",
    "603",
    "605",
    "000",
    "001",
    "002",
    "003",
    "300",
    "301",
    "688",
)

MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
CHINEXT_PREFIXES = ("300", "301")
STAR_PREFIXES = ("688",)

MARKET_LABELS = {
    "main": "主板",
    "chinext": "创业板",
    "star": "科创板",
}
