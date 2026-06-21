"""Pump.fun erken keşif — DexScreener üzerinden pump suffix + pumpswap havuzları.

pump.fun API Cloudflare bloklu; $0 alternatif:
- token-profiles/latest + boosts/latest → mint ...pump
- pumpswap/meteora havuz resolve (early_launch.select_entry_pool)
- İlk görülen mint'lere discovery_source=pump_fun
"""

from __future__ import annotations

import logging
import os
import time
from typing import Iterable

import httpx

from hibrit_trader.config import API, DEFAULT_SCAN_CHAINS, restrict_chains
from hibrit_trader.dexscreener_scan import pair_from_dexscreener
from hibrit_trader.early_launch import genesis_score, select_entry_pool
from hibrit_trader.scanner import Pair, _f

log = logging.getLogger(__name__)

_SEEN_MINTS: set[str] = set()


def pump_fun_feed_enabled() -> bool:
    return os.getenv("PUMP_FUN_FEED", "1") != "0"


def is_pump_fun_mint(mint: str) -> bool:
    return mint.endswith("pump")


def pump_fun_genesis_bonus() -> float:
    return float(os.getenv("PUMP_FUN_GENESIS_BONUS", "15"))


def _collect_mint_candidates(rows: Iterable[dict], chains: tuple[str, ...]) -> dict[str, int]:
    """mint → boost skoru."""
    out: dict[str, int] = {}
    for row in rows:
        chain = str(row.get("chainId") or "")
        if chain not in chains:
            continue
        mint = str(row.get("tokenAddress") or "")
        if not mint or not is_pump_fun_mint(mint):
            continue
        boost = int(_f(row.get("totalAmount")))
        out[mint] = max(out.get(mint, 0), boost)
    return out


def _fetch_rows(client: httpx.Client, path: str) -> list[dict]:
    try:
        resp = client.get(f"{API['dexscreener']}{path}", timeout=15)
        resp.raise_for_status()
        data = resp.json() or []
        return data if isinstance(data, list) else []
    except httpx.HTTPError as exc:
        log.warning("pump_fun_feed %s: %s", path, exc)
        return []


def _resolve_mints(client: httpx.Client, chain: str, mints: list[str]) -> list[Pair]:
    out: list[Pair] = []
    for i in range(0, len(mints), 30):
        chunk = mints[i : i + 30]
        url = f"{API['dexscreener']}/tokens/v1/{chain}/" + ",".join(chunk)
        try:
            resp = client.get(url, timeout=20)
            resp.raise_for_status()
            raw = resp.json() or []
        except httpx.HTTPError as exc:
            log.warning("pump_fun_feed tokens %s: %s", chain, exc)
            continue
        if not isinstance(raw, list):
            continue
        by_mint: dict[str, list[dict]] = {}
        for item in raw:
            base = item.get("baseToken") or {}
            addr = str(base.get("address") or "")
            if addr:
                by_mint.setdefault(addr, []).append(item)
        for mint, pool_items in by_mint.items():
            boost = 0
            p = select_entry_pool(pool_items, boost_score=boost)
            if not p:
                continue
            is_new = mint not in _SEEN_MINTS
            _SEEN_MINTS.add(mint)
            out.append(
                Pair(
                    **{
                        **p.__dict__,
                        "discovery_source": "pump_fun",
                        "boost_score": max(p.boost_score, boost),
                    }
                )
            )
            if is_new:
                log.info("pump_fun_feed: yeni mint %s…%s", mint[:6], mint[-4:])
        time.sleep(0.12)
    return out


def fetch_pump_fun_pairs(
    client: httpx.Client,
    chains: tuple[str, ...] = DEFAULT_SCAN_CHAINS,
) -> list[Pair]:
    """Pump suffix mint'ler — genesis havuzuna ek aday."""
    if not pump_fun_feed_enabled():
        return []

    chains = restrict_chains(chains)  # tutarlılık (bu modül zaten yalnız solana mint çözer)
    mint_boost: dict[str, int] = {}
    for path in (
        "/token-profiles/latest/v1",
        "/token-profiles/recent-updates/v1",
        "/token-boosts/latest/v1",
    ):
        for mint, boost in _collect_mint_candidates(_fetch_rows(client, path), chains).items():
            mint_boost[mint] = max(mint_boost.get(mint, 0), boost)

    if not mint_boost:
        return []

    by_chain: dict[str, list[str]] = {c: [] for c in chains}
    for mint in mint_boost:
        by_chain.setdefault("solana", []).append(mint)

    out: list[Pair] = []
    for chain, mints in by_chain.items():
        if not mints:
            continue
        pairs = _resolve_mints(client, chain, sorted(set(mints)))
        for p in pairs:
            bs = mint_boost.get(p.token_address, 0)
            if bs > p.boost_score:
                p = Pair(**{**p.__dict__, "boost_score": bs})
            if genesis_score(p) > 0 or p.liquidity_usd >= float(
                os.getenv("GENESIS_MIN_LIQ_USD", "8000")
            ):
                out.append(p)

    log.info("pump_fun_feed: %d aday (%d mint izleniyor)", len(out), len(_SEEN_MINTS))
    return out


def reset_seen_for_tests() -> None:
    _SEEN_MINTS.clear()
