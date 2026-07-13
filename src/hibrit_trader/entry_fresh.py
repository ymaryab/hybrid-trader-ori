"""Giris taze-fiyat teyidi: cikistaki hizli gozun simetrigi (v6/v7/X1).

Aday tum filtreleri gectikten sonra, ALIM kaydedilmeden hemen once fiyat
tazelenir. Trading kurallarina sifir dokunus, sadece fiyat katmani.

Kaynak oncelik sirasi:
  1) fast_price feed (kayit 3 sn'den tazeyse)     -> kaynak "fast"
  2) tek seferlik dogrudan fetch_pool_price       -> kaynak "fetch"
  3) tarama fiyati (fail-open: kaynak yoksa giris ENGELLENMEZ, loglanir)
                                                  -> kaynak "scan"

Karar (esik MOM_ENTRY_FRESH_MAX_PCT, varsayilan 2.0):
  - taze fiyat tarama fiyatinin esikten FAZLA ustundeyse giris IPTAL
    (fiyat kacmis, spike tepesi riski): momentum_rejects.jsonl'e
    "taze_fiyat_kacti" satiri + 30dk recheck kuyrugu (kacirilan olculur).
  - esikten fazla asagidaysa veya aradaysa giris TAZE fiyattan kaydedilir
    (daha durust maliyet).

Kayit: motorlar pozisyona ve trade satirina entry_price_source
(fast/fetch/scan) ve entry_fresh_fark_pct (tarama-taze fark yuzdesi) yazar.

Ek enstrumantasyon (10 Tem): rejim_reject_kaydet (rejim kapaliyken filtreyi
gecen adaylar + 30dk recheck, "rejim yuzunden ne kacti" olculur) ve HuniSayac
(gunluk giris-filtre hunisi, aday-yoklugu gunleri olculur).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass

import httpx

from hibrit_trader.fast_price import get_feed
from hibrit_trader.live_sim import fetch_pool_price
from hibrit_trader.momentum_session import (
    REJECT_RECHECK_SEC,
    REJECTS_FILE,
    _data_dir,
)
from hibrit_trader.paper import _now_iso

log = logging.getLogger(__name__)

FRESH_MAX_PCT = float(os.getenv("MOM_ENTRY_FRESH_MAX_PCT", "2.0"))
FAST_MAX_AGE_SEC = 3.0          # feed kaydi bundan eskiyse fetch'e dusulur
RECHECK_POLL_SEC = 30.0         # recheck kuyrugu tarama kadansi
RECHECK_MAX_PER_TICK = 10       # tick basina en cok 10 GET (yuk siniri)
WATCH_CAP = 100                 # kuyruk tavani (dosya/istek sismesin)
REJIM_REJECT_MAX_PER_TICK = 5   # rejim kapali tick'inde en cok 5 aday kaydi


@dataclass(frozen=True)
class TazeSonuc:
    fiyat: float            # giriste kullanilacak fiyat
    kaynak: str             # "fast" | "fetch" | "scan"
    fark_pct: float | None  # (taze/tarama - 1) * 100; kaynak yoksa None
    iptal: bool             # True: fiyat kacmis, giris yapilmaz


def taze_teyit(pair, motor: str, client: httpx.Client | None = None) -> TazeSonuc:
    """Tarama fiyatini taze fiyatla karsilastir, giris karari icin sonuc dondur."""
    scan_price = float(getattr(pair, "price_usd", 0.0) or 0.0)
    taze = None
    kaynak = "scan"
    feed = get_feed()
    if feed is not None:
        rec = feed.get_price(pair.pool_address, max_age_sec=FAST_MAX_AGE_SEC)
        if rec is not None:
            taze, kaynak = rec[0], "fast"
    if taze is None and client is not None:
        try:
            p = fetch_pool_price(client, pair.chain, pair.pool_address)
            if p is not None and p > 0:
                taze, kaynak = float(p), "fetch"
        except Exception:
            log.debug("%s taze fiyat fetch hatasi (%s)", motor, pair.name, exc_info=True)
    if taze is None or taze <= 0 or scan_price <= 0:
        log.info("%s taze fiyat kaynagi yok, tarama fiyatiyla devam (fail-open): %s",
                 motor, pair.name)
        return TazeSonuc(scan_price, "scan", None, False)
    fark = round((taze / scan_price - 1) * 100, 4)
    if fark > FRESH_MAX_PCT:
        _reject_kacti(pair, motor, scan_price, taze, fark)
        return TazeSonuc(taze, kaynak, fark, True)
    return TazeSonuc(taze, kaynak, fark, False)


# ---- taze_fiyat_kacti: reject kaydi + 30dk recheck kuyrugu -----------------------

_watch_lock = threading.Lock()
_watch: dict[str, dict] = {}
_recheck_thread: threading.Thread | None = None


def _rejects_yaz(row: dict) -> None:
    p = _data_dir() / REJECTS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": round(time.time(), 3), "ts_iso": _now_iso(), **row}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _reject_kacti(pair, motor: str, scan_price: float, taze: float, fark: float) -> None:
    try:
        now = time.time()
        _rejects_yaz({
            "type": "reject",
            "reason": "taze_fiyat_kacti",
            "engine": motor,
            "pair": pair.name,
            "chain": pair.chain,
            "pool_address": pair.pool_address,
            "token_address": pair.token_address,
            "liquidity_usd": round(getattr(pair, "liquidity_usd", 0.0), 2),
            "chg_m5": round(getattr(pair, "chg_m5", 0.0), 2),
            "chg_h1": round(getattr(pair, "chg_h1", 0.0), 2),
            "price_usd": scan_price,
            "fresh_price": taze,
            "fark_pct": fark,
        })
        with _watch_lock:
            if pair.pool_address not in _watch and len(_watch) < WATCH_CAP:
                _watch[pair.pool_address] = {
                    "pair": pair.name,
                    "chain": pair.chain,
                    "pool_address": pair.pool_address,
                    "reason": "taze_fiyat_kacti",
                    "engine": motor,
                    "engines": {motor},
                    "price_at_reject": taze,
                    "reject_ts": now,
                    "due_ts": now + REJECT_RECHECK_SEC,
                }
        _start_recheck_thread()
    except Exception:
        log.debug("taze_fiyat_kacti kaydi hatasi", exc_info=True)


def safety_reject_kaydet(pair, motor: str, neden: str, detay: str = "") -> None:
    """Guvenlik kontrolu girisi engelledi: motor etiketli gorunur reject kaydi.

    neden: "safety_red" (rapor RED) | "safety_hata" (kontrol exception atti)
    | "holder_hata" (holder verisi alinamadi, fail-closed RED).
    Sessiz continue'nun yerine olcum satiri; recheck kuyruguna girmez.
    """
    try:
        _rejects_yaz({
            "type": "reject",
            "reason": neden,
            "engine": motor,
            "pair": pair.name,
            "chain": pair.chain,
            "pool_address": pair.pool_address,
            "token_address": pair.token_address,
            "liquidity_usd": round(getattr(pair, "liquidity_usd", 0.0), 2),
            "chg_m5": round(getattr(pair, "chg_m5", 0.0), 2),
            "chg_h1": round(getattr(pair, "chg_h1", 0.0), 2),
            "price_usd": float(getattr(pair, "price_usd", 0.0) or 0.0),
            "detay": detay,
        })
    except Exception:
        log.debug("safety reject kaydi hatasi", exc_info=True)


def rejim_reject_kaydet(cands, motor: str, sol_h1: float | None) -> None:
    """Rejim kapaliyken diger tum filtreleri GECEN adaylar: reject + 30dk recheck.

    Dedupe motor boyutlu: ayni havuz icin her motor kendi kayit satirini yazar,
    recheck kuyruguna ise tek giris yapilir. Ayni motor ayni havuzu kuyruk
    suresince tekrar yazmaz; rejim uzun sure kapali kalirsa kayit kadansi
    dogal olarak ~30dk'ya iner.
    """
    try:
        now = time.time()
        yazilan = False
        # M1 paterni: veri-yok (fail-closed) ile negatif-rejim ayri etikette
        neden = "rejim_veri_yok" if sol_h1 is None else "rejim_reject"
        for pair in list(cands)[:REJIM_REJECT_MAX_PER_TICK]:
            with _watch_lock:
                mevcut = _watch.get(pair.pool_address)
                if mevcut is not None:
                    engines = mevcut.setdefault("engines", {mevcut.get("engine")})
                    if motor in engines:
                        continue
                    engines.add(motor)
                elif len(_watch) >= WATCH_CAP:
                    continue
                else:
                    _watch[pair.pool_address] = {
                        "pair": pair.name,
                        "chain": pair.chain,
                        "pool_address": pair.pool_address,
                        "reason": neden,
                        "engine": motor,
                        "engines": {motor},
                        "price_at_reject": float(pair.price_usd),
                        "reject_ts": now,
                        "due_ts": now + REJECT_RECHECK_SEC,
                    }
            _rejects_yaz({
                "type": "reject",
                "reason": neden,
                "engine": motor,
                "pair": pair.name,
                "chain": pair.chain,
                "pool_address": pair.pool_address,
                "token_address": pair.token_address,
                "liquidity_usd": round(getattr(pair, "liquidity_usd", 0.0), 2),
                "chg_m5": round(getattr(pair, "chg_m5", 0.0), 2),
                "chg_h1": round(getattr(pair, "chg_h1", 0.0), 2),
                "price_usd": float(pair.price_usd),
                "sol_chg_h1": sol_h1,
            })
            yazilan = True
        if yazilan:
            _start_recheck_thread()
    except Exception:
        log.debug("rejim_reject kaydi hatasi", exc_info=True)


def bant_reject_kaydet(pair, motor: str, sol_h1: float | None) -> None:
    """h1 bant kacinmasi (orn. v7 20-40) adayi eledi: gorunur olcum satiri.

    Recheck kuyruguna girmez; dedup cagiran motorun sorumlulugudur.
    sol_h1 son olcumden gelir (fetch yok), None olabilir.
    """
    try:
        _rejects_yaz({
            "type": "reject",
            "reason": "h1_bant_skip",
            "engine": motor,
            "pair": pair.name,
            "chain": pair.chain,
            "pool_address": pair.pool_address,
            "token_address": pair.token_address,
            "liquidity_usd": round(getattr(pair, "liquidity_usd", 0.0), 2),
            "chg_m5": round(getattr(pair, "chg_m5", 0.0), 2),
            "chg_h1": round(getattr(pair, "chg_h1", 0.0), 2),
            "price_usd": float(getattr(pair, "price_usd", 0.0) or 0.0),
            "sol_chg_h1": sol_h1,
        })
    except Exception:
        log.debug("h1_bant_skip kaydi hatasi", exc_info=True)


class HuniSayac:
    """Gunluk giris-filtre hunisi: aday-yoklugu (plato) gunlerini olcer.

    Her tick'te sayilir, gun donunce tek satir ozet loglanir:
    kac aday goruldu, kacinin likiditesi yetti, kacinin h1 bandi tuttu.
    """

    def __init__(self, motor: str) -> None:
        self.motor = motor
        self._gun: str | None = None
        self._c: dict[str, int] = {}

    def ekle(self, goruldu: int, liq_ok: int, h1_ok: int,
             now: float | None = None) -> None:
        gun = time.strftime(
            "%Y-%m-%d", time.localtime(time.time() if now is None else now))
        if gun != self._gun:
            self.ozet_logla()
            self._gun = gun
            self._c = {"tick": 0, "goruldu": 0, "liq_ok": 0, "h1_ok": 0}
        self._c["tick"] += 1
        self._c["goruldu"] += goruldu
        self._c["liq_ok"] += liq_ok
        self._c["h1_ok"] += h1_ok

    def ozet_logla(self) -> None:
        if self._gun and self._c.get("tick"):
            log.warning(
                "%s HUNI OZET %s: tick=%d aday=%d liq_ok=%d h1_band_ok=%d",
                self.motor, self._gun, self._c["tick"], self._c["goruldu"],
                self._c["liq_ok"], self._c["h1_ok"],
            )


def _recheck_tick(client: httpx.Client, now: float | None = None) -> None:
    """Suresi gelen iptal adaylarini BIR kez fiyatla (kacirilan olculur)."""
    now = time.time() if now is None else now
    with _watch_lock:
        due = sorted(
            (w for w in _watch.values() if now >= w["due_ts"]),
            key=lambda w: w["due_ts"],
        )[:RECHECK_MAX_PER_TICK]
    for w in due:
        try:
            price = fetch_pool_price(client, w["chain"], w["pool_address"])
        except Exception:
            price = None
        chg = (
            round((price / w["price_at_reject"] - 1) * 100, 3)
            if price and w["price_at_reject"] > 0 else None
        )
        _rejects_yaz({
            "type": "recheck_30m",
            "reason": w["reason"],
            "engine": w["engine"],
            "pair": w["pair"],
            "chain": w["chain"],
            "pool_address": w["pool_address"],
            "reject_ts": round(w["reject_ts"], 3),
            "price_at_reject": w["price_at_reject"],
            "price_30m_later": price,
            "chg_30m_pct": chg,
        })
        with _watch_lock:
            _watch.pop(w["pool_address"], None)


def _run_recheck() -> None:
    with httpx.Client(timeout=10.0) as client:
        while True:
            time.sleep(RECHECK_POLL_SEC)
            try:
                _recheck_tick(client)
            except Exception:
                log.debug("taze_fiyat recheck hatasi", exc_info=True)


def _start_recheck_thread() -> None:
    global _recheck_thread
    with _watch_lock:
        if _recheck_thread is not None:
            return
        _recheck_thread = threading.Thread(
            target=_run_recheck, name="entry-fresh-recheck", daemon=True
        )
    _recheck_thread.start()
