"""Fiyat sanity bandi: tek adimda 5x+ sapma = veri arizasi (ORCA vakasi, 09 Tem).

DexScreener ORCA'nin en likit havuzunda priceUsd'yi ~6000x sapik basti; motorlar
bunu gercek fiyat sanip once sapik fiyattan giris acti, fiyat "duzelince" de
stop_felaket ile kaybi realize etti. Ders: fiyat kaynagina kor guven yok.

Kural: yeni fiyat, pozisyonun son gecerli fiyatina gore tek adimda MAX_STEP_RATIO
kattan fazla sapmissa o tick veri arizasidir. Islem tetiklenmez, degerleme son
gecerli fiyatla surer, ariza loglanir. Sapma REBASE_SEC boyunca kesintisiz
surerse re-base DENENIR ama Jupiter hakem onayina baglidir (JTO/PYTH vakasi,
09 Tem aksam): hakem yeni fiyati MAX_STEP_RATIO icinde dogrulamazsa re-base
yok, degerleme son gecerli fiyatta kalir ve pencere bastan baslar. Hakem
ulasilamazsa da re-base yok (fail-closed).
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

log = logging.getLogger(__name__)

MAX_STEP_RATIO = float(os.getenv("PRICE_SANITY_MAX_RATIO", "5"))
REBASE_SEC = float(os.getenv("PRICE_SANITY_REBASE_SEC", "300"))

Hakem = Callable[[str], Optional[float]]


def _jupiter_hakem(token_address: str) -> float | None:
    from hibrit_trader.broker import jupiter_referans_fiyat
    return jupiter_referans_fiyat(token_address)


def guard_price(pos: dict, price: float, now: float, engine: str,
                hakem: Hakem = _jupiter_hakem) -> tuple[float, bool]:
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
        ref = None
        token = pos.get("token_address")
        if token:
            try:
                ref = hakem(token)
            except Exception as e:
                log.warning("%s VERI ARIZASI %s: hakem hatasi (%s)",
                            engine, pos.get("pair"), e)
        if ref is not None and ref > 0 and (
                max(price / ref, ref / price) <= MAX_STEP_RATIO):
            pos.pop("veri_ariza_ts", None)
            log.warning(
                "%s VERI ARIZASI %s: sapma %.0fs surdu, hakem (%.8g) onayladi, "
                "yeni taban kabul: %.8g (eski %.8g)",
                engine, pos.get("pair"), now - since, ref, price, last,
            )
            return price, False
        # hakem onaylamadi ya da ulasilamadi: re-base yok, pencere bastan
        pos["veri_ariza_ts"] = now
        log.warning(
            "%s VERI ARIZASI %s: re-base hakem onayi alamadi (fiyat %.8g, "
            "hakem %s), degerleme son gecerli fiyatta (%.8g)",
            engine, pos.get("pair"), price,
            f"{ref:.8g}" if ref else "yok", last,
        )
        return last, True
    return last, True
