def leverage_pattern(oi_change_pct: float | None, price_change_pct: float | None, adx: float | None) -> str | None:
    if oi_change_pct is None or price_change_pct is None:
        return None
    if oi_change_pct > 4 and abs(price_change_pct) < 0.5 and (adx or 0) < 23:
        return "leverage_build_up"
    if oi_change_pct > 4 and price_change_pct > 1.5:
        return "short_squeeze"
    if oi_change_pct > 4 and price_change_pct < -1.5:
        return "long_squeeze"
    return None

