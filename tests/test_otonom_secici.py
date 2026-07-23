"""Otonom kaynak secici: pencere skorlari, aday secimi, tasfiye kancasi."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import hibrit_trader.otonom_secici as osec


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    yield tmp_path


def _defter_yaz(d: Path, motor: str, satirlar):
    with open(d / f"{motor}_trades.jsonl", "w") as f:
        for t in satirlar:
            f.write(json.dumps(t) + "\n")


def test_pencere_skorlari(tmp_path):
    now = time.time()
    _defter_yaz(tmp_path, "yz", [
        {"ts": now - 60, "trade_id": "A", "pnl_usd": 5.0},
        {"ts": now - 120, "trade_id": "B", "pnl_usd": -2.0},
        {"ts": now - 999999, "trade_id": "ESKI", "pnl_usd": 100.0},  # pencere disi
        {"ts": now - 30, "type": "kural_degisim"},                    # tip satiri
        {"ts": now - 30, "trade_id": "C", "pnl_usd": 1.0,
         "exit_reason": "manuel_kapanis"},                            # manuel haric
    ])
    s = osec.pencere_skorlari(120, ["yz", "yok"])
    assert s["yz"] == {"pnl": 3.0, "islem": 2}
    assert "yok" not in s


def test_aday_sec_kurallari():
    sk = {"yz": {"pnl": 10.0, "islem": 5},
          "r1": {"pnl": 4.0, "islem": 8},
          "v7": {"pnl": 20.0, "islem": 2},    # islem siniri altinda
          "r2": {"pnl": -9.0, "islem": 9}}    # negatif
    assert osec.aday_sec(sk, "r1", min_islem=3) == "yz"
    assert osec.aday_sec(sk, "yz", min_islem=3) is None      # zaten en iyi
    assert osec.aday_sec({}, "r1") is None                   # veri yok
    hepsi_neg = {"yz": {"pnl": -1.0, "islem": 9}}
    assert osec.aday_sec(hepsi_neg, "r1") is None            # kazanan yok: kal


def test_durum_dosyasi(tmp_path):
    d = osec.durum_oku()
    assert d["acik"] is False and d["pencere_dk"] == 120
    d["acik"] = True
    d["pencere_dk"] = 45
    osec.durum_yaz(d)
    d2 = osec.durum_oku()
    assert d2["acik"] is True and d2["pencere_dk"] == 45


def test_tasfiye_kancasi(tmp_path, monkeypatch):
    import hibrit_trader.canli_session as cs
    assert cs.tasfiye_talebi_var() is False
    (tmp_path / cs.TASFIYE_FILE).write_text("test")
    assert cs.tasfiye_talebi_var() is True
