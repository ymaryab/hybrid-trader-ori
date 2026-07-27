"""Forensic Factory: KARSILASTIRMA katmani (imza cikarimi).

Ne yapar: hedef kohort ile kontrol kohortunu ozellik ozellik kiyaslar ve
her ozellik icin dagilim-serbest bir ETKI BUYUKLUGU (Cliff delta) ile
gun-gun KARARLILIK uretir.

Ne YAPMAZ: esik aramaz, model uydurmaz, "en iyi ayrimi" secip
raporlamaz. Bu oturumda esigi veriden secip basari ilan etme hatasina
iki kez dustuk; fabrika bu yuzden yalniz betimler. Esik onerisi isteyen
ayri ve on-kayitli bir surectir.

Cliff delta yorumu (mutlak deger): <0.15 ihmal, 0.15-0.33 kucuk,
0.33-0.47 orta, >=0.47 buyuk.
"""

from __future__ import annotations

import time
from statistics import median

from . import ozellik


def _cliff(a: list[float], b: list[float]) -> float:
    """P(a>b) - P(a<b). O(n log n) siralama tabanli."""
    if not a or not b:
        return 0.0
    hepsi = sorted((v, 0) for v in a)
    hepsi += sorted((v, 1) for v in b)
    hepsi.sort()
    buyuk = kucuk = 0
    b_gorulen = 0
    i = 0
    n = len(hepsi)
    while i < n:
        j = i
        esit_a = esit_b = 0
        while j < n and hepsi[j][0] == hepsi[i][0]:
            if hepsi[j][1] == 0:
                esit_a += 1
            else:
                esit_b += 1
            j += 1
        buyuk += esit_a * b_gorulen
        b_gorulen += esit_b
        i = j
    toplam = len(a) * len(b)
    kucuk = toplam - buyuk - _esit_cift(a, b)
    return (buyuk - kucuk) / toplam


def _esit_cift(a: list[float], b: list[float]) -> int:
    from collections import Counter
    ca, cb = Counter(a), Counter(b)
    return sum(ca[v] * cb[v] for v in ca if v in cb)


def _gun(t: dict) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t["_giris_ts"]))


def _degerler(kume: list[dict], ad: str) -> tuple[list[float], int]:
    v, eksik = [], 0
    for t in kume:
        d = ozellik.hesapla(ad, t)
        if d is None:
            eksik += 1
        else:
            v.append(float(d))
    return v, eksik


def imza(hedef: list[dict], kontrol: list[dict],
         ozellikler: list[str] | None = None,
         min_n: int = 8) -> dict:
    """Kohort imzasi: ozellik basina etki buyuklugu + gunluk kararlilik."""
    ozellikler = ozellikler or (ozellik.giris_anI() + ozellik.sonrasi())
    satirlar = []
    gunler = sorted({_gun(t) for t in hedef})
    for ad in ozellikler:
        hv, h_eksik = _degerler(hedef, ad)
        kv, k_eksik = _degerler(kontrol, ad)
        if len(hv) < min_n or len(kv) < min_n:
            satirlar.append({"ozellik": ad, "durum": "yetersiz_n",
                             "hedef_n": len(hv), "kontrol_n": len(kv),
                             "zaman": ozellik.meta(ad)["zaman"]})
            continue
        d = _cliff(hv, kv)
        # gun-gun isaret kararliligi: kohortun o gunku uyeleri vs ayni gun kontrol
        isaretler = []
        for g in gunler:
            hg = [t for t in hedef if _gun(t) == g]
            kg = [t for t in kontrol if _gun(t) == g]
            hgv, _ = _degerler(hg, ad)
            kgv, _ = _degerler(kg, ad)
            if len(hgv) >= 3 and len(kgv) >= 3:
                isaretler.append(1 if _cliff(hgv, kgv) > 0 else -1)
        ayni = (sum(1 for s in isaretler if s == (1 if d > 0 else -1))
                if isaretler else 0)
        m = ozellik.meta(ad)
        satirlar.append({
            "ozellik": ad, "durum": "olculdu", "zaman": m["zaman"],
            "guven": m["guven"], "turev": list(m["turev"]),
            "kismi_alanlar": list(m["kismi_alanlar"]),
            "hedef_n": len(hv), "kontrol_n": len(kv),
            "hedef_medyan": round(median(hv), 4),
            "kontrol_medyan": round(median(kv), 4),
            "cliff_delta": round(d, 3),
            "buyukluk": _sinif(d),
            "gun_tutarliligi": f"{ayni}/{len(isaretler)}" if isaretler else "-",
            "eksik_hedef": h_eksik, "eksik_kontrol": k_eksik,
        })
    # Siralama: ONCE guvenilirlik sinifi (A>B>C), SONRA etki buyuklugu.
    # Boylece kismi/turev bir ozellik, tam ve bagimsiz bir ozelligin
    # onune yalnizca buyuk delta gosterdigi icin gecemez.
    olculen = [s for s in satirlar if s["durum"] == "olculdu"]
    olculen.sort(key=lambda s: (s.get("guven", "C"), -abs(s["cliff_delta"])))
    return {"satirlar": olculen + [s for s in satirlar if s["durum"] != "olculdu"],
            "hedef_n": len(hedef), "kontrol_n": len(kontrol),
            "gun_sayisi": len(gunler)}


def _sinif(d: float) -> str:
    a = abs(d)
    if a < 0.15:
        return "ihmal"
    if a < 0.33:
        return "kucuk"
    if a < 0.47:
        return "orta"
    return "buyuk"


def maliyet_ozeti(hedef: list[dict], tum: list[dict]) -> dict:
    """Kohortun karliliga maliyeti: ne kadarini o birkac islem yiyor."""
    h_usd = sum(t.get("pnl_usd") or 0 for t in hedef)
    t_usd = sum(t.get("pnl_usd") or 0 for t in tum)
    return {"kohort_pnl_usd": round(h_usd, 2),
            "evren_pnl_usd": round(t_usd, 2),
            "kohort_haric_pnl_usd": round(t_usd - h_usd, 2),
            "kohort_islem_payi_pct": round(100 * len(hedef) / max(len(tum), 1), 2)}
