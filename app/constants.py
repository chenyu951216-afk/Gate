from zoneinfo import ZoneInfo

APP_TIMEZONE = ZoneInfo("Asia/Taipei")
SETTLE = "usdt"
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "4h": 14400}
SUPPORTED_INTERVALS = tuple(INTERVAL_SECONDS)
RANKING_TYPES = ("combined", "long", "short")
MAX_DISCORD_MESSAGE_LENGTH = 2000
MAX_TOP_N = 10
FATAL_RISK_FLAGS = {"api_partial_failure", "historical_data_gap", "time_alignment_error"}

