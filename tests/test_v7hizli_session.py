"""V7HIZLI: tp_2 + 60dk kosulsuz cikis (21 Tem kullanici karari)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v7hizli_session as v7h


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr(v7h, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None: (price, False))


def test_tp2_ve_60dk_kosulsuz_cikis():
    eng = v7h.V7HizliEngine(SimpleNamespace(scan_chains=("solana",)))
    now = time.time()

    def poz(yas_dk=0.0):
        return {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
                "opened_ts": now - yas_dk * 60, "mfe_pct": 0.0, "mae_pct": 0.0}

    assert eng._eval_position(poz(), 1.03, now) == "tp_2"
    assert eng._eval_position(poz(yas_dk=59), 0.85, now) is None     # -15: kapak alti, stop yok
    assert eng._eval_position(poz(yas_dk=5), 0.79, now) == "stop_felaket"  # -21: kuyruk kapagi
    assert eng._eval_position(poz(yas_dk=61), 0.9, now) == "timeout_60"
    assert eng._eval_position(poz(yas_dk=61), 1.01, now) == "timeout_60"
