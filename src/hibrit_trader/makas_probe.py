"""Makas probe: motorlardan bagimsiz periyodik round-trip makas olcumcusu.

M1/M2 durunca golge olcum de durdu (motor fill'ine bagliydi); canary on-sarti
(round-trip makas ortalamasi < tp hedefi) icin veri birikmeye devam etmeli.
Probe, m1_universe tokenlarinda saatte bir sanal al VE sat quote'u alir,
round-trip makasi dryrun_fills.jsonl'a yazar. Islem yok, state yok, motor yok.

Satir: tur="probe", al_fiyat / sat_fiyat Jupiter yurutulebilir fiyatlari,
fark_bps = (al_fiyat / sat_fiyat - 1) * 1e4 = round-trip makas (al pahali,
sat ucuz; pozitif deger gidis-donus maliyetidir).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

PROBE_INTERVAL_SEC = float(os.getenv("PROBE_INTERVAL_SEC", "3600"))
PROBE_USD = float(os.getenv("PROBE_USD", "200"))
PROBE_SLIPPAGE_BPS = int(os.getenv("PROBE_SLIPPAGE_BPS", "100"))


def _universe_tokens(data_dir: Path) -> list[dict]:
    p = data_dir / "m1_universe.json"
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text()).get("tokens") or [])
    except Exception:
        return []


def probe_token(token: dict) -> dict | None:
    """Tek token icin al+sat quote'u al, probe satirini dondur (yazmaz)."""
    from hibrit_trader.broker import _get_golge_broker

    addr = token.get("token_address")
    if not addr:
        return None
    br = _get_golge_broker()
    t0 = time.monotonic()
    q_al, neden = br._quote(addr, "al", PROBE_USD, PROBE_SLIPPAGE_BPS)
    if q_al is None or q_al.fiyat <= 0:
        log.warning("PROBE %s: al quote yok (%s)", token.get("symbol"), neden)
        return None
    q_sat, neden = br._quote(addr, "sat", q_al.miktar_token, PROBE_SLIPPAGE_BPS)
    if q_sat is None or q_sat.fiyat <= 0:
        log.warning("PROBE %s: sat quote yok (%s)", token.get("symbol"), neden)
        return None
    gecikme_ms = round((time.monotonic() - t0) * 1000, 1)
    rt_bps = round((q_al.fiyat / q_sat.fiyat - 1) * 10_000, 2)
    return {
        "ts": round(time.time(), 3),
        "tur": "probe",
        "engine": "PROBE",
        "token": addr,
        "symbol": token.get("symbol"),
        "usd": PROBE_USD,
        "al_fiyat": q_al.fiyat,
        "sat_fiyat": q_sat.fiyat,
        "fark_bps": rt_bps,
        "gecikme_ms": gecikme_ms,
        "al_route": q_al.route,
        "sat_route": q_sat.route,
    }


def probe_turu() -> int:
    """Evrendeki tum tokenlar icin bir olcum turu; yazilan satir sayisi doner."""
    from hibrit_trader.broker import _fills_yaz

    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    tokens = _universe_tokens(data_dir)
    if not tokens:
        log.warning("PROBE: evren bos ya da okunamadi, tur atlandi")
        return 0
    n = 0
    for token in tokens:
        try:
            row = probe_token(token)
        except Exception as e:
            log.warning("PROBE %s: olcum hatasi (%s)", token.get("symbol"), e)
            continue
        if row is not None:
            _fills_yaz(row)
            n += 1
        time.sleep(1.0)  # lite-api nezaket araligi
    log.warning("PROBE turu bitti: %d/%d token olculdu", n, len(tokens))
    return n


def run_forever() -> None:
    while True:
        try:
            probe_turu()
        except Exception as e:
            log.warning("PROBE: tur hatasi (%s)", e)
        time.sleep(PROBE_INTERVAL_SEC)
