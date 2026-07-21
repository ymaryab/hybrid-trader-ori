"""YZ motoru: damitilmis cekirdek (TP+2 + 60dk giyotin + -20 kapak)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.yz_session as yz


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr(yz, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None: (price, False))


def test_cekirdek_kurallari():
    import hibrit_trader.v7hizli_session as v7h
    assert yz.TP_PCT == v7h.TP_PCT
    assert yz.CHG_H1_MIN == v7h.CHG_H1_MIN and yz.CHG_H1_MAX == v7h.CHG_H1_MAX
    assert yz.LIQ_MIN_USD == v7h.LIQ_MIN_USD
    assert yz.TIMEOUT_MIN == 60.0 and yz.FELAKET_PCT == -20.0
    assert yz.STATE_FILE == "yz_state.json"  # defter izolasyonu


def test_uc_cikis_kapisi():
    eng = yz.YZEngine(SimpleNamespace(scan_chains=("solana",)))
    now = time.time()

    def poz(yas_dk=0.0):
        return {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
                "opened_ts": now - yas_dk * 60, "mfe_pct": 0.0, "mae_pct": 0.0}

    assert eng._eval_position(poz(), 1.03, now) == "tp_2"
    assert eng._eval_position(poz(), 0.85, now) is None          # -15: kapak alti
    assert eng._eval_position(poz(), 0.79, now) == "stop_felaket"  # -21: kapak
    assert eng._eval_position(poz(yas_dk=61), 0.9, now) == "timeout_60"


def test_sonda_teyit_ve_kes(monkeypatch):
    # replay birebir: 1/3 sonda; +1 teyitte kalan alinir; teyitsiz -2 kes
    eng = yz.YZEngine(SimpleNamespace(scan_chains=("solana",)))
    eng.balance = 100.0
    now = time.time()

    def sonda_poz():
        return {"pair": "T / SOL", "chain": "solana", "entry_price": 1.0,
                "last_price": 1.0, "opened_ts": now, "mfe_pct": 0.0,
                "mae_pct": 0.0, "amount_token": 10.0, "cost_usd": 10.0,
                "liq_entry": 200000.0, "sonda": True, "sonda_tam_usd": 30.0}

    p = sonda_poz()
    assert eng._eval_position(p, 1.011, now) is None  # teyit: olceklendi, tp degil
    assert p["sonda"] is False and p["sonda_durum"] == "teyitli"
    assert p["cost_usd"] == pytest.approx(30.0)
    assert p["entry_price"] > 1.0  # harmanli giris
    p2 = sonda_poz()
    assert eng._eval_position(p2, 0.979, now) == "sonda_kes"
    p3 = sonda_poz(); eng.balance = 5.0
    assert eng._eval_position(p3, 1.011, now) is None
    assert p3["sonda_durum"] == "olceklenemedi" and p3["cost_usd"] == 10.0
