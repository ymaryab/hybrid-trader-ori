"""V7T: tp_2 + 20dk kosulsuz cikis (21 Tem kullanici karari)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v7t_session as v7t


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr(v7t, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None: (price, False))


def test_tp2_ve_20dk_kosulsuz_cikis():
    eng = v7t.V7TEngine(SimpleNamespace(scan_chains=("solana",)))
    now = time.time()

    def poz(yas_dk=0.0):
        return {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
                "opened_ts": now - yas_dk * 60, "mfe_pct": 0.0, "mae_pct": 0.0}

    assert eng._eval_position(poz(), 1.03, now) == "tp_2"
    assert eng._eval_position(poz(yas_dk=19), 0.7, now) is None
    assert eng._eval_position(poz(yas_dk=21), 0.7, now) == "timeout_20"
    assert eng._eval_position(poz(yas_dk=21), 1.01, now) == "timeout_20"
