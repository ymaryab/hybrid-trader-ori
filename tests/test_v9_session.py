"""V9 senaryo motoru testleri: v7 birebir + TEK fark likidite tabani $300k."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v9_session as v9
from hibrit_trader.v9_session import V9Engine


@pytest.fixture(autouse=True)
def v9_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="ZP1", token="ZT1", price=1.0, liq=350_000.0, h1=15.0, m5=-2.0):
    return SimpleNamespace(
        name="Z / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(v9, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        v9, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(v9.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- TEK fark: likidite tabani $300k ------------------------------------------------

def test_entry_rejects_liq_below_300k(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=299_000)) == []
    assert _enter(eng, monkeypatch, _pair(liq=150_000)) == []  # v7 kabul ederdi


def test_entry_accepts_liq_at_300k(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(liq=300_000))) == 1


# ---- v7 bandi korunuyor: h1 10..50 --------------------------------------------------

def test_entry_rejects_h1_above_50(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=459658.0)) == []
    assert _enter(eng, monkeypatch, _pair(h1=50.1)) == []


def test_entry_accepts_h1_band_edges(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(h1=10.0))) == 1
    assert len(_enter(eng, monkeypatch, _pair(pool="ZP2", token="ZT2", h1=50.0))) == 2


def test_entry_rejects_h1_below_10(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=9.9)) == []


# ---- v7'den korunanlar ----------------------------------------------------------------

def test_entry_v7_rules_preserved(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []       # rejim < 0
    assert len(_enter(eng, monkeypatch, _pair(h1=45.0, m5=-5.0))) == 1  # m5 sarti yok


def test_candidates_sorted_highest_h1_first(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    low = _pair(pool="PL", token="TL", h1=12.0)
    high = _pair(pool="PH", token="TH", h1=40.0)
    positions = _enter(eng, monkeypatch, [low, high])
    assert positions[0]["pool_address"] == "PH"


def test_entry_keeps_cooldown(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    eng._cooldown_until["ZT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- Cikislar (v7 birebir) -------------------------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(v9, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(v9.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / v9.TRADES_FILE).read_text().splitlines()[-1])


def test_tp_2(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)
    t = _last(v9_data_dir)
    assert t["exit_reason"] == "tp_2"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + v9.COOLDOWN_EXIT_SEC
    )


def test_patience_holds_above_brake(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    pos = _open(eng)
    # -%9.9: fren tetiklenmez, sabir tutar (v7 ile ayni)
    _tick_price(eng, pos, pos["entry_price"] * 0.901, pos["opened_ts"] + v9.GRACE_SEC - 1, monkeypatch)
    assert eng.positions == [pos]


def test_brake_fires_at_minus_10(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # ilk 30dk icinde -%11 -> sabir iptal, aninda stop_felaket (v7 ile ayni)
    _tick_price(eng, pos, pos["entry_price"] * 0.89, t0 + 60, monkeypatch)
    assert eng.positions == []
    t = _last(v9_data_dir)
    assert t["exit_reason"] == "stop_felaket"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + v9.COOLDOWN_LOSS_SEC
    )


def test_late_stop_after_30min(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.97, t0 + v9.GRACE_SEC + 1, monkeypatch)
    t = _last(v9_data_dir)
    assert t["exit_reason"] == "stop_gec"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + v9.GRACE_SEC + 1 + v9.COOLDOWN_LOSS_SEC
    )


def test_timeout_60(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.005, pos["opened_ts"] + v9.CEILING_SEC + 1, monkeypatch)
    assert _last(v9_data_dir)["exit_reason"] == "timeout_60"


# ---- sol_h1 kaydi + tam set + izolasyon (v7 ile ayni) ----------------------------------

def test_sol_h1_recorded_in_trade(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    pos = _open(eng)
    assert pos["sol_chg_h1"] == 0.77
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v9_data_dir)
    assert t["sol_chg_h1"] == 0.77
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_h1", "liq_entry", "mfe_pct", "mae_pct", "friction_pct"):
        assert k in t, k


def test_writes_only_v9_files(v9_data_dir, monkeypatch):
    eng = V9Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in v9_data_dir.iterdir())
    assert all(f.startswith("v9_") for f in files), files
    state = json.loads((v9_data_dir / v9.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0
