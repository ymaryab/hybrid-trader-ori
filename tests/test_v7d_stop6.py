"""V7D stop_6: -6'yi goren HER AN satilir (23 Tem kullanici karari)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v7d_session as v7d


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr(v7d, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None: (price, False))


def test_stop6_siralamasi():
    eng = v7d.V7DEngine(SimpleNamespace(scan_chains=("solana",)))
    now = time.time()

    def poz(yas_sn=60):
        return {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
                "opened_ts": now - yas_sn, "mfe_pct": 0.0, "mae_pct": 0.0}

    assert eng._eval_position(poz(), 0.939, now) == "stop_6"      # -6.1: erken fazda da satar
    assert eng._eval_position(poz(), 0.941, now) is None          # -5.9: dokunmaz (grace icinde)
    assert eng._eval_position(poz(), 0.84, now) == "stop_6"       # -16: felaket kalkti, stop_6 yakalar
    assert eng._eval_position(poz(yas_sn=v7d.GRACE_SEC + 1), 0.975, now) == "stop_gec"  # gec faz -2.5
    assert eng._eval_position(poz(), 1.025, now) == "tp_2"
