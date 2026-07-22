from typing import Any

from app.gate.normalizer import normalize_contract, normalize_snapshot
from app.scanner.liquidity import liquidity_quality


def build_universe(contracts: list[dict[str, Any]], tickers: list[dict[str, Any]], blacklist: set[str], settings: Any | None = None) -> list[dict[str, Any]]:
    ticker_map = {str(item.get("contract", "")).upper(): item for item in tickers}
    universe: list[dict[str, Any]] = []
    for raw in contracts:
        info = normalize_contract(raw)
        if info.name.upper() in blacklist or info.status != "trading":
            continue
        ticker = ticker_map.get(info.name.upper())
        if ticker:
            snapshot = normalize_snapshot(ticker)
            if settings is not None:
                allowed, _, _ = liquidity_quality(snapshot.turnover_24h_usdt, snapshot.spread_pct, settings)
                if not allowed:
                    continue
            universe.append({"info": info, "ticker": ticker, "snapshot": snapshot})
    return universe
