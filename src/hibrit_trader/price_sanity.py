"""Fiyat sanity bandi: tek adimda 5x+ sapma = veri arizasi (ORCA vakasi, 09 Tem).

DexScreener ORCA'nin en likit havuzunda priceUsd'yi ~6000x sapik basti; motorlar
bunu gercek fiyat sanip once sapik fiyattan giris acti, fiyat "duzelince" de
stop_felaket ile kaybi realize etti. Ders: fiyat kaynagina kor guven yok.

Kural: yeni fiyat, pozisyonun son gecerli fiyatina gore tek adimda MAX_STEP_RATIO
kattan fazla sapmissa o tick veri arizasidir. Islem tetiklenmez, degerleme son
gecerli fiyatla surer, ariza loglanir. Sapma REBASE_SEC boyunca kesintisiz
surerse yeni seviye taban kabul edilir (kaynak kalici re-base olmus demektir);
boylece motor sonsuza kadar kor kalmaz.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

MAX_STEP_RATIO = float(os.getenv("PRICE_SANITY_MAX_RATIO", "5"))
REBASE_SEC = float(os.getenv("PRICE_SANITY_REBASE_SEC", "300"))


def guard_price(pos: dict, price: float, now: float, engine: str) -> tuple[float, bool]:
    """Yeni fiyati pozisyonun son gecerli fiyatina karsi dogrula.

    Donus: (kullanilacak_fiyat, ariza_mi). Ariza durumunda son gecerli fiyat
    doner; caller o tick'te islem tetiklememeli. Ariza takibi pos icinde
    "veri_ariza_ts" alaniyla tutulur (state'e persist olur, restart dayanir).
    """
    last = float(pos.get("last_price") or 0)
    if price <= 0 or last <= 0:
        return price, False
    ratio = price / last if price >= last else last / price
    if ratio <= MAX_STEP_RATIO:
        if pos.pop("veri_ariza_ts", None) is not None:
            log.warning("%s VERI ARIZASI %s: fiyat normale dondu (%.8g)",
                        engine, pos.get("pair"), price)
        return price, False
    since = pos.get("veri_ariza_ts")
    if since is None:
        pos["veri_ariza_ts"] = now
        log.warning(
            "%s VERI ARIZASI %s: fiyat %.8g son gecerliden (%.8g) %.0fx sapik, "
            "tick yok sayildi (degerleme son gecerli fiyatla)",
            engine, pos.get("pair"), price, last, ratio,
        )
        return last, True
    if now - since >= REBASE_SEC:
        pos.pop("veri_ariza_ts", None)
        log.warning(
            "%s VERI ARIZASI %s: sapma %.0fs kesintisiz surdu, yeni taban kabul: "
            "%.8g (eski %.8g)",
            engine, pos.get("pair"), now - since, price, last,
        )
        return price, False
    return last, True
