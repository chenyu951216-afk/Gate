def new_diagnostics(requested_time: str, aligned_time: str) -> dict:
    return {
        "requested_time": requested_time,
        "aligned_time": aligned_time,
        "universe_total": 0,
        "contracts_available": 0,
        "contracts_excluded": 0,
        "contracts_ranked": 0,
        "api_errors": [],
        "missing_candle_data": [],
        "missing_oi": [],
        "missing_funding": [],
        "missing_active_buy_sell": [],
        "missing_liquidation": [],
        "stale_data": [],
        "indicator_failures": [],
        "time_alignment_errors": [],
        "unit_conversion_failures": [],
        "insufficient_warmup": [],
        "data_completeness_failures": [],
        "ranking_suppression_reasons": [],
        "reliable": False,
    }

