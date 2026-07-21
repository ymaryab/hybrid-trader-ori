"""YZN1: YZ kopyasi + sonda A/B deney motoru."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.yzn1_session as yzn1


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr(yzn1, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None: (price, False))


def test_yz_ile_ayni_cekirdek_ve_izole_defter():
    import hibrit_trader.yz_session as yz
    assert yzn1.TP_PCT == yz.TP_PCT
    assert yzn1.TIMEOUT_MIN == yz.TIMEOUT_MIN
    assert yzn1.FELAKET_PCT == yz.FELAKET_PCT
    assert yzn1.CHG_H1_MIN == yz.CHG_H1_MIN and yzn1.CHG_H1_MAX == yz.CHG_H1_MAX
    assert yzn1.STATE_FILE == "yzn1_state.json"
    assert yzn1.SONDA_AKTIF is True and yzn1.SONDA_TEYIT_PCT == 1.0


def test_sonda_akisi():
    eng = yzn1.YZN1Engine(SimpleNamespace(scan_chains=("solana",)))
    eng.balance = 100.0
    now = time.time()
    p = {"pair": "T / SOL", "chain": "solana", "entry_price": 1.0,
         "last_price": 1.0, "opened_ts": now, "mfe_pct": 0.0, "mae_pct": 0.0,
         "amount_token": 10.0, "cost_usd": 10.0, "liq_entry": 200000.0,
         "sonda": True, "sonda_tam_usd": 30.0}
    assert eng._eval_position(p, 1.011, now) is None
    assert p["sonda_durum"] == "teyitli" and p["cost_usd"] == pytest.approx(30.0)
    p2 = dict(p, sonda=True, sonda_durum=None, cost_usd=10.0, amount_token=10.0, entry_price=1.0)
    assert eng._eval_position(p2, 0.979, now) == "sonda_kes"
