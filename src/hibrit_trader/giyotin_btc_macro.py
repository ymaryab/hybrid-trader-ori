"""Giyotin v3 — BTC m15 makro filtresi (Delikanlı upgrade 1)."""

from __future__ import annotations

import os
import time

import httpx

_CACHE_TTL_SEC = 30.0
_btc_m15_cache: dict[str, float | None] = {"ts": 0.0, "m15": None}


def btc_m15_block_threshold() -> float:
    return float(os.getenv("GIYOTIN_BTC_M15_BLOCK", "-1.5"))


def _btc_m15_from_binance(client: httpx.Client) -> float | None:
    r = client.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "15m", "limit": 2},
        timeout=10,
    )
    r.raise_for_status()
    rows = r.json()
    if len(rows) < 2:
        return None
    prev_close = float(rows[-2][4])
    last_close = float(rows[-1][4])
    if prev_close <= 0:
        return None
    return (last_close - prev_close) / prev_close * 100.0


def fetch_btc_m15_pct(*, client: httpx.Client | None = None) -> float | None:
    global _btc_m15_cache
    now = time.time()
    if now - float(_btc_m15_cache["ts"]) < _CACHE_TTL_SEC and _btc_m15_cache["m15"] is not None:
        return _btc_m15_cache["m15"]
    try:
        if client is not None:
            m15 = _btc_m15_from_binance(client)
        else:
            with httpx.Client() as c:
                m15 = _btc_m15_from_binance(c)
        _btc_m15_cache = {"ts": now, "m15": m15}
        return m15
    except Exception:
        return _btc_m15_cache.get("m15")


def btc_macro_blocks_entry(m15: float | None = None) -> tuple[bool, str]:
    """True = giriş yasak (BTC dump)."""
    val = m15 if m15 is not None else fetch_btc_m15_pct()
    if val is None:
        return False, "btc m15 okunamadı (fail-open)"
    floor = btc_m15_block_threshold()
    if val < floor:
        return True, f"btc m15 %{val:.2f} < %{floor:.1f} makro blok"
    return False, f"btc m15 %{val:.2f} ok"
