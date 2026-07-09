"""GeckoTerminal trending tarayıcı — 4 ağda trend havuzları çeker, Pair'e normalize eder.

Ücretsiz, key'siz; rate limit ~30 istek/dk → tarama döngüsü 30-60 sn yeterli.

PAYLASIMLI TARAMA (09 Tem gece): scan_all_cached ile dongu basina TEK tarama.
Ilk isteyen motor HTTP'yi yapar, digerleri ayni sonucu paylasir (faz adaleti:
tum motorlar ayni aday listesini gorur, dongu sonundaki motor 429 kurbani
olmaz). 429'da kisa backoff + tek retry; tarama bos/hatali donerse son iyi
sonuc SCAN_STALE_MAX_SEC'e kadar telafi olarak kullanilir.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from hibrit_trader.config import API, DEFAULT_SCAN_CHAINS, restrict_chains, solana_only_enabled

log = logging.getLogger(__name__)

BACKOFF_429_SEC = float(os.getenv("SCAN_429_BACKOFF_SEC", "2.5"))
SCAN_CACHE_SEC = float(os.getenv("SCAN_CACHE_SEC", "20"))
SCAN_STALE_MAX_SEC = float(os.getenv("SCAN_STALE_MAX_SEC", "90"))


@dataclass
class Pair:
    chain: str
    dex: str
    pool_address: str
    token_address: str
    name: str
    price_usd: float
    liquidity_usd: float
    vol_m5: float
    vol_h1: float
    vol_h24: float
    chg_m5: float
    chg_h1: float
    chg_h24: float
    txns_h1: int
    pool_created_at: float | None = None
    market_cap_usd: float = 0.0
    txns_m5: int = 0
    txns_h24: int = 0
    boost_score: int = 0
    discovery_source: str = ""


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_pool_created_at(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def parse_pool(chain: str, item: dict) -> Optional[Pair]:
    """GeckoTerminal trending_pools öğesini Pair'e çevirir; bozuk kayıt → None."""
    try:
        attr = item["attributes"]
        rel = item["relationships"]
        # id formatı: "<network>_<adres>"
        pool_address = item["id"].split("_", 1)[1]
        token_address = rel["base_token"]["data"]["id"].split("_", 1)[1]
        dex = rel["dex"]["data"]["id"]
        vol = attr.get("volume_usd") or {}
        chg = attr.get("price_change_percentage") or {}
        tx_h1 = (attr.get("transactions") or {}).get("h1") or {}
        return Pair(
            chain=chain,
            dex=dex,
            pool_address=pool_address,
            token_address=token_address,
            name=attr.get("name") or "?",
            price_usd=_f(attr.get("base_token_price_usd")),
            liquidity_usd=_f(attr.get("reserve_in_usd")),
            vol_m5=_f(vol.get("m5")),
            vol_h1=_f(vol.get("h1")),
            vol_h24=_f(vol.get("h24")),
            chg_m5=_f(chg.get("m5")),
            chg_h1=_f(chg.get("h1")),
            chg_h24=_f(chg.get("h24")),
            txns_h1=int(_f(tx_h1.get("buys")) + _f(tx_h1.get("sells"))),
            pool_created_at=_parse_pool_created_at(attr.get("pool_created_at")),
        )
    except (KeyError, IndexError, TypeError):
        log.warning("Bozuk havuz kaydı atlandı: %s", item.get("id"))
        return None


def fetch_trending(client: httpx.Client, chain: str) -> list[Pair]:
    """Tek ağın trend havuzlarını çeker. SOLANA_ONLY açıkken EVM ağı erken döner."""
    if solana_only_enabled() and chain != "solana":
        return []
    url = f"{API['geckoterminal']}/networks/{chain}/trending_pools"
    resp = client.get(url, headers={"accept": "application/json"}, timeout=15)
    if resp.status_code == 429:
        log.warning("%s trending 429, %.1fs backoff sonrasi tek tekrar", chain, BACKOFF_429_SEC)
        time.sleep(BACKOFF_429_SEC)
        resp = client.get(url, headers={"accept": "application/json"}, timeout=15)
    resp.raise_for_status()
    items = resp.json().get("data", [])
    pairs = [parse_pool(chain, item) for item in items]
    return [p for p in pairs if p is not None]


