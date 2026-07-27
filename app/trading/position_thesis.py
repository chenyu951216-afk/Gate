from __future__ import annotations

from math import isfinite
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


def assess_closed_15m_thesis(
    side: str,
    context: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    """Assess whether an open-position trend has genuinely disappeared.

    Every input is derived from a completed 15m candle. No live mark, 5m
    candle, holding duration or scanner rank is allowed to create a thesis
    failure. Price structure, EMA state, DMI/ADX, VWAP and volume-confirmed MFI
    must agree before the position advances from HEALTHY to WATCH/CONFIRMED.
    """
    integrity = context.get("integrity15", {})
    if isinstance(integrity, dict) and integrity.get("complete") is False:
        return {
            "available": False,
            "state": "DATA_INVALID",
            "candidate": False,
            "decisive": False,
            "healthy": False,
            "score": 0,
            "evidence": ["15m_data_integrity"],
            "observation_key": str(context.get("closed_timestamp15") or ""),
        }
    close = _number(context.get("last_close15"))
    atr15 = _number(context.get("atr15"))
    ema20 = _number(context.get("ema2015"))
    ema50 = _number(context.get("ema5015"))
    vwap15 = _number(context.get("vwap15"))
    recent_low = _number(context.get("recent_low15"))
    recent_high = _number(context.get("recent_high15"))
    plus_di = _number(context.get("plus_di15"))
    minus_di = _number(context.get("minus_di15"))
    adx = _number(context.get("adx15"))
    mfi = _number(context.get("mfi15"), 50.0)
    turnover_ratio = _number(context.get("turnover_ratio15"), 1.0)
    ema20_slope_atr = _number(context.get("ema20_slope_atr15"))
    below_count = int(_number(context.get("below_trend_count15")))
    above_count = int(_number(context.get("above_trend_count15")))
    if (
        side not in {"long", "short"}
        or min(close, atr15, ema20, ema50) <= 0
    ):
        return {
            "available": False,
            "state": "DATA_INVALID",
            "candidate": False,
            "decisive": False,
            "healthy": False,
            "score": 0,
            "evidence": ["15m_required_metric_missing"],
            "observation_key": str(context.get("closed_timestamp15") or ""),
        }

    structure_buffer = float(
        getattr(settings, "position_thesis_structure_buffer_atr", 0.25)
    ) * atr15
    vwap_buffer = float(
        getattr(settings, "position_thesis_vwap_buffer_atr", 0.15)
    ) * atr15
    min_adx = float(getattr(settings, "position_thesis_min_adx", 20.0))
    dmi_delta = float(getattr(settings, "position_thesis_dmi_delta", 5.0))
    slope_threshold = float(
        getattr(settings, "position_thesis_ema_slope_atr", 0.08)
    )
    if side == "long":
        structure_break = (
            recent_low > 0 and close < recent_low - structure_buffer
        )
        ema_reversal = close < ema20 < ema50
        ema_slope_reversal = ema20_slope_atr <= -slope_threshold
        dmi_reversal = (
            adx >= min_adx and minus_di - plus_di >= dmi_delta
        )
        vwap_loss = vwap15 > 0 and close < vwap15 - vwap_buffer
        momentum_reversal = mfi <= 35 and turnover_ratio >= 1.10
        adverse_persistence = below_count >= 2
        supportive_dmi = plus_di >= minus_di or adx < min_adx
        supportive_price = close >= ema20 or (
            vwap15 > 0 and close >= vwap15
        )
    else:
        structure_break = (
            recent_high > 0 and close > recent_high + structure_buffer
        )
        ema_reversal = close > ema20 > ema50
        ema_slope_reversal = ema20_slope_atr >= slope_threshold
        dmi_reversal = (
            adx >= min_adx and plus_di - minus_di >= dmi_delta
        )
        vwap_loss = vwap15 > 0 and close > vwap15 + vwap_buffer
        momentum_reversal = mfi >= 65 and turnover_ratio >= 1.10
        adverse_persistence = above_count >= 2
        supportive_dmi = minus_di >= plus_di or adx < min_adx
        supportive_price = close <= ema20 or (
            vwap15 > 0 and close <= vwap15
        )

    evidence_values = {
        "15m_structure_break": (structure_break, 3),
        "15m_ema_stack_reversed": (ema_reversal, 2),
        "15m_ema_slope_reversed": (ema_slope_reversal, 1),
        "15m_dmi_adx_reversed": (dmi_reversal, 2),
        "15m_vwap_lost": (vwap_loss, 1),
        "15m_volume_mfi_reversal": (momentum_reversal, 1),
        "15m_adverse_close_persistence": (adverse_persistence, 1),
    }
    evidence = [
        name for name, (present, _weight) in evidence_values.items() if present
    ]
    score = sum(
        weight for present, weight in evidence_values.values() if present
    )
    decisive_score = int(
        getattr(settings, "position_thesis_decisive_score", 8)
    )
    candidate_score = int(
        getattr(settings, "position_thesis_candidate_score", 6)
    )
    decisive = bool(
        score >= decisive_score
        and structure_break
        and dmi_reversal
        and (ema_reversal or momentum_reversal)
    )
    candidate = bool(
        decisive
        or (
            score >= candidate_score
            and len(evidence) >= 3
            and (structure_break or ema_reversal)
            and (dmi_reversal or adverse_persistence)
        )
    )
    healthy = bool(
        not candidate
        and score <= 2
        and supportive_price
        and supportive_dmi
        and not structure_break
    )
    state = (
        "CONFIRMED"
        if decisive
        else "WATCH"
        if candidate
        else "HEALTHY"
        if healthy
        else "NEUTRAL"
    )
    return {
        "available": True,
        "state": state,
        "candidate": candidate,
        "decisive": decisive,
        "healthy": healthy,
        "score": score,
        "evidence": evidence,
        "observation_key": str(
            context.get("closed_timestamp15") or f"{close:.12g}"
        ),
        "metrics": {
            "close": close,
            "atr": atr15,
            "adx": adx,
            "mfi": mfi,
            "turnover_ratio": turnover_ratio,
            "ema20_slope_atr": ema20_slope_atr,
        },
    }


def assess_closed_5m_exit(
    *,
    side: str,
    context: dict[str, Any],
    live_price: float,
    current_r: float,
    recovery_target: float,
    settings: Any,
) -> dict[str, Any]:
    """Choose execution timing after the 15m thesis has already failed."""
    integrity = context.get("integrity5", {})
    if isinstance(integrity, dict) and integrity.get("complete") is False:
        return {
            "available": False,
            "candidate": False,
            "decisive": False,
            "target_reached": False,
            "emergency": False,
            "score": 0,
            "evidence": ["5m_data_integrity"],
            "observation_key": str(context.get("closed_timestamp5") or ""),
        }
    close = _number(context.get("last_close5"))
    atr5 = _number(context.get("atr5"))
    ema20 = _number(context.get("ema205"))
    ema50 = _number(context.get("ema505"))
    vwap5 = _number(context.get("vwap5"))
    recent_low = _number(context.get("recent_low5"))
    recent_high = _number(context.get("recent_high5"))
    plus_di = _number(context.get("plus_di5"))
    minus_di = _number(context.get("minus_di5"))
    adx = _number(context.get("adx5"))
    if (
        side not in {"long", "short"}
        or min(close, atr5, ema20, ema50, live_price) <= 0
    ):
        return {
            "available": False,
            "candidate": False,
            "decisive": False,
            "target_reached": False,
            "emergency": False,
            "score": 0,
            "evidence": ["5m_required_metric_missing"],
            "observation_key": str(context.get("closed_timestamp5") or ""),
        }
    buffer = float(getattr(settings, "position_exit_5m_buffer_atr", 0.15))
    min_adx = float(getattr(settings, "position_exit_5m_min_adx", 18.0))
    if side == "long":
        structure = recent_low > 0 and close < recent_low - buffer * atr5
        ema = close < ema20 < ema50
        dmi = adx >= min_adx and minus_di > plus_di
        vwap_loss = vwap5 > 0 and close < vwap5
        target_reached = recovery_target > 0 and live_price >= recovery_target
        recovery = close > ema20 and plus_di >= minus_di
    else:
        structure = recent_high > 0 and close > recent_high + buffer * atr5
        ema = close > ema20 > ema50
        dmi = adx >= min_adx and plus_di > minus_di
        vwap_loss = vwap5 > 0 and close > vwap5
        target_reached = recovery_target > 0 and live_price <= recovery_target
        recovery = close < ema20 and minus_di >= plus_di
    evidence_values = {
        "5m_structure_continuation": (structure, 2),
        "5m_ema_continuation": (ema, 2),
        "5m_dmi_adx_continuation": (dmi, 1),
        "5m_vwap_continuation": (vwap_loss, 1),
    }
    evidence = [
        name for name, (present, _weight) in evidence_values.items() if present
    ]
    score = sum(
        weight for present, weight in evidence_values.values() if present
    )
    decisive = bool(structure and ema and dmi)
    candidate = bool(
        score >= int(getattr(settings, "position_exit_5m_candidate_score", 4))
        and (structure or ema)
        and not recovery
    )
    emergency = current_r <= float(
        getattr(settings, "position_exit_emergency_r", -0.75)
    )
    return {
        "available": True,
        "candidate": candidate,
        "decisive": decisive,
        "target_reached": target_reached,
        "emergency": emergency,
        "recovery": recovery,
        "score": score,
        "evidence": evidence,
        "observation_key": str(
            context.get("closed_timestamp5") or f"{close:.12g}"
        ),
    }
