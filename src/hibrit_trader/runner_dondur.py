"""Runner ailesi dondurma anahtari (27 Tem, kullanici karari).

TEK AYAR: RUNNER_DONDUR=1  (systemd drop-in). Kapatmak icin satiri sil.

Dondurulan ne DEMEK:
  - CANLI secim havuzundan cikar (Edge + Governor aday olarak gormez).
  - Paper AYNEN calisir: kadans, kurallar, dolumlar degismez.
  - Gozlem/metrik/counterfactual kayitlari AYNEN akar (tam-evren
    cekirdegi runner'i degerlendirmeye devam eder, aday_tam yazilir).
  - Panel: dondurulmus motorlarin equity/ozet yanitlari kisa TTL ile
    onbelleklenir (grafik en fazla TTL kadar bayat; alim-satima etkisi
    YOK). En buyuk runner-kaynakli CPU kalemi budur.

Bilerek YAPILMAYAN: paper dongu kadansini yavaslatmak veya thread
onceligini dusurmek. Ikisi de r1/r2'nin olculen performansini
degistirir ve tam da korunmasi istenen counterfactual/GO verisini
kirletir; ~%1.4 CPU icin veri butunlugu feda edilmez.
"""

from __future__ import annotations

import os
import time

AILE = "runner"
MOTORLAR = ("r1", "r2")
PANEL_TTL_SN = float(os.getenv("RUNNER_DONDUR_PANEL_TTL", "15"))

_onbellek: dict = {}


def aktif() -> bool:
    return os.getenv("RUNNER_DONDUR", "").strip() in ("1", "true", "True")


def donduruldu(motor: str | None) -> bool:
    """Motor dondurulmus mu (panel/olcum tarafi icin)."""
    return bool(motor) and aktif() and motor in MOTORLAR


def panel_onbellek(anahtar: str, uret):
    """Dondurulmus motorlar icin TTL onbellek; digerlerinde birebir gecis.

    uret(): pahali hesap (tum trades dosyasini okuyup ayristirir).
    """
    simdi = time.time()
    kayit = _onbellek.get(anahtar)
    if kayit is not None and simdi - kayit[0] <= PANEL_TTL_SN:
        return kayit[1]
    deger = uret()
    _onbellek[anahtar] = (simdi, deger)
    if len(_onbellek) > 64:          # sinirli sozluk: sizinti olmasin
        for k in [k for k, v in _onbellek.items()
                  if simdi - v[0] > PANEL_TTL_SN * 4]:
            _onbellek.pop(k, None)
    return deger
