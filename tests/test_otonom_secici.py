"""Otonom kaynak secici: kayan pencere degisimi, zirvede-kal, tasfiye."""

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


def _kur(d: Path, motor: str, start=1000.0, created=0.0,
         trades=(), equity=()):
    (d / f"{motor}_state.json").write_text(json.dumps(
        {"start_balance": start, "created_ts": created}))
    with open(d / f"{motor}_trades.jsonl", "w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")
    if equity:
        with open(d / f"{motor}_equity.jsonl", "w") as f:
            for e in equity:
                f.write(json.dumps(e) + "\n")


def test_kayan_degisim_kullanici_ornegi(tmp_path):
    """13:00'da 1010 olan motor 12:00'da 1000 idi -> +%1."""
    now = time.time()
    _kur(tmp_path, "yz",
         trades=[{"ts": now - 90 * 60, "trade_id": "A", "pnl_usd": 0.0},
                 {"ts": now - 30 * 60, "trade_id": "B", "pnl_usd": 10.0}],
         equity=[{"ts": now - 61 * 60, "eq": 1000.0}])
    s = osec.kayan_degisim("yz", 60)
    assert abs(s["pct"] - 1.0) < 1e-6      # 1010/1000-1
    assert s["islem"] == 1                  # pencere icinde 1 islem


def test_kayan_degisim_islemsiz_sifir(tmp_path):
    now = time.time()
    _kur(tmp_path, "yz",
         trades=[{"ts": now - 120 * 60, "trade_id": "A", "pnl_usd": 50.0}],
         equity=[{"ts": now - 61 * 60, "eq": 1050.0}])
    s = osec.kayan_degisim("yz", 60)
    assert s["pct"] == 0.0 and s["islem"] == 0   # son saatte hareket yok


def test_aday_sec_zirvede_kal():
    sk = {"yz": {"pct": 2.5, "islem": 4},
          "r1": {"pct": 1.0, "islem": 2},
          "r2": {"pct": -3.0, "islem": 5}}     # negatif: elenir
    assert osec.aday_sec(sk, "r1", min_islem=0) == "yz"     # zirveye gec
    assert osec.aday_sec(sk, "yz", min_islem=0) is None     # ZIRVEDE KAL
    hepsi_neg = {"yz": {"pct": -1.0, "islem": 9}}
    assert osec.aday_sec(hepsi_neg, "r1", min_islem=0) is None
    assert osec.aday_sec({}, "r1") is None


def test_durum_dosyasi_varsayilan_60dk(tmp_path):
    d = osec.durum_oku()
    assert d["acik"] is False and d["pencere_dk"] == 60
    d["acik"] = True
    d["pencere_dk"] = 45
    osec.durum_yaz(d)
    assert osec.durum_oku()["pencere_dk"] == 45


def test_tasfiye_kancasi(tmp_path):
    import hibrit_trader.canli_session as cs
    assert cs.tasfiye_talebi_var() is False
    (tmp_path / cs.TASFIYE_FILE).write_text("test")
    assert cs.tasfiye_talebi_var() is True
