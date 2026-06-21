"""Erken launch keşfi — trending ÖNCESİ, genesis havuz seçimi.

DexScreener trending = pump SONRASI. Bu modül:
- token-profiles/latest + boosts/latest (yeni listelenenler)
- çoklu havuzda en genç + m5 hızlı havuzu seçer (max liq tuzağı yok)
- genesis skoru: yaş küçük, h1 henüz parabolik değil, m5 txns/vol sıcak
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

from hibrit_trader.config import API, DEFAULT_SCAN_CHAINS, restrict_chains
from hibrit_trader.dexscreener_scan import _best_pair_per_token, pair_from_dexscreener
from hibrit_trader.dex_trending_strategy import pool_age_hours
from hibrit_trader.scanner import Pair, _f

log = logging.getLogger(__name__)


def early_launch_enabled() -> bool:
    return os.getenv("EARLY_LAUNCH", "1") != "0"


def genesis_max_age_hours() -> float:
    return float(os.getenv("GENESIS_MAX_AGE_H", "6"))


def genesis_max_h1_pct() -> float:
    """Parabolik tepede alma — Merlin +272k% burada elenir."""
    return float(os.getenv("GENESIS_MAX_H1_PCT", "85"))


def genesis_min_m5_txns() -> int:
    return int(os.getenv("GENESIS_MIN_M5_TXNS", "12"))


def genesis_min_liq_usd() -> float:
    return float(os.getenv("GENESIS_MIN_LIQ_USD", "8000"))


def m5_turnover(pair: Pair) -> float:
    return pair.vol_m5 / max(pair.liquidity_usd, 1.0)


def genesis_score(pair: Pair) -> float:
    """0..100 — yüksek = erken pump adayı (tepe DEĞİL)."""
    age = pool_age_hours(pair)
    if age is None or age > genesis_max_age_hours():
        return 0.0
    if pair.liquidity_usd < genesis_min_liq_usd():
        return 0.0
    h1 = pair.chg_h1
    if h1 > genesis_max_h1_pct():
        return 0.0
    if h1 < -15 and pair.chg_m5 < -10:
        return 0.0

    tx_m5 = int(getattr(pair, "txns_m5", 0) or 0)
    if tx_m5 < genesis_min_m5_txns() and pair.vol_m5 < 500:
        return 0.0

    score = 0.0
    # Gençlik — 0-6h linear
    score += max(0.0, 28.0 - age * 4.5)
    # m5 aktivite
    score += min(25.0, tx_m5 * 0.8)
    score += min(20.0, m5_turnover(pair) * 8.0)
    # Erken momentum (5-85% h1 ideal; 0-5% + sıcak m5 OK)
    if 3 <= h1 <= genesis_max_h1_pct():
        score += min(18.0, h1 * 0.22)
    elif h1 <= 3 and pair.chg_m5 > 2:
        score += min(14.0, pair.chg_m5 * 1.2)
    # Erken boost
    boost = int(getattr(pair, "boost_score", 0) or 0)
    if 10 <= boost < 100:
        score += 12.0
    elif 0 < boost < 10:
        score += 6.0
    if boost >= 500:
        score -= 20.0
    if getattr(pair, "discovery_source", "") == "pump_fun":
        from hibrit_trader.pump_fun_feed import pump_fun_genesis_bonus

        score += pump_fun_genesis_bonus()
    return round(min(100.0, max(0.0, score)), 1)


def genesis_entry_ok(pair: Pair) -> tuple[bool, str]:
    g = genesis_score(pair)
    if g < float(os.getenv("GENESIS_ENTRY_MIN", "52")):
        return False, f"genesis {g:.0f}<{os.getenv('GENESIS_ENTRY_MIN', '52')}"
    age = pool_age_hours(pair)
    return True, f"genesis {g:.0f} · age {age:.1f}h · h1 {pair.chg_h1:.0f}% · m5 tx {getattr(pair, 'txns_m5', 0)}"


def runner_max_age_h() -> float:
    return float(os.getenv("RUNNER_MAX_AGE_H", "24"))


def runner_min_age_h() -> float:
    return float(os.getenv("RUNNER_MIN_AGE_H", "6"))


def runner_entry_ok(pair: Pair) -> tuple[bool, str]:
    """6–24h momentum koşucusu — SIN sınıfı (genesis yaşını geçmiş ama h1/m5 sıcak)."""
    if os.getenv("RUNNER_ENTRY", "1") == "0":
        return False, "runner kapalı"
    if is_trending_late_pump(pair):
        return False, "runner late-pump"
    age = pool_age_hours(pair)
    if age is None or age < runner_min_age_h() or age > runner_max_age_h():
        return False, f"runner yaş {age or 999:.0f}h"
    h1_min = float(os.getenv("RUNNER_MIN_H1", "15"))
    m5_min = float(os.getenv("RUNNER_MIN_M5", "5"))
    if pair.chg_h1 < h1_min or pair.chg_m5 < m5_min:
        return False, f"runner mom h1={pair.chg_h1:.0f} m5={pair.chg_m5:.0f}"
    if pair.liquidity_usd < float(os.getenv("RUNNER_MIN_LIQ_USD", "15000")):
        return False, "runner liq düşük"
    if pair.chg_h1 > genesis_max_h1_pct():
        return False, "runner parabolik h1"
    rs = round(
        min(
            100.0,
            pair.chg_h1 * 1.6
            + pair.chg_m5 * 2.2
            + min(22.0, m5_turnover(pair) * 12.0)
            + min(12.0, pair.txns_h1 / 400),
        ),
        1,
    )
    need = float(os.getenv("RUNNER_ENTRY_MIN", "52"))
    if rs < need:
        return False, f"runner {rs:.0f}<{need:.0f}"
    return True, f"runner {rs:.0f} · age {age:.1f}h · h1 {pair.chg_h1:.0f}% · m5 {pair.chg_m5:.0f}%"


def pump_entry_ok(pair: Pair) -> tuple[bool, str]:
    """Genesis veya runner — trending genç pump sınıfı giriş."""
    ok, note = genesis_entry_ok(pair)
    if ok:
        return True, note
    return runner_entry_ok(pair)


def late_pump_h24_pct() -> float:
    return float(os.getenv("LATE_PUMP_H24_PCT", "400"))


def is_trending_late_pump(pair: Pair) -> bool:
    """DexScreener trending #1–10 sınıfı — pump SONRASI (Merlin/Chaton/ALGOPUB tipi)."""
    age = pool_age_hours(pair)
    h1 = pair.chg_h1
    h24 = pair.chg_h24
    if h1 > genesis_max_h1_pct():
        return True
    late_h24 = late_pump_h24_pct()
    if h24 > late_h24:
        max_age = float(os.getenv("LATE_PUMP_MAX_AGE_H", "8"))
        if age is None or age > max_age:
            return True
        if h24 > late_h24 * 2:
            return True
    return False


