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

_SECICILER: dict[str, dict] = {}


def kaydet(ad: str, aciklama: str) -> Callable:
    def sar(fn):
        _SECICILER[ad] = {"fn": fn, "aciklama": aciklama}
        return fn
    return sar


def liste() -> dict:
    return {k: v["aciklama"] for k, v in _SECICILER.items()}


def uygula(ad: str, islemler: list[dict], **kw) -> tuple[list[dict], list[dict]]:
    if ad not in _SECICILER:
        raise KeyError(f"bilinmeyen kohort: {ad}; mevcut: {sorted(_SECICILER)}")
    hedef, kontrol = _SECICILER[ad]["fn"](islemler, **kw)
    kimlik = {id(t) for t in hedef}
    kontrol = [t for t in kontrol if id(t) not in kimlik]
    return hedef, kontrol


def _gun(t: dict) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t["_giris_ts"]))


@kaydet("pareto_zarar",
        "Her gun, gunluk toplam zararin `pay` kadarini ureten en buyuk "
        "kayiplar (varsayilan %50). Kontrol: ayni gunun geri kalani.")
def pareto_zarar(islemler: list[dict], pay: float = 0.5) -> tuple[list, list]:
    hedef: list[dict] = []
    gunler: dict[str, list[dict]] = {}
    for t in islemler:
        gunler.setdefault(_gun(t), []).append(t)
    for _g, grup in gunler.items():
        kayiplar = sorted((t for t in grup if (t.get("pnl_usd") or 0) < 0),
                          key=lambda t: t["pnl_usd"])
        toplam = sum(t["pnl_usd"] for t in kayiplar)
        if not kayiplar or toplam == 0:
            continue
        birikim = 0.0
        for t in kayiplar:
            hedef.append(t)
            birikim += t["pnl_usd"]
            if birikim <= toplam * pay:
                break
    return hedef, list(islemler)


@kaydet("gunluk_en_kotu_n",
        "Her gunun en buyuk `n` zararli islemi (varsayilan 4). "
        "Kontrol: ayni gunun geri kalani.")
def gunluk_en_kotu_n(islemler: list[dict], n: int = 4) -> tuple[list, list]:
    hedef: list[dict] = []
    gunler: dict[str, list[dict]] = {}
    for t in islemler:
        gunler.setdefault(_gun(t), []).append(t)
    for _g, grup in gunler.items():
        hedef.extend(sorted(grup, key=lambda t: t.get("pnl_usd") or 0)[:n])
    return hedef, list(islemler)


@kaydet("esik_alti_pct",
        "pnl_pct <= `esik` olan islemler (varsayilan -15). "
        "Kontrol: esigin ustundekiler.")
def esik_alti_pct(islemler: list[dict], esik: float = -15.0) -> tuple[list, list]:
    hedef = [t for t in islemler if t["pnl_pct"] <= esik]
    return hedef, list(islemler)


@kaydet("cikis_nedeni",
        "Belirli cikis nedenleri (or. nedenler=('stop_felaket',)). "
        "Kontrol: digerleri.")
def cikis_nedeni(islemler: list[dict],
                 nedenler: tuple[str, ...] = ("stop_felaket",)) -> tuple[list, list]:
    hedef = [t for t in islemler if t.get("exit_reason") in nedenler]
    return hedef, list(islemler)


@kaydet("katki_kuyrugu",
        "Tum pencerede, toplam zararin `pay` kadarini ureten en buyuk "
        "kayiplar (gun kirilimi yok). Kontrol: geri kalan her sey.")
def katki_kuyrugu(islemler: list[dict], pay: float = 0.5) -> tuple[list, list]:
    kayiplar = sorted((t for t in islemler if (t.get("pnl_usd") or 0) < 0),
                      key=lambda t: t["pnl_usd"])
    toplam = sum(t["pnl_usd"] for t in kayiplar)
    hedef: list[dict] = []
    birikim = 0.0
    for t in kayiplar:
        hedef.append(t)
        birikim += t["pnl_usd"]
        if toplam and birikim <= toplam * pay:
            break
    return hedef, list(islemler)
