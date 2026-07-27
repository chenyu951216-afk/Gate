"""Bitget USDT-M futures adapter.

The rest of the application deliberately keeps the existing internal market
schema (for example ``BTC_USDT``).  This package translates that schema to the
official Bitget v2 API (for example ``BTCUSDT``), so scanner and risk logic do
not change when the execution venue changes.
"""

from app.bitget.client import BitgetClient
from app.bitget.rest_client import BitgetRestClient

__all__ = ["BitgetClient", "BitgetRestClient"]
