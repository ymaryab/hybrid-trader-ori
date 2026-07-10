"""Canlı simülasyon — paper işlem sanal, fiyat/teklif gerçek DEX kaynağından.

- Havuz fiyatı: GeckoTerminal tek havuz API (on-chain pool snapshot)
- Solana çıkış teklifi: Jupiter quote (imza yok, yalnız okuma)
- EVM çıkış teklifi: havuz fiyatı × miktar − slippage modeli (0x quote ileride)
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from hibrit_trader.config import API, Settings
from hibrit_trader.jupiter import LAMPORTS_PER_SOL, SOL_MINT, fetch_sol_price_usd, get_quote
from hibrit_trader.paper import PaperBroker, Position

log = logging.getLogger(__name__)

_CACHE_TTL_SEC = 30.0
_pool_cache: dict[str, tuple[float, float]] = {}
_decimals_cache: dict[str, tuple[int, float]] = {}


def _cache_get(cache: dict[str, tuple[float, float]], key: str) -> Optional[float]:
    hit = cache.get(key)
    if not hit:
        return None
    value, ts = hit
    if time.monotonic() - ts > _CACHE_TTL_SEC:
        return None
    return value


def _cache_set(cache: dict[str, tuple[float, float]], key: str, value: float) -> None:
    cache[key] = (value, time.monotonic())


def fetch_pool_price(client: httpx.Client, chain: str, pool_address: str) -> Optional[float]:
    """GeckoTerminal tek havuz — güncel base_token_price_usd."""
    key = f"{chain}:{pool_address}"
    cached = _cache_get(_pool_cache, key)
    if cached is not None:
        return cached
    url = f"{API['geckoterminal']}/networks/{chain}/pools/{pool_address}"
    try:
        resp = client.get(url, headers={"accept": "application/json"}, timeout=12)
        resp.raise_for_status()
        price = float(resp.json()["data"]["attributes"]["base_token_price_usd"])
        if price > 0:
            _cache_set(_pool_cache, key, price)
        return price if price > 0 else None
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        log.debug("Havuz fiyatı alınamadı %s: %s", key, exc)
        return None


_pool_liq_cache: dict[str, tuple[float, float]] = {}


def fetch_pool_snapshot(
    client: httpx.Client, chain: str, pool_address: str
) -> tuple[Optional[float], Optional[float]]:
    """GeckoTerminal tek havuz — (base_token_price_usd, reserve_in_usd).

    Ayni GET zaten iki alani da tasiyor; likidite teyidi (price_sanity) icin
    ek istek gerekmez. Fiyat cache'i fetch_pool_price ile ortaktir.
    """
    key = f"{chain}:{pool_address}"
    price_c = _cache_get(_pool_cache, key)
    liq_c = _cache_get(_pool_liq_cache, key)
    if price_c is not None and liq_c is not None:
        return price_c, liq_c
    url = f"{API['geckoterminal']}/networks/{chain}/pools/{pool_address}"
    try:
        resp = client.get(url, headers={"accept": "application/json"}, timeout=12)
        resp.raise_for_status()
        attrs = resp.json()["data"]["attributes"]
        price = float(attrs["base_token_price_usd"])
        raw_liq = attrs.get("reserve_in_usd")
        liq = float(raw_liq) if raw_liq not in (None, "") else None
        if price > 0:
            _cache_set(_pool_cache, key, price)
        if liq is not None and liq > 0:
            _cache_set(_pool_liq_cache, key, liq)
        return (price if price > 0 else None), liq
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        log.debug("Havuz snapshot alınamadı %s: %s", key, exc)
        return None, None


def fetch_token_decimals(client: httpx.Client, chain: str, token_address: str) -> int:
    """Token decimals — GeckoTerminal; bilinmezse Solana 6, EVM 18."""
    key = f"{chain}:{token_address}"
    hit = _decimals_cache.get(key)
    if hit and time.monotonic() - hit[1] <= _CACHE_TTL_SEC:
        return hit[0]
    default = 6 if chain == "solana" else 18
    url = f"{API['geckoterminal']}/networks/{chain}/tokens/{token_address}"
    try:
        resp = client.get(url, headers={"accept": "application/json"}, timeout=12)
        resp.raise_for_status()
        dec = int(resp.json()["data"]["attributes"].get("decimals") or default)
        _decimals_cache[key] = (dec, time.monotonic())
        return dec
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return default


def jupiter_exit_quote(
    client: httpx.Client,
    token_mint: str,
    amount_token: float,
    decimals: int,
    slippage_bps: int,
) -> Optional[dict]:
    """Token → SOL Jupiter quote; USD karşılığı döner."""
    if amount_token <= 0:
        return None
    amount_raw = int(amount_token * (10**decimals))
    if amount_raw <= 0:
        return None
    try:
        quote = get_quote(client, token_mint, SOL_MINT, amount_raw, slippage_bps)
        out_lamports = int(quote["outAmount"])
        sol_price = fetch_sol_price_usd(client)
        out_usd = out_lamports / LAMPORTS_PER_SOL * sol_price
        impact = float(quote.get("priceImpactPct") or 0)
        return {
            "proceeds_usd": round(out_usd, 4),
            "proceeds_sol": round(out_lamports / LAMPORTS_PER_SOL, 6),
            "price_impact_pct": round(impact, 3),
            "source": "jupiter_v6_sol",
        }
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.debug("Jupiter çıkış teklifi yok %s: %s", token_mint[:8], exc)
        return None


def _pool_exit_estimate(pos: Position, pool_price: float, liquidity_usd: float) -> dict:
    """Havuz fiyatı + slippage modeli — Jupiter/0x quote yoksa yedek."""
    slip = min(pos.cost_usd / max(liquidity_usd, 1.0), 0.05)
    effective = pool_price * (1 - slip)
    proceeds = pos.amount_token * effective
    return {
        "proceeds_usd": round(proceeds, 4),
        "price_impact_pct": round(slip * 100, 3),
        "source": "pool_slippage_model",
    }


def enrich_position(
    pos: Position,
    fallback_price: float,
    fallback_liquidity: float,
    client: httpx.Client,
    settings: Settings,
) -> dict:
    """Panel için gerçek-zamanlı pozisyon snapshot."""
    live_price = fetch_pool_price(client, pos.chain, pos.pool_address)
    price = live_price if live_price is not None else fallback_price
    price_source = "geckoterminal_pool" if live_price is not None else "scan_tick"

    exit_quote: Optional[dict] = None
    if settings.paper_live_quotes:
        if pos.chain == "solana":
            dec = fetch_token_decimals(client, pos.chain, pos.token_address)
            exit_quote = jupiter_exit_quote(
                client, pos.token_address, pos.amount_token, dec, settings.max_slippage_bps
            )
        if exit_quote is None:
            exit_quote = _pool_exit_estimate(pos, price, fallback_liquidity)
    elif settings.mode == "paper":
        exit_quote = _pool_exit_estimate(pos, price, fallback_liquidity)

    mark_pnl = PaperBroker.unrealized_pnl(pos, price)
    quote_pnl: Optional[float] = None
    if exit_quote:
        quote_pnl = round(exit_quote["proceeds_usd"] - pos.cost_usd, 2)

    return {
        "pair": pos.pair_name,
        "chain": pos.chain,
        "token_address": pos.token_address,
        "pool_address": pos.pool_address,
        "entry_price": round(pos.entry_price, 8),
        "current_price": round(price, 8),
        "price_source": price_source,
        "cost_usd": round(pos.cost_usd, 2),
        "amount_token": round(pos.amount_token, 4),
        "unrealized_pnl": round(mark_pnl, 2),
        "exit_quote_usd": exit_quote["proceeds_usd"] if exit_quote else None,
        "exit_quote_pnl": quote_pnl,
        "price_impact_pct": exit_quote["price_impact_pct"] if exit_quote else None,
        "quote_source": exit_quote["source"] if exit_quote else None,
        "entry_score": pos.entry_score,
        "opened_at": pos.opened_at,
        "trade_type": "paper",
        "prices_live": True,
    }


def live_sim_summary(settings: Settings) -> dict:
    return {
        "enabled": settings.paper_live_quotes or settings.mode == "paper",
        "trade_execution": settings.mode,
        "price_feed": "geckoterminal",
        "exit_quotes": (
            "jupiter_v6+pool_fallback"
            if settings.paper_live_quotes
            else "pool_slippage_model"
        ),
        "description_tr": (
            "İşlemler sanal (paper); güncel fiyat GeckoTerminal havuz API'sinden (gerçek DEX). "
            "Solana çıkış teklifi: önce Jupiter quote, yoksa havuz+slippage modeli. Blockchain imzası yok."
            if settings.paper_live_quotes
            else "İşlemler sanal; fiyat havuz API, çıkış teklifi slippage modeli."
        ),
    }
