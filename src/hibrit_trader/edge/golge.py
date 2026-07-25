"""Edge zinciri GOLGE degerlendirmesi (HAT 2, 25 Tem). SAF fonksiyon.

Amac: yeni mimarinin v1 karar zincirini (edge sozlesmesi + tahsis)
mevcut seciciyle AYNI girdiler uzerinde, AYNI anda kosup kiyaslamak.
Karar akisina dokunmaz, yan etkisi yoktur; cikti seciciden
EdgeShadowEvaluated olayiyla omurgaya yazilir.

v1 siniri (durustluk): edge vekili = kayan pencere pct (legacy ile ayni
girdi). Legacy'nin ek kurallari (min islem, min kasa, egim/marj/veto,
firsat sarti, cooldown) golgede YOK; sapmalarin cogu bu kural farkindan
beklenir ve sapma_nedeni alaniyla ayristirilir. Kill-bataryasi gecerse
vekil yerine kosullama-temelli edge takilir, kiyas ayni olayla surer.
"""

from __future__ import annotations

from .tahsis import HepsiLidere


def golge_degerlendir(skorlar: dict[str, dict], mevcut: str,
                      legacy_karar: str, legacy_aday: str | None,
                      esik: float = 0.0) -> dict:
    """Ayni skor sozlugunden edge-zinciri v1 karari uret ve kiyasla.

    legacy_karar: kal|cooldown|firsat_yok|gecis|sistem_kapali|otonom_kapali
    legacy_aday : aday_sec ciktisi (None = uygun aday yok)
    """
    edgeler = {m: float(s.get("pct") or 0.0) for m, s in skorlar.items()}
    paylar = HepsiLidere(esik=esik).dagit(edgeler)
    golge_aday = next(iter(paylar), None)          # None = salter iner
    legacy_hedef = legacy_aday if legacy_karar == "gecis" else (
        mevcut if legacy_karar in ("kal", "cooldown", "firsat_yok",
                                   "otonom_kapali")
        else None)                                 # sistem_kapali -> hic
    uyum = golge_aday == legacy_hedef
    if uyum:
        neden = None
    elif golge_aday is None:
        neden = "golge_salter"                     # golge: pozitif edge yok
    elif legacy_hedef is None:
        neden = "legacy_salter"                    # legacy sistem kapali
    elif legacy_karar == "otonom_kapali":
        neden = "legacy_pasif"                     # secici kapali, kiyas bilgi
    elif legacy_karar in ("cooldown", "firsat_yok"):
        neden = f"legacy_{legacy_karar}"           # kural farki (beklenen)
    elif legacy_karar == "kal" and legacy_aday is None:
        neden = "legacy_filtre"                    # esik/min_islem/kasa/veto
    else:
        neden = "siralama_farki"                   # ayni evren, farkli lider
    return {"edgeler": {m: round(e, 3) for m, e in edgeler.items()},
            "paylar": paylar, "golge_aday": golge_aday,
            "legacy_karar": legacy_karar, "legacy_aday": legacy_aday,
            "legacy_hedef": legacy_hedef, "mevcut": mevcut,
            "uyum": uyum, "sapma_nedeni": neden}
