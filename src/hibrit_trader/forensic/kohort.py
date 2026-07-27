"""Forensic Factory: KOHORT seciciler.

Fabrikanin sorusu "kotu islem nedir" degil, "karliligi bozan AZ SAYIDA
islem hangileri" oldugu icin varsayilan secici Pareto tabanlidir: gunun
zararinin buyuk kismini ureten kuyruk.

Genisletme: yeni secici eklemek = @kaydet ile isaretlenmis tek fonksiyon.
Secici (islemler, **kw) -> (hedef, kontrol) dondurur; ikisi de ayni
evrenden gelir, kesisimleri bostur.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

_SECICILER: dict[str, dict] = {}

# 28 Tem duzeltmesi: kohort hangi olcutle siralanir.
#   pct (VARSAYILAN) : yuzde kayip. Bilet buyuklugunden BAGIMSIZ.
#   usd (opsiyonel)  : dolar kayip. pnl_usd = pnl_pct * cost_usd oldugu
#                      icin buyuk biletleri MEKANIK olarak one alir; bu
#                      olcutle secilen kohortta "bilet buyuklugu" bir
#                      bulgu degil tautolojidir (olculdu: cliff +0.805
#                      usd ile, +0.090 pct ile).
_OLCUTLER = {
    "pct": lambda t: float(t.get("pnl_pct") or 0.0),
    "usd": lambda t: float(t.get("pnl_usd") or 0.0),
}
VARSAYILAN_OLCUT = "pct"


def _olcut_fn(olcut: str) -> Callable:
    if olcut not in _OLCUTLER:
        raise ValueError(f"bilinmeyen olcut: {olcut}; mevcut: {sorted(_OLCUTLER)}")
    return _OLCUTLER[olcut]


@dataclass
class Secim:
    """Kohort sonucu + hangi secici/olcutle uretildiginin damgasi."""
    hedef: list = field(default_factory=list)
    kontrol: list = field(default_factory=list)
    damga: dict = field(default_factory=dict)

    def __iter__(self):                     # hedef, kontrol = uygula(...)
        return iter((self.hedef, self.kontrol))


def kaydet(ad: str, aciklama: str) -> Callable:
    def sar(fn):
        _SECICILER[ad] = {"fn": fn, "aciklama": aciklama}
        return fn
    return sar


def liste() -> dict:
    return {k: v["aciklama"] for k, v in _SECICILER.items()}


def uygula(ad: str, islemler: list[dict], **kw) -> Secim:
    if ad not in _SECICILER:
        raise KeyError(f"bilinmeyen kohort: {ad}; mevcut: {sorted(_SECICILER)}")
    kw.setdefault("olcut", VARSAYILAN_OLCUT)
    _olcut_fn(kw["olcut"])                  # erken dogrulama
    hedef, kontrol = _SECICILER[ad]["fn"](islemler, **kw)
    kimlik = {id(t) for t in hedef}
    kontrol = [t for t in kontrol if id(t) not in kimlik]
    uyari = ("DIKKAT: dolar olcutu bilet buyuklugunu mekanik olarak one "
             "alir; 'bilet_log' ayrimi bu kosuda BULGU SAYILMAZ."
             if kw["olcut"] == "usd" else "")
    return Secim(hedef, kontrol,
                 {"secici": ad, "olcut": kw["olcut"],
                  "parametreler": {k: v for k, v in kw.items() if k != "olcut"},
                  "uyari": uyari})


def _gun(t: dict) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t["_giris_ts"]))


@kaydet("pareto_zarar",
        "Her gun, gunluk toplam zararin `pay` kadarini ureten en buyuk "
        "kayiplar (varsayilan %50). Kontrol: ayni gunun geri kalani.")
def pareto_zarar(islemler: list[dict], pay: float = 0.5,
                 olcut: str = VARSAYILAN_OLCUT) -> tuple[list, list]:
    f = _olcut_fn(olcut)
    hedef: list[dict] = []
    gunler: dict[str, list[dict]] = {}
    for t in islemler:
        gunler.setdefault(_gun(t), []).append(t)
    for _g, grup in gunler.items():
        kayiplar = sorted((t for t in grup if f(t) < 0), key=f)
        toplam = sum(f(t) for t in kayiplar)
        if not kayiplar or toplam == 0:
            continue
        birikim = 0.0
        for t in kayiplar:
            hedef.append(t)
            birikim += f(t)
            if birikim <= toplam * pay:
                break
    return hedef, list(islemler)


@kaydet("gunluk_en_kotu_n",
        "Her gunun en buyuk `n` zararli islemi (varsayilan 4). "
        "Kontrol: ayni gunun geri kalani.")
def gunluk_en_kotu_n(islemler: list[dict], n: int = 4,
                     olcut: str = VARSAYILAN_OLCUT) -> tuple[list, list]:
    f = _olcut_fn(olcut)
    hedef: list[dict] = []
    gunler: dict[str, list[dict]] = {}
    for t in islemler:
        gunler.setdefault(_gun(t), []).append(t)
    for _g, grup in gunler.items():
        hedef.extend(sorted(grup, key=f)[:n])
    return hedef, list(islemler)


@kaydet("esik_alti_pct",
        "pnl_pct <= `esik` olan islemler (varsayilan -15). "
        "Kontrol: esigin ustundekiler. (Zaten yuzde tabanli; olcut etkisiz.)")
def esik_alti_pct(islemler: list[dict], esik: float = -15.0,
                  olcut: str = VARSAYILAN_OLCUT) -> tuple[list, list]:
    hedef = [t for t in islemler if t["pnl_pct"] <= esik]
    return hedef, list(islemler)


@kaydet("cikis_nedeni",
        "Belirli cikis nedenleri (or. nedenler=('stop_felaket',)). "
        "Kontrol: digerleri. (Olcutten bagimsiz.)")
def cikis_nedeni(islemler: list[dict],
                 nedenler: tuple[str, ...] = ("stop_felaket",),
                 olcut: str = VARSAYILAN_OLCUT) -> tuple[list, list]:
    hedef = [t for t in islemler if t.get("exit_reason") in nedenler]
    return hedef, list(islemler)


@kaydet("katki_kuyrugu",
        "Tum pencerede, toplam zararin `pay` kadarini ureten en buyuk "
        "kayiplar (gun kirilimi yok). Kontrol: geri kalan her sey.")
def katki_kuyrugu(islemler: list[dict], pay: float = 0.5,
                  olcut: str = VARSAYILAN_OLCUT) -> tuple[list, list]:
    f = _olcut_fn(olcut)
    kayiplar = sorted((t for t in islemler if f(t) < 0), key=f)
    toplam = sum(f(t) for t in kayiplar)
    hedef: list[dict] = []
    birikim = 0.0
    for t in kayiplar:
        hedef.append(t)
        birikim += f(t)
        if toplam and birikim <= toplam * pay:
            break
    return hedef, list(islemler)
