"""Forensic Factory: OZELLIK kayit defteri.

Her ozellik, hangi alanlara dayandigini ve GIRIS aninda mi yoksa
sonradan mi bilinebildigini beyan etmek zorundadir. Bu beyan zorunlu,
cunku bu oturumda MFE gibi giristen SONRA olusan bir buyuklugu "giris
sinyali" gibi okuma hatasina dustuk; kayit defteri bunu yapisal olarak
ayirir ve rapor iki blogu asla karistirmaz.

Genisletme: yeni ozellik = tek dekorator.

    @kaydet("h1_m5_sapma", zaman="giris", alanlar=("chg_h1", "chg_m5"))
    def _(t): return (t.get("chg_h1") or 0) - 12*(t.get("chg_m5") or 0)

Deger None dondururse o satir O OZELLIK icin evren disidir (sifirla
DOLDURULMAZ; fabrika eksigi sayar ve raporlar).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from .veri import ALAN_SICILI, alan_kontrol

_OZELLIKLER: dict[str, dict] = {}


def kaydet(ad: str, zaman: str, alanlar: tuple[str, ...],
           aciklama: str = "", turev: tuple[str, ...] = ()) -> Callable:
    """Ozellik kaydi. zaman beyani KOD DENETIMIYLE dogrulanir.

    28 Tem kurali: bir ozellik 'giris' blogunda yer alabilmesi icin
    dayandigi TUM alanlarin sicilde damga="giris" olmasi sarttir. Cikista
    veya dolum sonrasi uretilen bir alan giris blogunda ILAN EDILEMEZ;
    denemek hata verir (tetik_gecikme bu kurala takildi).

    turev: bu ozellik baska ozelliklerin deterministik fonksiyonuysa
    onlarin adlari; rapor bagimsiz sinyal gibi okunmasin diye isaretler.
    """
    if zaman not in ("giris", "sonra"):
        raise ValueError("zaman 'giris' veya 'sonra' olmali")
    alan_kontrol(alanlar)                      # yok/bilinmeyen alan = hata
    if zaman == "giris":
        ihlal = [a for a in alanlar
                 if ALAN_SICILI[a].get("damga", "giris") != "giris"]
        if ihlal:
            raise ValueError(
                f"'{ad}' giris blogunda ilan edilemez: {ihlal} alanlari "
                "girisTEN SONRA uretiliyor (sicildeki damga). zaman='sonra' "
                "kullanin.")
    kismi = [a for a in alanlar
             if ALAN_SICILI[a]["durum"] in ("kismi", "supheli")]
    supheli = [a for a in alanlar if ALAN_SICILI[a]["durum"] == "supheli"]
    if supheli:
        guven = "C"
    elif kismi or turev:
        guven = "B"
    else:
        guven = "A"

    def sar(fn):
        _OZELLIKLER[ad] = {"fn": fn, "zaman": zaman, "alanlar": alanlar,
                           "aciklama": aciklama, "kismi_alanlar": tuple(kismi),
                           "turev": tuple(turev), "guven": guven}
        return fn
    return sar


GUVEN_ACIKLAMA = {
    "A": "tam dolu alan · karar aninda bilinir · bagimsiz",
    "B": "kismi dolu alan VEYA baska ozelliklerin turevi",
    "C": "degeri dogrulanmamis (supheli) alan",
}


def liste(zaman: str | None = None) -> dict:
    return {k: v for k, v in _OZELLIKLER.items()
            if zaman is None or v["zaman"] == zaman}


def giris_anI() -> list[str]:
    """Girişte bilinebilen ozellikler (karar kurmaya aday olanlar)."""
    return sorted(k for k, v in _OZELLIKLER.items() if v["zaman"] == "giris")


def sonrasi() -> list[str]:
    """Girişten sonra olusanlar: yalniz TESHIS icin, kural kurulamaz."""
    return sorted(k for k, v in _OZELLIKLER.items() if v["zaman"] == "sonra")


def hesapla(ad: str, t: dict):
    return _OZELLIKLER[ad]["fn"](t)


def meta(ad: str) -> dict:
    m = dict(_OZELLIKLER[ad])
    m.pop("fn")
    return m


def _f(t: dict, k: str):
    v = t.get(k)
    return None if v is None else float(v)


# ---- GIRIS ANI ozellikleri --------------------------------------------
@kaydet("h1", "giris", ("chg_h1",), "saatlik degisim")
def _(t): return _f(t, "chg_h1")


@kaydet("m5", "giris", ("chg_m5",), "5 dakikalik degisim")
def _(t): return _f(t, "chg_m5")


@kaydet("log_likidite", "giris", ("liq_entry",), "log10 giris likiditesi")
def _(t):
    v = _f(t, "liq_entry")
    return None if not v or v <= 0 else math.log10(v)


@kaydet("havuz_yasi_log", "giris", ("pool_yas_dk",), "log10 havuz yasi (dk)")
def _(t):
    v = _f(t, "pool_yas_dk")
    return None if v is None or v < 0 else math.log10(v + 1)


@kaydet("m5_h1_orani", "giris", ("chg_h1", "chg_m5"), "kisa/uzun ivme orani",
        turev=("h1", "m5"))
def _(t):
    h, m = _f(t, "chg_h1"), _f(t, "chg_m5")
    if h is None or m is None or abs(h) < 1:
        return None
    return m / h


@kaydet("h1_m5_sapma", "giris", ("chg_h1", "chg_m5"),
        "h1 ile m5'in dogrusal izdusumu arasindaki sapma",
        turev=("h1", "m5"))
def _(t):
    h, m = _f(t, "chg_h1"), _f(t, "chg_m5")
    return None if h is None or m is None else h - 12 * m


@kaydet("bilet_log", "giris", ("cost_usd",), "log10 bilet buyuklugu")
def _(t):
    v = _f(t, "cost_usd")
    return None if not v or v <= 0 else math.log10(v)


@kaydet("saat_utc", "giris", ("_giris_ts",), "giris saati (UTC, 0-23)")
def _(t):
    return float(time.gmtime(t["_giris_ts"]).tm_hour)


@kaydet("taze_fark", "giris", ("entry_fresh_fark_pct",),
        "KISMI ALAN (%58): giris dogrulamasinda hizli fiyat ile taze "
        "kotasyon farki (karar aninda bilinir)")
def _(t): return _f(t, "entry_fresh_fark_pct")


# ---- GIRISTEN SONRA olusanlar: yalniz teshis -------------------------
# 28 Tem: tetik_gecikme buraya TASINDI. Kod denetimi (v7hizli_session:692)
# bu degerin cikisi tetikleyen fiyat ornegi ile kapanis arasindaki gecikme
# oldugunu gosterdi; giris aninda mevcut degildir. Onceki "en kararli
# giris sinyali" okumasi bu yuzden gecersizdir.
@kaydet("tetik_gecikme", "sonra", ("tetik_gecikme_sec",),
        "CIKIS aninda uretilir: tetikleyen ornek ile kapanis arasi gecikme")
def _(t): return _f(t, "tetik_gecikme_sec")


@kaydet("friction", "sonra", ("friction_pct",),
        "dolum SONRASI: giris kaymasi + cikis kaymasi toplami")
def _(t): return _f(t, "friction_pct")


@kaydet("mfe", "sonra", ("mfe_pct",), "en yuksek lehte hareket")
def _(t): return _f(t, "mfe_pct")


@kaydet("mae", "sonra", ("mae_pct",), "en yuksek aleyhte hareket")
def _(t): return _f(t, "mae_pct")


@kaydet("hiç_artiya_gecmedi", "sonra", ("mfe_pct",),
        "MFE <= 0.01 (dogumdan olu pozisyon isareti)")
def _(t):
    v = _f(t, "mfe_pct")
    return None if v is None else (1.0 if v <= 0.01 else 0.0)


@kaydet("tutus_dk", "sonra", ("hold_sec",), "tutus suresi (dakika)")
def _(t):
    v = _f(t, "hold_sec")
    return None if v is None else v / 60.0


@kaydet("yurutme_maliyeti_puan", "sonra", ("karar_pnl_pct", "pnl_pct"),
        "karar fiyatina gore kaybedilen puan")
def _(t):
    k, p = _f(t, "karar_pnl_pct"), _f(t, "pnl_pct")
    return None if k is None or p is None else k - p
