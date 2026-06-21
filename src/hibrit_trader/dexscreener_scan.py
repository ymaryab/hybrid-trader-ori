"""Dexscreener trending — token boosts + pair metrikleri ($0 API).

Dexscreener UI'daki Trending 6H listesine yakın: boost skoru, MCAP, hacim, txns, yaş.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from hibrit_trader.config import API, DEFAULT_SCAN_CHAINS, restrict_chains
from hibrit_trader.scanner import Pair, _f, _parse_pool_created_at

log = logging.getLogger(__name__)

DS_CHAIN = {"solana", "base", "bsc", "arbitrum"}


def _txns_total(txns: dict | None, window: str) -> int:
    if not txns:
        return 0
    block = txns.get(window) or {}
    return int(_f(block.get("buys")) + _f(block.get("sells")))


def pair_from_dexscreener(item: dict, *, boost_score: int = 0) -> Optional[Pair]:
    try:
        chain = str(item.get("chainId") or "")
        if chain not in DS_CHAIN:
            return None
        base = item.get("baseToken") or {}
        token_address = str(base.get("address") or "")
        if not token_address:
            return None
        liq = item.get("liquidity") or {}
        vol = item.get("volume") or {}
        chg = item.get("priceChange") or {}
        sym = str(base.get("symbol") or "?")
        quote = (item.get("quoteToken") or {}).get("symbol") or "?"
        name = f"{sym} / {quote}"
        created_ms = item.get("pairCreatedAt")
        created_ts = float(created_ms) / 1000.0 if created_ms else None
        return Pair(
            chain=chain,
            dex=str(item.get("dexId") or "dexscreener"),
            pool_address=str(item.get("pairAddress") or ""),
            token_address=token_address,
            name=name,
            price_usd=_f(item.get("priceUsd")),
            liquidity_usd=_f(liq.get("usd")),
            vol_m5=_f(vol.get("m5")),
            vol_h1=_f(vol.get("h1")),
            vol_h24=_f(vol.get("h24")),
            chg_m5=_f(chg.get("m5")),
            chg_h1=_f(chg.get("h1") or chg.get("h6")),
            chg_h24=_f(chg.get("h24") or chg.get("h6")),
            txns_h1=_txns_total(item.get("txns"), "h1"),
            pool_created_at=created_ts,
            market_cap_usd=_f(item.get("marketCap") or item.get("fdv")),
            txns_m5=_txns_total(item.get("txns"), "m5"),
            txns_h24=_txns_total(item.get("txns"), "h24"),
            boost_score=boost_score,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _best_pair_per_token(items: list[dict]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for item in items:
        base = item.get("baseToken") or {}
        addr = str(base.get("address") or "")
        if not addr:
            continue
        liq = _f((item.get("liquidity") or {}).get("usd"))
        prev = best.get(addr)
        if prev is None or liq > _f((prev.get("liquidity") or {}).get("usd")):
            best[addr] = item
    return best


def fetch_dexscreener_trending(
    client: httpx.Client,
    chains: tuple[str, ...] = DEFAULT_SCAN_CHAINS,
    boost_limit: int = 30,
) -> list[Pair]:
    """Token boost top list → en likit havuz → Pair."""
    chains = restrict_chains(chains)  # SOLANA_ONLY açıksa EVM token istekleri hiç yapılmaz
    try:
        r = client.get(f"{API['dexscreener']}/token-boosts/top/v1", timeout=15)
        r.raise_for_status()
        boosts = r.json() or []
    except httpx.HTTPError as e:
        log.warning("Dexscreener boosts erişilemedi: %s", e)
        return []

    by_chain: dict[str, list[tuple[str, int]]] = {}
    for row in boosts[:boost_limit]:
        chain = str(row.get("chainId") or "")
        if chain not in chains or chain not in DS_CHAIN:
            continue
        addr = str(row.get("tokenAddress") or "")
        if not addr:
            continue
        boost = int(_f(row.get("totalAmount")))
        by_chain.setdefault(chain, []).append((addr, boost))

    out: list[Pair] = []
    for chain, entries in by_chain.items():
        boost_map = {a: b for a, b in entries}
        addrs = [a for a, _ in entries]
        for i in range(0, len(addrs), 30):
            chunk = addrs[i : i + 30]
            url = f"{API['dexscreener']}/tokens/v1/{chain}/" + ",".join(chunk)
            try:
                resp = client.get(url, timeout=20)
                resp.raise_for_status()
                raw = resp.json() or []
            except httpx.HTTPError as e:
                log.warning("Dexscreener tokens %s: %s", chain, e)
                continue
            if not isinstance(raw, list):
                continue
            by_token: dict[str, list[dict]] = {}
            for item in raw:
                base = item.get("baseToken") or {}
                addr = str(base.get("address") or "")
                if addr:
                    by_token.setdefault(addr, []).append(item)
            from hibrit_trader.early_launch import select_entry_pool

            for addr, pool_items in by_token.items():
                p = select_entry_pool(pool_items, boost_score=boost_map.get(addr, 0))
                if p and p.liquidity_usd > 0:
                    out.append(p)
            time.sleep(0.15)
    return out
