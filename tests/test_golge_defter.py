"""Golge-defter KPI araci: eslestirme, gecis maliyeti, CASH, IC."""

import importlib.util
import sys
from pathlib import Path

_kok = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "golge_defter", _kok / "scripts" / "golge_defter.py")
gd = importlib.util.module_from_spec(_spec)
sys.modules["golge_defter"] = gd
_spec.loader.exec_module(gd)


import pytest


def test_spearman_temel():
    assert gd._spearman([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert gd._spearman([1, 2, 3], [30, 20, 10]) == pytest.approx(-1.0)
    assert gd._spearman([1, 1, 1], [1, 2, 3]) is None


def test_hesapla_eslestirme_ve_maliyet():
    turlar = [
        (0.0, "r2", "v7", {"r2": 2.0, "v7": 1.0, "yz": 0.5}),
        (300.0, "r2", "v7", {"r2": 2.0, "v7": 1.0, "yz": 0.5}),
        (600.0, None, "v7", {"r2": -1.0, "v7": -2.0, "yz": -3.0}),
        (900.0, None, "v7", {}),                        # son tur: pencere kapatir
    ]
    islemler = {
        "r2": [(100.0, 5.0), (400.0, -1.0), (700.0, 3.0)],
        "v7": [(150.0, 1.0), (450.0, 1.0), (750.0, -2.0)],
        "yz": [(160.0, 0.5), (460.0, 0.2)],
    }
    r = gd.hesapla(turlar, islemler)
    # tur1: r2(5) - v7(1) = +4 ; tur2: r2(-1) - v7(1) = -2 (ayni karar)
    # tur3: CASH(0) - v7(-2) = +2 - 1.50 maliyet = +0.5
    assert r["tur_n"] == 3
    assert r["gecis_n"] == 1
    assert abs(r["toplam_fark_usd"] - (4 - 2 + 0.5)) < 1e-9
    assert r["pozitif_pencere"] == 2 and r["negatif_pencere"] == 1
    assert r["ic_n"] >= 2                     # ilk iki turda tam siralama


def test_cash_penceresi_sifir():
    turlar = [(0.0, None, "v7", {}), (300.0, None, "v7", {})]
    islemler = {"v7": [(100.0, -5.0)]}
    r = gd.hesapla(turlar, islemler)
    assert r["toplam_fark_usd"] == 5.0        # CASH 0 - (-5) = +5, maliyet yok