def classify_pump_window(pair: Pair) -> dict:
    """Trending meme sınıfı — tek coin değil, pencere etiketi (panel + sıralama)."""
    gen = genesis_score(pair)
    age = pool_age_hours(pair)
    if is_trending_late_pump(pair):
        return {
            "window": "trending_late",
            "label": "Trend geç — tepe kaçırıldı",
            "action": "avoid",
            "genesis_score": gen,
        }
    ok, note = pump_entry_ok(pair)
    if ok and note.startswith("genesis"):
        return {
            "window": "genesis",
            "label": "Genesis — erken giriş",
            "action": "enter",
            "genesis_score": gen,
        }
    if ok and note.startswith("runner"):
        return {
            "window": "runner",
            "label": "Runner — momentum koşusu",
            "action": "enter",
            "genesis_score": gen,
        }
    if gen >= float(os.getenv("GENESIS_POOL_MIN", "40")):
        return {
            "window": "early",
            "label": "Erken izle",
            "action": "watch",
            "genesis_score": gen,
        }
    if age is not None and age <= genesis_max_age_hours() and pair.chg_h1 > 0:
        return {
            "window": "early",
            "label": "Genç momentum",
            "action": "watch",
            "genesis_score": gen,
        }
    return {
        "window": "standard",
        "label": "Standart",
        "action": "watch",
        "genesis_score": gen,
    }


