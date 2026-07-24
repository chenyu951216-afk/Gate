from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "bitget-quant-ranking-scanner"
    app_env: str = "production"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8080
    timezone: str = "Asia/Taipei"
    database_url: str | None = None
    database_startup_retries: int = 12
    database_startup_retry_delay_seconds: float = 5.0
    gate_rest_base_url: str = "https://api.gateio.ws/api/v4"
    gate_ws_url: str = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    gate_api_key: str | None = None
    gate_api_secret: str | None = None
    gate_request_timeout_seconds: float = 15.0
    gate_max_concurrency: int = 8
    gate_requests_per_second: float = 8.0
    gate_retry_attempts: int = 4
    gate_circuit_failure_threshold: int = 5
    gate_circuit_recovery_seconds: float = 30.0
    gate_settle: str = "usdt"
    gate_margin_mode: str = "cross"
    gate_position_mode: str = "single"
    gate_market_order_slip_ratio: float = 0.01
    # Bitget v2 USDT-M futures settings.  These are intentionally separate
    # from the legacy Gate fields so an old environment cannot accidentally
    # route real orders to the wrong venue.
    bitget_rest_base_url: str = "https://api.bitget.com"
    bitget_ws_url: str = "wss://ws.bitget.com/v2/ws/public"
    bitget_api_key: str | None = None
    bitget_api_secret: str | None = None
    bitget_api_passphrase: str | None = None
    bitget_product_type: str = "USDT-FUTURES"
    bitget_margin_coin: str = "USDT"
    bitget_request_timeout_seconds: float = 15.0
    bitget_max_concurrency: int = 8
    bitget_requests_per_second: float = 8.0
    bitget_retry_attempts: int = 4
    bitget_circuit_failure_threshold: int = 5
    bitget_circuit_recovery_seconds: float = 30.0
    bitget_margin_mode: str = "crossed"
    bitget_position_mode: str = "one_way_mode"
    trading_mode: str = "live"
    test_mode_notional_multiplier: float = 0.1
    coinglass_enabled: bool = False
    coinglass_api_key: str | None = None
    coinglass_base_url: str = "https://open-api-v4.coinglass.com"
    coinglass_request_timeout_seconds: float = 10.0
    coinglass_max_concurrency: int = 4
    coinglass_requests_per_second: float = 4.0
    coinglass_retry_attempts: int = 2
    coinglass_exchange_list: str = "Binance,OKX,Bybit"
    # CoinGlass Hobbyist plans require >=4h for aggregated liquidation
    # history. Gate's scanner still runs every 30 minutes; CoinGlass is a
    # supplemental input and can be overridden to 30m on an eligible plan.
    coinglass_interval: str = "4h"
    coinglass_history_limit: int = 48
    coinglass_cache_ttl_seconds: int = 1800
    coinglass_use_heatmap: bool = True
    coinglass_heatmap_range: str = "1d"
    coinglass_require_heatmap: bool = False
    coinglass_max_symbols_per_scan: int = 100
    min_24h_turnover_usdt: float = 7_000_000
    max_spread_pct: float = 0.10
    min_30m_candles: int = 240
    min_4h_candles: int = 150
    min_data_completeness_pct: float = 70.0
    ranking_min_score: float = 55.0
    blacklist_contracts: str = ""
    scan_delay_seconds: int = 20
    scan_on_startup: bool = False
    scheduler_enabled: bool = True
    auto_order_enabled: bool = False
    position_management_enabled: bool = False
    position_manager_interval_seconds: int = 5
    position_market_refresh_seconds: int = 15
    entry_order_mode: str = "limit"
    limit_entry_offset_pct: float = 0.0005
    # A small price excursion is normal for crypto perpetuals.  Do not
    # cancel a pending entry on the first management tick just because the
    # market moved a fraction away from its passive limit price.
    limit_entry_cancel_move_pct: float = 0.015
    limit_entry_min_cancel_move_pct: float = 0.015
    limit_entry_min_observation_seconds: int = 30
    limit_entry_momentum_confirmations: int = 2
    limit_entry_hard_move_pct: float = 0.03
    limit_entry_hard_move_min_observation_seconds: int = 10
    # Keep a limit entry under continuous monitoring for up to three hours.
    # Momentum movement can still cancel it earlier.
    limit_entry_timeout_seconds: int = 10_800
    max_market_driver_positions: int = 2
    max_total_positions: int = 20
    max_same_direction_orders_per_batch: int = 2
    market_driver_contracts: str = "BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,HYPE_USDT"
    market_driver_notional_contracts: str = "BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,HYPE_USDT,ZEC_USDT"
    regular_alt_notional_usdt: float = 2_000.0
    stock_notional_usdt: float = 5_000.0
    market_driver_notional_usdt: float = 10_000.0
    btc_eth_notional_usdt: float = 20_000.0
    minimum_order_rr: float = 1.0
    require_max_leverage: bool = True
    max_initial_stop_loss_usdt: float = 1_000.0
    stop_loss_buffer_atr: float = 0.9
    fallback_stop_atr: float = 2.2
    take_profit_1_pct: float = 0.25
    take_profit_2_pct: float = 0.30
    take_profit_3_pct: float = 0.25
    runner_pct: float = 0.20
    order_trigger_expiration_seconds: int = 0
    trading_control_token: str = "change-this-trading-token"
    manual_scan_token: str = "change-this-token"
    admin_bearer_token: str = "change-this-admin-token"
    discord_webhook_url: str | None = None
    scan_discord_webhook_url: str | None = None
    order_discord_webhook_url: str | None = None
    discord_cooldown_seconds: int = 900
    discord_max_retries: int = 4
    discord_max_timepoints: int = 50
    public_base_url: str = "http://localhost:8080"
    replay_max_hours: int = 168
    replay_max_concurrent_jobs: int = 1
    replay_require_historical_spread: bool = True
    replay_require_historical_active_flow: bool = False
    replay_cache_ttl_seconds: int = 3600
    backtest_default_fee_pct: float = 0.05
    backtest_default_slippage_pct: float = 0.02

    @property
    def blacklist(self) -> set[str]:
        return {item.strip().upper() for item in self.blacklist_contracts.split(",") if item.strip()}

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return value

    @field_validator("gate_market_order_slip_ratio")
    @classmethod
    def validate_market_order_slip_ratio(cls, value: float) -> float:
        return min(0.015, max(0.000001, value))

    @field_validator("gate_margin_mode", mode="before")
    @classmethod
    def force_cross_margin(cls, value: str) -> str:
        # This strategy is intentionally cross-margin only. A stale Zeabur
        # environment value of `isolated` must not silently re-enable it.
        return "cross"

    @field_validator("gate_position_mode", mode="before")
    @classmethod
    def force_single_position_mode(cls, value: str) -> str:
        # Protection orders use Gate one-way close semantics (size=0, close=true).
        # A stale dual/dual_plus environment value must not silently enable an
        # incompatible order format.
        return "single"

    @field_validator("bitget_margin_mode", mode="before")
    @classmethod
    def force_bitget_cross_margin(cls, value: str) -> str:
        return "crossed"

    @field_validator("bitget_position_mode", mode="before")
    @classmethod
    def force_bitget_one_way_mode(cls, value: str) -> str:
        return "one_way_mode"

    @field_validator("trading_mode", mode="before")
    @classmethod
    def validate_trading_mode(cls, value: str) -> str:
        mode = str(value or "live").lower()
        if mode in {"formal", "production", "real"}:
            return "live"
        if mode not in {"live", "test"}:
            raise ValueError("TRADING_MODE must be live or test")
        return mode

    @field_validator("test_mode_notional_multiplier")
    @classmethod
    def validate_test_multiplier(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("test mode notional multiplier must be between 0 and 1")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
