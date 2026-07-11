"""GoPlus güvenlik filtresi — geçmeyen coin'e işlem YOK (pazarlıksız kural).

EVM: token_security/{chain_id} · Solana: solana/token_security — ikisi de ücretsiz.
Solana: RugCheck yedek / çift kontrol (Faz 8b).
API erişilemezse karar 'belirsiz' değil 'RED' — güvenlik filtresi fail-closed çalışır.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

import httpx

from hibrit_trader.config import API, GOPLUS_EVM_CHAIN_ID

MAX_TAX_PCT = 10.0


def _max_top10_holder_pct() -> float:
    """Paper agresif modda meme havuzları için daha yüksek tavan (bilinçli risk)."""
    if os.getenv("MAX_TOP10_HOLDER_PCT"):
        return float(os.getenv("MAX_TOP10_HOLDER_PCT", "70"))
    if os.getenv("BOT_MODE", "paper").lower() == "paper" and os.getenv("PAPER_AGGRESSIVE", "1") != "0":
        return 92.0
    return 70.0


@dataclass
class SafetyReport:
    ok: bool
    reasons: list = field(default_factory=list)  # RED nedenleri (boş = temiz)
    metrics: dict = field(default_factory=dict)  # ham sayısal: top1/top10/insider, mint/freeze, rugcheck_score
    kapi: str = ""  # kapi-bazli reject etiketi (orn. "holder_hata"); bos = genel safety_red


def _goplus_no_data(report: SafetyReport) -> bool:
    if not report.reasons:
        return False
    msg = report.reasons[0].lower()
    return "verisi yok" in msg or "erişilemedi" in msg


def entry_safety_ok(report: SafetyReport, *, genesis_ok: bool = False) -> tuple[bool, str]:
    """Giriş güvenliği — paper genesis: index yok/warn kabul, danger/honeypot asla."""
    if report.ok:
        return True, "OK"
    if not genesis_ok or os.getenv("BOT_MODE", "paper").lower() != "paper":
        return False, "; ".join(report.reasons[:2])
    if os.getenv("GENESIS_SAFETY_LAX", "1") == "0":
        return False, "; ".join(report.reasons[:2])
    blob = " ".join(report.reasons).lower()
    hard = (
        "honeypot",
        "rugcheck danger",
        "satış engelli",
        "mint yetkisi açık",
        "freeze yetkisi açık",
        "transfer hook",
    )
    if any(h in blob for h in hard):
        return False, "; ".join(report.reasons[:2])
    if _goplus_no_data(report) or "rugcheck verisi yok" in blob or "rugcheck erişilemedi" in blob:
        return True, "genesis paper · index yok"
    if report.reasons and all(
        "warn" in r.lower() or "skor" in r.lower() for r in report.reasons
    ):
        return True, "genesis paper · warn kabul"
    return False, "; ".join(report.reasons[:2])


def _merge_solana_safety(goplus: SafetyReport, rugcheck: SafetyReport) -> SafetyReport:
    if not goplus.ok and _goplus_no_data(goplus):
        return rugcheck
    if not goplus.ok:
        return goplus
    if os.getenv("RUGCHECK_STRICT", "1") == "0":
        return goplus
    if not rugcheck.ok:
        return SafetyReport(ok=False, reasons=list(rugcheck.reasons))
    return goplus


def _evm_decision(d: dict) -> SafetyReport:
    """GoPlus EVM token_security yanıtından karar üretir."""
    reasons = []
    if d.get("is_honeypot") == "1":
        reasons.append("honeypot")
    if d.get("cannot_sell_all") == "1":
        reasons.append("satış engelli")
    if float(d.get("buy_tax") or 0) * 100 > MAX_TAX_PCT:
        reasons.append(f"alım vergisi >%{MAX_TAX_PCT:.0f}")
    if float(d.get("sell_tax") or 0) * 100 > MAX_TAX_PCT:
        reasons.append(f"satış vergisi >%{MAX_TAX_PCT:.0f}")
    if d.get("is_open_source") == "0":
        reasons.append("kontrat kapalı kaynak")
    if d.get("hidden_owner") == "1":
        reasons.append("gizli owner")
    if d.get("owner_change_balance") == "1":
        reasons.append("owner bakiye değiştirebilir")
    if d.get("is_mintable") == "1":
        reasons.append("mint edilebilir")
    holders = d.get("holders") or []
    top10 = sum(float(h.get("percent") or 0) for h in holders[:10]) * 100
    top1 = float(holders[0].get("percent") or 0) * 100 if holders else 0.0
    cap = _max_top10_holder_pct()
    if top10 > cap:
        reasons.append(f"top10 holder %{top10:.0f}")
    mintable = d.get("is_mintable") == "1"
    metrics = {
        "top1_holder_pct": round(top1, 2),
        "top10_holder_pct": round(top10, 2),
        "mint_revoked": not mintable,
        "honeypot": d.get("is_honeypot") == "1",
    }
    return SafetyReport(ok=not reasons, reasons=reasons, metrics=metrics)


def _solana_decision(d: dict) -> SafetyReport:
    """GoPlus Solana token_security yanıtından karar üretir."""
    reasons = []
    mintable = (d.get("mintable") or {}).get("status") == "1"
    freezable = (d.get("freezable") or {}).get("status") == "1"
    if mintable:
        reasons.append("mint yetkisi açık")
    if freezable:
        reasons.append("freeze yetkisi açık")
    if (d.get("transfer_fee_upgradable") or {}).get("status") == "1":
        reasons.append("transfer ücreti değiştirilebilir")
    if d.get("transfer_hook") not in (None, [], ""):
        reasons.append("transfer hook var")
    holders = d.get("holders") or []
    top10 = sum(float(h.get("percent") or 0) for h in holders[:10])
    top1 = float(holders[0].get("percent") or 0) if holders else 0.0
    cap = _max_top10_holder_pct()
    if top10 > cap:
        reasons.append(f"top10 holder %{top10:.0f}")
    metrics = {
        "top1_holder_pct": round(top1, 2),
        "top10_holder_pct": round(top10, 2),
        "mint_revoked": not mintable,
        "freeze_revoked": not freezable,
    }
    return SafetyReport(ok=not reasons, reasons=reasons, metrics=metrics)


def _check_goplus(client: httpx.Client, chain: str, token_address: str) -> SafetyReport:
    try:
        if chain == "solana":
            url = f"{API['goplus']}/solana/token_security"
            resp = client.get(url, params={"contract_addresses": token_address}, timeout=15)
            resp.raise_for_status()
            data = (resp.json().get("result") or {}).get(token_address)
            if not data:
                return SafetyReport(ok=False, reasons=["GoPlus verisi yok"])
            return _solana_decision(data)

        chain_id = GOPLUS_EVM_CHAIN_ID.get(chain)
        if not chain_id:
            return SafetyReport(ok=False, reasons=[f"desteklenmeyen ağ: {chain}"])
        url = f"{API['goplus']}/token_security/{chain_id}"
        resp = client.get(url, params={"contract_addresses": token_address}, timeout=15)
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        data = result.get(token_address.lower()) or result.get(token_address)
        if not data:
            return SafetyReport(ok=False, reasons=["GoPlus verisi yok"])
        return _evm_decision(data)
    except httpx.HTTPError as e:
        return SafetyReport(ok=False, reasons=[f"GoPlus erişilemedi: {type(e).__name__}"])


_token_cache: dict[tuple[str, str, bool], tuple[float, SafetyReport]] = {}
_token_cache_lock = threading.Lock()


def _token_cache_ttl() -> float:
    return float(os.getenv("SAFETY_CACHE_TTL_SEC", "90"))


def check_token(
    client: httpx.Client,
    chain: str,
    token_address: str,
    *,
    genesis_ok: bool = False,
) -> SafetyReport:
    """Tek token güvenlik kararı, motorlar arası paylaşımlı cache (TTL 90s).

    Aynı token'i her motor ayrı ayrı sorgulamasın diye sonuç kısa süre saklanır;
    karar mantığı _check_token_taze içinde değişmeden durur (fail-closed).
    """
    key = (chain, token_address, genesis_ok)
    now = time.time()
    with _token_cache_lock:
        hit = _token_cache.get(key)
        if hit is not None and now - hit[0] < _token_cache_ttl():
            return hit[1]
    report = _check_token_taze(client, chain, token_address, genesis_ok=genesis_ok)
    with _token_cache_lock:
        _token_cache[key] = (time.time(), report)
        if len(_token_cache) > 512:
            esik = time.time() - _token_cache_ttl()
            for k in [k for k, (ts, _) in _token_cache.items() if ts < esik]:
                del _token_cache[k]
    return report


def _check_token_taze(
    client: httpx.Client,
    chain: str,
    token_address: str,
    *,
    genesis_ok: bool = False,
) -> SafetyReport:
    """Tek token güvenlik kararı. Veri yoksa/erişim hatasında RED (fail-closed)."""
    report = _check_goplus(client, chain, token_address)
    metrics = dict(report.metrics)
    if chain != "solana":
        return report
    from hibrit_trader.rugcheck import check_rugcheck_summary, rugcheck_enabled

    if rugcheck_enabled():
        rug = check_rugcheck_summary(client, token_address)
        metrics.update(rug.metrics)
        report = _merge_solana_safety(report, rug)

    from hibrit_trader.holder_risk import check_holder_concentration, holder_risk_enabled

    if holder_risk_enabled():
        holder = check_holder_concentration(client, token_address, genesis_ok=genesis_ok)
        metrics.update(holder.metrics)
        if not holder.ok:
            return SafetyReport(
                ok=False, reasons=list(holder.reasons), metrics=metrics, kapi=holder.kapi
            )
    report.metrics = metrics
    return report
