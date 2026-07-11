"""Canli cuzdan gostergesi: SOL bakiyesi + acik canli pozisyon degeri (USD).

Motor/kural katmanina dokunmaz, sadece okur. Kendi dongusunde calisir ki
RPC kotasi panel poll'undan bagimsiz kalsin (CANLI_POLL_SEC, varsayilan 45s).
Son olcum bellekte snapshot olarak durur (son()); egri data/canli_equity.jsonl
dosyasina yazilir (panel _equity_series ile ayni satir formati: ts/eq).
Acik pozisyon degeri v7_state.json'daki canli_miktar * last_price'tan gelir;
motor last_price'i zaten guncelliyor, buradan ek API cagrisi cikmaz.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import httpx

from hibrit_trader.config import DEFAULT_RPC

log = logging.getLogger("hibrit.canli")

CUZDAN = "DZXZGD5FURZDwa5BWByxxd7iLdCvGxSCy6RWHsgupaYa"
_ROTATE_BYTES = 5 * 1024 * 1024

_lock = threading.Lock()
_son: dict | None = None


def _data_dir() -> Path:
    return Path(os.getenv("MOMENTUM_DATA_DIR", "data"))


def baz_usd() -> float:
    """Baslangic referansi: ASAMA 2 acilis degeri (12 Tem karari, sabit baz)."""
    try:
        return float(os.getenv("CANLI_BAZ_USD", "119.59"))
    except ValueError:
        return 119.59


def _sol_bakiye(client: httpx.Client) -> float | None:
    rpc = os.getenv("SOLANA_RPC_URL") or DEFAULT_RPC["solana"]
    try:
        r = client.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getBalance",
                                   "params": [CUZDAN]}, timeout=15)
        return int(r.json()["result"]["value"]) / 1e9
    except Exception as e:
        log.warning("canli gosterge: bakiye alinamadi: %s", e)
        return None


def _v7_canli_ozet() -> tuple[float, int, int]:
    """(acik canli poz degeri usd, acik canli poz sayisi, kapali canli islem sayisi)."""
    d = _data_dir()
    poz_usd, poz_n = 0.0, 0
    try:
        state = json.loads((d / "v7_state.json").read_text())
        for p in state.get("positions") or []:
            m = float(p.get("canli_miktar") or 0.0)
            if m > 0:
                poz_usd += m * float(p.get("last_price") or 0.0)
                poz_n += 1
    except Exception:
        pass
    islem_n = 0
    tp = d / "v7_trades.jsonl"
    if tp.exists():
        try:
            for ln in tp.read_text().splitlines():
                if not ln.strip():
                    continue
                try:
                    if json.loads(ln).get("signature"):
                        islem_n += 1
                except ValueError:
                    continue
        except OSError:
            pass
    return round(poz_usd, 2), poz_n, islem_n


def _egri_yaz(ts: float, eq: float) -> None:
    try:
        d = _data_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / "canli_equity.jsonl"
        if p.exists() and p.stat().st_size > _ROTATE_BYTES:
            stamp = time.strftime("%Y-%m-%d", time.gmtime())
            hedef = p.with_name(f"canli_equity_arsiv_{stamp}.jsonl")
            n = 1
            while hedef.exists():
                n += 1
                hedef = p.with_name(f"canli_equity_arsiv_{stamp}.{n}.jsonl")
            p.rename(hedef)
        with p.open("a") as fh:
            fh.write(json.dumps({"ts": ts, "eq": eq}) + "\n")
    except Exception:
        pass


def olc(client: httpx.Client) -> dict | None:
    """Tek olcum turu: mtm hesapla, snapshot guncelle, egriye yaz."""
    from hibrit_trader.jupiter import fetch_sol_price_usd

    sol = _sol_bakiye(client)
    if sol is None:
        return None
    fiyat = fetch_sol_price_usd(client, fallback=0.0)
    if fiyat <= 0:
        # yanlis fiyatla sahte mtm sicramasi yazmaktansa turu atla
        log.warning("canli gosterge: SOL fiyati alinamadi, tur atlandi")
        return None
    poz_usd, poz_n, islem_n = _v7_canli_ozet()
    mtm = round(sol * fiyat + poz_usd, 2)
    snap = {"ts": round(time.time(), 1), "mtm": mtm, "sol": round(sol, 4),
            "sol_fiyat": round(fiyat, 2), "poz_usd": poz_usd,
            "acik_poz": poz_n, "islem_n": islem_n}
    global _son
    with _lock:
        _son = snap
    _egri_yaz(snap["ts"], mtm)
    return snap


def son() -> dict | None:
    with _lock:
        return dict(_son) if _son else None


def run_forever() -> None:
    poll = 45.0
    try:
        poll = max(15.0, float(os.getenv("CANLI_POLL_SEC", "45") or 45))
    except ValueError:
        pass
    client = httpx.Client(timeout=15)
    log.warning("CANLI gosterge basladi: cuzdan %s..., poll %.0fs, baz $%.2f",
                CUZDAN[:4], poll, baz_usd())
    while True:
        try:
            olc(client)
        except Exception as e:
            log.warning("canli gosterge tur hatasi: %s", e)
        time.sleep(poll)
