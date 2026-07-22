"""V7NEW: v7hizli klonu, tek fark TP +%5 (22 Tem kullanici talebi)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v7new_session as v7n


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr(v7n, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None: (price, False))


def test_tp5_ve_kurallar():
    eng = v7n.V7NewEngine(SimpleNamespace(scan_chains=("solana",)))
    now = time.time()

    def poz(yas_dk=0.0):
        return {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
                "opened_ts": now - yas_dk * 60, "mfe_pct": 0.0, "mae_pct": 0.0}

    assert eng._eval_position(poz(), 1.03, now) is None            # +3: v7hizli satardi, v7new satmaz
    assert eng._eval_position(poz(), 1.06, now) == "tp_5"          # +6: TP
    assert eng._eval_position(poz(yas_dk=59), 0.85, now) is None   # -15: stop yok
    assert eng._eval_position(poz(yas_dk=5), 0.79, now) == "stop_felaket"
    assert eng._eval_position(poz(yas_dk=61), 0.9, now) == "timeout_60"


def test_tp_esigi_v7hizliden_bagimsiz():
    import hibrit_trader.v7hizli_session as v7h
    assert v7n.TP_PCT == 5.0
    assert v7h.TP_PCT == 2.0          # klon kaynagi degismedi
    assert v7n.STATE_FILE.startswith("v7new")
