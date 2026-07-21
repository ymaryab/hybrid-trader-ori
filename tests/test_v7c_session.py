"""V7C: tp_2 + 30dk kosulsuz cikis (21 Tem kullanici karari)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v7c_session as v7c


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr(v7c, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None: (price, False))


def test_tp2_ve_30dk_kosulsuz_cikis():
    eng = v7c.V7CEngine(SimpleNamespace(scan_chains=("solana",)))
    now = time.time()

    def poz(yas_dk=0.0):
        return {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
                "opened_ts": now - yas_dk * 60, "mfe_pct": 0.0, "mae_pct": 0.0}

    assert eng._eval_position(poz(), 1.03, now) == "tp_2"
    assert eng._eval_position(poz(yas_dk=29), 0.7, now) is None
    assert eng._eval_position(poz(yas_dk=31), 0.7, now) == "timeout_30"
    assert eng._eval_position(poz(yas_dk=31), 1.01, now) == "timeout_30"
