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
    min_24h_turnover_usdt: float = 5_000_000
    max_spread_pct: float = 0.10
    min_30m_candles: int = 240
    min_4h_candles: int = 150
    min_data_completeness_pct: float = 70.0
    ranking_min_score: float = 55.0
    tactical_signals_enabled: bool = True
    tactical_min_score: float = 72.0
    tactical_min_turnover_ratio: float = 1.35
    tactical_min_adx: float = 22.0
    tactical_time_stop_hours: float = 6.0
    tactical_risk_multiplier: float = 0.85
    adaptive_trend_score_relief: float = 3.0
    stock_default_calendar: str = "XNYS"
    # Example: ANTA_USDT:XHKG,7203_USDT:XTKS
    stock_calendar_overrides: str = ""
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
    five_minute_entry_max_atr: float = 0.65
    five_minute_entry_max_distance_pct: float = 0.012
    five_minute_entry_fallback_atr: float = 0.15
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
    market_driver_contracts: str = "BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,HYPE_USDT"
    # Portfolio sizing is risk based.  Leverage changes required margin, never
    # the amount the strategy is allowed to lose.
    risk_per_trade_pct: float = 0.01
    min_risk_per_trade_pct: float = 0.006
    max_risk_per_trade_pct: float = 0.013
    max_position_notional_equity_multiple: float = 2.5
    max_portfolio_notional_equity_multiple: float = 6.0
    available_margin_utilization_pct: float = 0.70
    minimum_viable_position_equity_multiple: float = 0.25
    max_promoted_risk_per_trade_pct: float = 0.025
    order_book_depth_range_pct: float = 0.005
    time_stop_base_hours: float = 12.0
    time_stop_trend_hours: float = 18.0
    time_stop_range_hours: float = 6.0
    time_stop_extreme_hours: float = 4.0
    time_stop_confirmation_extension_hours: float = 6.0
    time_stop_max_holding_hours: float = 72.0
    time_stop_min_progress_r: float = 0.5
    reentry_cooldown_minutes: int = 90
    estimated_round_trip_fee_pct: float = 0.0012
    minimum_net_rr: float = 1.0
    max_operational_leverage: float = 20.0
    liquidation_distance_stop_multiple: float = 2.0
    minimum_order_rr: float = 1.20
    require_max_leverage: bool = True
    # Optional absolute circuit breaker. Zero disables it; normal sizing uses
    # the account-equity percentage above.
    max_initial_stop_loss_usdt: float = 0.0
    stop_loss_buffer_atr: float = 0.9
    fallback_stop_atr: float = 2.2
    minimum_stop_atr: float = 1.6
    minimum_stop_pct: float = 0.01
    maximum_stop_atr: float = 4.5
    thesis_stop_buffer_atr: float = 0.55
    liquidity_stop_buffer_atr: float = 0.35
    coinglass_stop_confluence_atr: float = 0.75
    break_even_activation_r: float = 1.20
    structure_trail_activation_r: float = 2.0
    runner_trail_activation_r: float = 2.5
    break_even_fee_buffer_multiple: float = 1.25
    trend_trailing_atr: float = 3.0
    range_trailing_atr: float = 2.2
    high_volatility_trailing_atr: float = 3.5
    volatility_baseline_bars_30m: int = 144
    volatility_recent_bars_30m: int = 12
    volatility_shock_bars_30m: int = 4
    volatility_expansion_ratio: float = 1.50
    volatility_expansion_min_bars: int = 3
    expansion_break_even_activation_r: float = 1.50
    isolated_spike_break_even_activation_r: float = 1.60
    thesis_soft_failure_confirmations: int = 2
    reversal_signal_confirmations: int = 2
    position_trend_15m_candles: int = 288
    position_exit_5m_candles: int = 96
    position_thesis_confirmations: int = 2
    position_thesis_structure_buffer_atr: float = 0.25
    position_thesis_vwap_buffer_atr: float = 0.15
    position_thesis_min_adx: float = 20.0
    position_thesis_dmi_delta: float = 5.0
    position_thesis_ema_slope_atr: float = 0.08
    position_thesis_candidate_score: int = 6
    position_thesis_decisive_score: int = 8
    position_exit_5m_confirmations: int = 2
    position_exit_5m_buffer_atr: float = 0.15
    position_exit_5m_min_adx: float = 18.0
    position_exit_5m_candidate_score: int = 4
    position_exit_emergency_r: float = -0.75
    position_exit_recovery_max_loss_r: float = -0.20
    stop_update_min_atr: float = 0.20
    stop_update_cooldown_seconds: int = 60
    take_profit_1_pct: float = 0.30
    take_profit_2_pct: float = 0.30
    take_profit_3_pct: float = 0.25
    runner_pct: float = 0.15
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

    @field_validator(
        "risk_per_trade_pct",
        "min_risk_per_trade_pct",
        "max_risk_per_trade_pct",
        "available_margin_utilization_pct",
        "max_promoted_risk_per_trade_pct",
    )
    @classmethod
    def validate_risk_fraction(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("risk and margin fractions must be between 0 and 1")
        return value

    @field_validator("position_trend_15m_candles")
    @classmethod
    def validate_position_history(cls, value: int) -> int:
        if value < 288:
            raise ValueError(
                "POSITION_TREND_15M_CANDLES must cover at least 72 hours"
            )
        return min(value, 1000)

    @field_validator("position_exit_5m_candles")
    @classmethod
    def validate_exit_history(cls, value: int) -> int:
        if value < 96:
            raise ValueError(
                "POSITION_EXIT_5M_CANDLES must be at least 96"
            )
        return min(value, 1000)

    @field_validator(
        "position_thesis_confirmations",
        "position_exit_5m_confirmations",
    )
    @classmethod
    def validate_position_confirmations(cls, value: int) -> int:
        if not 2 <= value <= 5:
            raise ValueError(
                "position confirmation counts must be between 2 and 5"
            )
        return value

    @field_validator(
        "position_thesis_structure_buffer_atr",
        "position_thesis_vwap_buffer_atr",
        "position_thesis_ema_slope_atr",
        "position_exit_5m_buffer_atr",
    )
    @classmethod
    def validate_position_atr_threshold(cls, value: float) -> float:
        if not 0 < value <= 2:
            raise ValueError(
                "position ATR thresholds must be greater than 0 and at most 2"
            )
        return value

    @field_validator(
        "position_exit_emergency_r",
        "position_exit_recovery_max_loss_r",
    )
    @classmethod
    def validate_position_exit_r(cls, value: float) -> float:
        if not -1 < value <= 0:
            raise ValueError(
                "position exit R thresholds must be greater than -1 and at most 0"
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
