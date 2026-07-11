"""RugCheck top holder yoğunluğu — rug erken uyarı (CloddsBot fikir transferi, hafif filtre)."""

from __future__ import annotations

import logging
import os
import time

import httpx

from hibrit_trader.rugcheck import RUGCHECK_BASE, _rate_limit
from hibrit_trader.safety import SafetyReport

log = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, SafetyReport]] = {}
_CACHE_TTL = 600.0


def holder_risk_enabled() -> bool:
    return os.getenv("HOLDER_RISK_ENABLED", "1") != "0"


def _top1_max_pct(*, genesis_ok: bool) -> float:
    if genesis_ok:
        return float(os.getenv("GENESIS_TOP1_HOLDER_MAX_PCT", "55"))
    return float(os.getenv("TOP1_HOLDER_MAX_PCT", "45"))


def _top10_max_pct(*, genesis_ok: bool) -> float:
    if genesis_ok:
        return float(os.getenv("GENESIS_TOP10_HOLDER_MAX_PCT", "88"))
    if os.getenv("MAX_TOP10_HOLDER_PCT"):
        return float(os.getenv("MAX_TOP10_HOLDER_PCT"))
    return float(os.getenv("TOP10_HOLDER_MAX_PCT", "75"))


def holder_report_from_payload(data: dict, *, genesis_ok: bool = False) -> SafetyReport:
    holders = data.get("topHolders") or []
    if not holders:
        return SafetyReport(ok=True, reasons=[])

    top1 = float(holders[0].get("pct") or 0)
    top10 = sum(float(h.get("pct") or 0) for h in holders[:10])
    reasons: list[str] = []

    if top1 > _top1_max_pct(genesis_ok=genesis_ok):
        reasons.append(f"top1 holder %{top1:.0f}")
    if top10 > _top10_max_pct(genesis_ok=genesis_ok):
        reasons.append(f"top10 holder %{top10:.0f}")

    insiders = sum(1 for h in holders[:5] if h.get("insider"))
    if insiders >= 3 and not genesis_ok:
        reasons.append(f"insider holder {insiders}/5")

    metrics = {
        "top1_holder_pct": round(top1, 2),
        "top10_holder_pct": round(top10, 2),
        "insider_count": insiders,
    }
    return SafetyReport(ok=not reasons, reasons=reasons, metrics=metrics)


def check_holder_concentration(
    client: httpx.Client,
    mint: str,
    *,
    genesis_ok: bool = False,
) -> SafetyReport:
    """RugCheck full report — ~1 req/s, cache 10 dk."""
    if not holder_risk_enabled() or not mint:
        return SafetyReport(ok=True, reasons=[])

    cache_key = f"{mint}:{'g' if genesis_ok else 's'}"
    row = _CACHE.get(cache_key)
    if row and time.time() - row[0] < _CACHE_TTL:
        return row[1]

    _rate_limit()
    try:
        url = f"{RUGCHECK_BASE}/v1/tokens/{mint}/report"
        resp = client.get(url, headers={"accept": "application/json"}, timeout=20)
        resp.raise_for_status()
        report = holder_report_from_payload(resp.json() or {}, genesis_ok=genesis_ok)
    except httpx.HTTPError as exc:
        # Fail-closed: veri alinamayan token GECMEZ. Hata raporu cache'e
        # YAZILMAZ ki kesinti bitince ilk sorguda toparlansin; basarili
        # sonuclar 600s cache'te kaldigi icin kesintide asiri red olmaz.
        log.warning(
            "HOLDER RISK: veri alinamadi (%s), fail-closed RED: %s",
            type(exc).__name__, mint,
        )
        return SafetyReport(
            ok=False,
            reasons=[f"holder verisi alinamadi: {type(exc).__name__}"],
            kapi="holder_hata",
        )

    _CACHE[cache_key] = (time.time(), report)
    return report
