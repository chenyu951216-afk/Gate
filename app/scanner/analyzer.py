from typing import Any

import numpy as np
import pandas as pd

from app.indicators.atr import atr
from app.indicators.bollinger import bollinger
from app.indicators.dmi_adx import dmi_adx
from app.indicators.divergence import divergence
from app.indicators.ema import ema
from app.indicators.mfi import mfi
from app.indicators.oi import oi_features
from app.indicators.turnover import turnover_features
from app.indicators.vwap import vwap
from app.gate.trades import aggregate_taker_flow
from app.structure.breakouts import breakout_state
from app.structure.pullbacks import pullback_signal
from app.structure.ranges import range_features
from app.structure.regimes import regime_state
from app.structure.special_patterns import leverage_pattern


def _frame(candles: list[Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp": item.timestamp,
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume_contracts if item.volume_contracts is not None else np.nan,
                "turnover": item.turnover_usdt if item.turnover_usdt is not None else np.nan,
            }
            for item in candles
        ]
    ).sort_values("timestamp")


def _last(series: pd.Series) -> float | None:
    if series.empty or pd.isna(series.iloc[-1]):
        return None
    return float(series.iloc[-1])


def analyze_market(collected: dict[str, Any], min_30m: int, min_4h: int) -> dict[str, Any]:
    frame4 = _frame(collected.get("4h", []))
    frame30 = _frame(collected.get("30m", []))
    frame15 = _frame(collected.get("15m", []))
    frame5 = _frame(collected.get("5m", []))
    missing: list[str] = []
    errors: list[str] = list(collected.get("collection_errors", []))
    coinglass = collected.get("coinglass", {})
    coinglass_required = bool(collected.get("coinglass_required", False))
    available = {
        "4h": len(frame4) >= min_4h,
        "30m": len(frame30) >= min_30m,
        "15m": len(frame15) >= 50,
        "5m": len(frame5) >= 50,
        "oi": len(collected.get("oi", [])) >= 3,
        "funding": len(collected.get("funding", [])) >= 1,
        "coinglass": not coinglass_required or bool(coinglass.get("available")),
    }
    missing.extend([key for key, value in available.items() if not value])
    if coinglass_required and not available["coinglass"]:
        errors.extend(str(error) for error in coinglass.get("errors", []))
    if not available["30m"] or not available["4h"]:
        return {
            "available": available,
            "missing_data": missing,
            "errors": errors + ["insufficient_history"],
            "features": {},
            "market_state": "unavailable",
        }
    def build(frame: pd.DataFrame, include_mfi: bool) -> dict[str, Any]:
        c, h, low_series, o, v = (frame[name] for name in ("close", "high", "low", "open", "volume"))
        e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
        bb = bollinger(c)
        dmi = dmi_adx(h, low_series, c)
        atr_value = atr(h, low_series, c)
        mfi_series = mfi(h, low_series, c, v.fillna(0)) if include_mfi else pd.Series(dtype=float)
        result: dict[str, Any] = {
            "close": _last(c),
            "open": _last(o),
            "ema20": _last(e20),
            "ema50": _last(e50),
            "ema200": _last(e200),
            "boll_mid": _last(bb["mid"]),
            "boll_upper": _last(bb["upper"]),
            "boll_lower": _last(bb["lower"]),
            "boll_bandwidth": _last(bb["bandwidth"]),
            "adx": _last(dmi["adx"]),
            "plus_di": _last(dmi["plus_di"]),
            "minus_di": _last(dmi["minus_di"]),
            "atr": _last(atr_value),
            "vwap": _last(vwap(h, low_series, c, v.fillna(0))),
            "mfi": _last(mfi_series),
            # These are causal extrema from already closed candles.  The
            # execution layer uses them as structure references, never as a
            # replacement for the ranking logic.
            "recent_high": float(h.iloc[-21:-1].max()) if len(h) >= 21 else None,
            "recent_low": float(low_series.iloc[-21:-1].min()) if len(low_series) >= 21 else None,
            "mfi_series": mfi_series,
            "frame": frame,
        }
        return result

    f4 = build(frame4, False)
    f30 = build(frame30, True)
    f15 = build(frame15, False)
    f5 = build(frame5, False)
    turnover = turnover_features(frame30["turnover"].dropna())
    turnover_ratio = _last(turnover["turnover_ratio"])
    oi_frame = pd.DataFrame(collected.get("oi", []))
    oi_values: dict[str, Any] = {}
    if not oi_frame.empty and "open_interest" in oi_frame:
        oi = pd.Series(pd.to_numeric(oi_frame["open_interest"], errors="coerce").dropna().to_numpy())
        oi_values = oi_features(oi).iloc[-1].dropna().to_dict()
    funding = collected.get("funding", [])
    funding_rate = None
    if funding:
        try:
            funding_rate = float(funding[-1].get("r"))
        except (TypeError, ValueError):
            missing.append("funding")
    price_change = ((f30["close"] - f30["open"]) / f30["open"] * 100) if f30["open"] else None
    dvg = divergence(frame30["close"], f30["mfi_series"], 20)
    range_data = range_features(frame30["high"], frame30["low"], frame30["close"])
    breakout = breakout_state(frame30["high"], frame30["low"], frame30["close"], f30["atr"], turnover_ratio, f30["adx"])
    regime = regime_state(f4["ema20"], f4["ema50"], f4["ema200"], f4["adx"], None)
    pullback15 = pullback_signal(f15["close"], f15["ema20"], f15["ema50"], f15["vwap"], f15["atr"])
    oi_change = oi_values.get("oi_change_30m_pct")
    special = leverage_pattern(oi_change, price_change, f30["adx"])
    active_flow = aggregate_taker_flow(collected.get("trades", []))
    if not active_flow["available"]:
        missing.append("active_trade_aggregate")
    liquidation = {"available": False, "long": 0.0, "short": 0.0}
    if not oi_frame.empty:
        long_values = pd.to_numeric(oi_frame.get("long_liq_usd"), errors="coerce") if "long_liq_usd" in oi_frame else pd.Series(dtype=float)
        short_values = pd.to_numeric(oi_frame.get("short_liq_usd"), errors="coerce") if "short_liq_usd" in oi_frame else pd.Series(dtype=float)
        if not long_values.empty or not short_values.empty:
            liquidation = {"available": True, "long": float(long_values.fillna(0).sum()), "short": float(short_values.fillna(0).sum())}
    ticker = collected["ticker"]
    ticker_24h = {
        "change_percentage": float(ticker["change_percentage"]) if ticker.get("change_percentage") not in (None, "") else None,
        "turnover_usdt": float(ticker["volume_24h_quote"]) if ticker.get("volume_24h_quote") not in (None, "") else None,
        "bid": float(ticker["highest_bid"]) if ticker.get("highest_bid") not in (None, "") else None,
        "ask": float(ticker["lowest_ask"]) if ticker.get("lowest_ask") not in (None, "") else None,
        "mark_price": float(ticker["mark_price"]) if ticker.get("mark_price") not in (None, "") else None,
        "index_price": float(ticker["index_price"]) if ticker.get("index_price") not in (None, "") else None,
        "funding_rate": funding_rate,
    }
    basis = None
    if ticker_24h["mark_price"] and ticker_24h["index_price"]:
        basis = (ticker_24h["mark_price"] - ticker_24h["index_price"]) / ticker_24h["index_price"] * 100
    return {
        "available": available,
        "missing_data": sorted(set(missing)),
        "errors": errors,
        "features": {
            "4h": {key: value for key, value in f4.items() if key not in {"frame", "mfi_series"}},
            "30m": {key: value for key, value in f30.items() if key not in {"frame", "mfi_series"}},
            "15m": {key: value for key, value in f15.items() if key not in {"frame", "mfi_series"}},
            "5m": {key: value for key, value in f5.items() if key not in {"frame", "mfi_series"}},
            "turnover": {key: _last(value) for key, value in turnover.items()},
            "oi": oi_values,
            "funding_rate": funding_rate,
            "funding_percentile": None,
            "basis_pct": basis,
            "active_flow": active_flow,
            "liquidation": liquidation,
            "range": range_data,
            "breakout": breakout,
            "pullback15": pullback15,
            "divergence": dvg,
            "special_pattern": special,
            "price_change_pct": price_change,
            "ticker": ticker_24h,
            "coinglass": coinglass,
        },
        "market_state": regime,
        "signal_state": breakout["state"],
    }