def merge_pairs(*sources: list[Pair]) -> list[Pair]:
    """Token adresine göre birleştir — Dexscreener metrikleri Gecko kaydına yazılır."""
    out: dict[str, Pair] = {}
    for pairs in sources:
        for p in pairs:
            prev = out.get(p.token_address)
            if prev is None:
                out[p.token_address] = p
                continue
            out[p.token_address] = Pair(
                chain=prev.chain,
                dex=p.dex if p.dex != "dexscreener" else prev.dex,
                pool_address=p.pool_address or prev.pool_address,
                token_address=p.token_address,
                name=p.name if p.boost_score else prev.name,
                price_usd=p.price_usd or prev.price_usd,
                liquidity_usd=max(p.liquidity_usd, prev.liquidity_usd),
                vol_m5=p.vol_m5 or prev.vol_m5,
                vol_h1=p.vol_h1 or prev.vol_h1,
                vol_h24=p.vol_h24 or prev.vol_h24,
                chg_m5=p.chg_m5 if p.boost_score else prev.chg_m5,
                chg_h1=p.chg_h1 if abs(p.chg_h1) > abs(prev.chg_h1) else prev.chg_h1,
                chg_h24=p.chg_h24 if abs(p.chg_h24) > abs(prev.chg_h24) else prev.chg_h24,
                txns_h1=max(p.txns_h1, prev.txns_h1),
                pool_created_at=p.pool_created_at or prev.pool_created_at,
                market_cap_usd=p.market_cap_usd or prev.market_cap_usd,
                txns_m5=p.txns_m5 or prev.txns_m5,
                txns_h24=p.txns_h24 or prev.txns_h24,
                boost_score=max(p.boost_score, prev.boost_score),
                discovery_source=p.discovery_source or prev.discovery_source,
            )
    return list(out.values())


def scan_all(chains: tuple[str, ...] | None = None) -> list[Pair]:
    if chains is None:
        chains = DEFAULT_SCAN_CHAINS
    """GeckoTerminal + Dexscreener boost trending birleşik tarama."""
    chains = restrict_chains(chains)  # merkezi kısıt: SOLANA_ONLY açıksa yalnız solana
    gecko: list[Pair] = []
    with httpx.Client() as client:
        for chain in chains:
            try:
                gecko.extend(fetch_trending(client, chain))
            except httpx.HTTPError as e:
                log.warning("%s taraması başarısız: %s", chain, e)
        ds: list[Pair] = []
        try:
            from hibrit_trader.dexscreener_scan import fetch_dexscreener_trending

            ds = fetch_dexscreener_trending(client, chains=tuple(chains))
        except Exception as e:
            log.warning("Dexscreener tarama atlandı: %s", e)
        early: list[Pair] = []
        try:
            from hibrit_trader.early_launch import fetch_early_launches

            early = fetch_early_launches(client, chains=tuple(chains))
        except Exception as e:
            log.warning("Erken launch tarama atlandı: %s", e)
        pump: list[Pair] = []
        try:
            from hibrit_trader.pump_fun_feed import fetch_pump_fun_pairs

            pump = fetch_pump_fun_pairs(client, chains=tuple(chains))
        except Exception as e:
            log.warning("Pump.fun feed atlandı: %s", e)
    return merge_pairs(gecko, ds, early, pump)


_scan_lock = threading.Lock()
_scan_cache: dict[tuple[str, ...], tuple[float, list[Pair]]] = {}


def scan_all_cached(chains: tuple[str, ...] | None = None) -> list[Pair]:
    """Dongu basina TEK tarama: taze sonucu paylas, hatada son iyi sonucla telafi."""
    key = tuple(restrict_chains(chains if chains is not None else DEFAULT_SCAN_CHAINS))
    now = time.monotonic()
    with _scan_lock:
        rec = _scan_cache.get(key)
        if rec is not None and now - rec[0] <= SCAN_CACHE_SEC and rec[1]:
            return list(rec[1])
    try:
        pairs = scan_all(chains)
    except Exception as e:
        log.warning("paylasimli tarama hatasi: %s", e)
        pairs = []
    now = time.monotonic()
    with _scan_lock:
        if pairs:
            _scan_cache[key] = (now, list(pairs))
            return list(pairs)
        rec = _scan_cache.get(key)
        if rec is not None and now - rec[0] <= SCAN_STALE_MAX_SEC and rec[1]:
            log.warning("tarama bos dondu, %.0fs onceki paylasimli sonucla telafi",
                        now - rec[0])
            return list(rec[1])
    return pairs