def select_entry_pool(items: list[dict], *, boost_score: int = 0) -> Optional[Pair]:
    """Token için giriş havuzu — genesis aday varsa genç/sıcak havuz, yoksa max liq."""
    candidates: list[Pair] = []
    for item in items:
        p = pair_from_dexscreener(item, boost_score=boost_score)
        if p and p.liquidity_usd > 0:
            candidates.append(p)
    if not candidates:
        return None

    genesis_ranked = [(genesis_score(p), p) for p in candidates]
    genesis_ranked.sort(key=lambda x: x[0], reverse=True)
    best_g, best_p = genesis_ranked[0]
    if best_g >= float(os.getenv("GENESIS_POOL_MIN", "40")):
        return best_p

    best = max(candidates, key=lambda p: p.liquidity_usd)
    return best


def _fetch_token_rows(client: httpx.Client, chain: str, addrs: list[str]) -> list[Pair]:
    out: list[Pair] = []
    for i in range(0, len(addrs), 30):
        chunk = addrs[i : i + 30]
        url = f"{API['dexscreener']}/tokens/v1/{chain}/" + ",".join(chunk)
        try:
            resp = client.get(url, timeout=20)
            resp.raise_for_status()
            raw = resp.json() or []
        except httpx.HTTPError as e:
            log.warning("early_launch tokens %s: %s", chain, e)
            continue
        if not isinstance(raw, list):
            continue
        by_token: dict[str, list[dict]] = {}
        for item in raw:
            base = item.get("baseToken") or {}
            addr = str(base.get("address") or "")
            if addr:
                by_token.setdefault(addr, []).append(item)
        for addr, pool_items in by_token.items():
            p = select_entry_pool(pool_items, boost_score=0)
            if p:
                out.append(p)
        time.sleep(0.12)
    return out


def fetch_early_launches(
    client: httpx.Client,
    chains: tuple[str, ...] = DEFAULT_SCAN_CHAINS,
) -> list[Pair]:
    """Yeni profil + yeni boost — trending top değil."""
    if not early_launch_enabled():
        return []

    chains = restrict_chains(chains)  # SOLANA_ONLY açıksa EVM token satırları hiç istenmez
    token_boost: dict[str, int] = {}
    addrs_by_chain: dict[str, set[str]] = {c: set() for c in chains}

    for path in (
        "/token-profiles/latest/v1",
        "/token-profiles/recent-updates/v1",
        "/token-boosts/latest/v1",
    ):
        try:
            r = client.get(f"{API['dexscreener']}{path}", timeout=15)
            r.raise_for_status()
            rows = r.json() or []
        except httpx.HTTPError as e:
            log.warning("early_launch %s: %s", path, e)
            continue
        for row in rows:
            chain = str(row.get("chainId") or "")
            if chain not in chains:
                continue
            addr = str(row.get("tokenAddress") or "")
            if not addr:
                continue
            addrs_by_chain.setdefault(chain, set()).add(addr)
            if "boost" in path or "totalAmount" in row:
                token_boost[addr] = max(token_boost.get(addr, 0), int(_f(row.get("totalAmount"))))

    out: list[Pair] = []
    for chain, addrs in addrs_by_chain.items():
        if not addrs:
            continue
        pairs = _fetch_token_rows(client, chain, sorted(addrs))
        for p in pairs:
            bs = token_boost.get(p.token_address, 0)
            if bs and bs > p.boost_score:
                p = Pair(**{**p.__dict__, "boost_score": bs})
            if genesis_score(p) > 0 or bs > 0:
                out.append(p)
    log.info("early_launch: %d aday (profiles+boosts latest)", len(out))
    return out
