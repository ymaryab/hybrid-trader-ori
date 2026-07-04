"""V8 senaryo motoru testleri: gölge birebir + 4 fark (200k / 20..50 / tp3 / 20dk)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v8_session as v8
from hibrit_trader.v8_session import V8Engine


@pytest.fixture(autouse=True)
def v8_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="ZP1", token="ZT1", price=1.0, liq=250_000.0, h1=30.0, m5=-2.0):
    return SimpleNamespace(
        name="Z / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(v8, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        v8, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(v8.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- Fark 1: likidite tabani $200k -------------------------------------------------

def test_entry_rejects_liq_below_200k(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=199_000)) == []
    assert _enter(eng, monkeypatch, _pair(liq=150_000)) == []  # golge kabul ederdi


def test_entry_accepts_liq_at_200k(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(liq=200_000))) == 1


# ---- Fark 2: giris bandi h1 20..50 --------------------------------------------------

def test_entry_rejects_h1_below_20(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=19.9)) == []
    assert _enter(eng, monkeypatch, _pair(h1=10.0)) == []  # golge kabul ederdi


def test_entry_rejects_h1_above_50(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=459658.0)) == []
    assert _enter(eng, monkeypatch, _pair(h1=50.1)) == []


def test_entry_accepts_band_edges(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(h1=20.0))) == 1
    assert len(_enter(eng, monkeypatch, _pair(pool="ZP2", token="ZT2", h1=50.0))) == 2


# ---- Golge'den korunanlar ------------------------------------------------------------

def test_entry_golge_rules_preserved(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []       # rejim < 0
    assert len(_enter(eng, monkeypatch, _pair(h1=45.0, m5=-5.0))) == 1  # m5 sarti yok


def test_candidates_sorted_highest_h1_first(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    low = _pair(pool="PL", token="TL", h1=22.0)
    high = _pair(pool="PH", token="TH", h1=48.0)
    positions = _enter(eng, monkeypatch, [low, high])
    assert positions[0]["pool_address"] == "PH"


def test_entry_keeps_cooldown(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    eng._cooldown_until["ZT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- Cikislar: sadece tp_3 / timeout_20 ----------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(v8, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(v8.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / v8.TRADES_FILE).read_text().splitlines()[-1])


def test_tp_3(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.03, t0 + 60, monkeypatch)
    t = _last(v8_data_dir)
    assert t["exit_reason"] == "tp_3"
    # karli cikis: 15dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + v8.COOLDOWN_EXIT_SEC
    )


def test_plus_2_not_enough(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    pos = _open(eng)
    # golge tp+2'de satardi; v8 hedef +3, tutar
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    assert eng.positions == [pos]


def test_no_stop_before_20min(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    pos = _open(eng)
    # dususte bile 20dk dolmadan cikis yok (stop dali yok)
    _tick_price(eng, pos, pos["entry_price"] * 0.90, pos["opened_ts"] + v8.CEILING_SEC - 1, monkeypatch)
    assert eng.positions == [pos]


def test_timeout_20_closes_at_loss_with_loss_cooldown(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.95, t0 + v8.CEILING_SEC + 1, monkeypatch)
    assert eng.positions == []
    t = _last(v8_data_dir)
    assert t["exit_reason"] == "timeout_20"
    assert t["pnl_usd"] < 0
    # kayip cikisi: 60dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + v8.CEILING_SEC + 1 + v8.COOLDOWN_LOSS_SEC
    )


def test_timeout_20_closes_at_profit_with_exit_cooldown(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    pos = _open(eng, liq=5_000_000.0)  # friction'i kucult, +%2 net karda kalsin
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + v8.CEILING_SEC + 1, monkeypatch)
    t = _last(v8_data_dir)
    assert t["exit_reason"] == "timeout_20"
    assert t["pnl_usd"] > 0
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + v8.CEILING_SEC + 1 + v8.COOLDOWN_EXIT_SEC
    )


def test_max_hold_is_20min(v8_data_dir):
    # 60dk tavan YOK: mutlak tavan 20dk
    assert v8.CEILING_SEC == 20 * 60
    assert not hasattr(v8, "GRACE_SEC")
    assert not hasattr(v8, "LATE_STOP_PCT")


# ---- sol_h1 kaydi + tam set + izolasyon ----------------------------------------------

def test_sol_h1_recorded_in_trade(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    pos = _open(eng)
    assert pos["sol_chg_h1"] == 0.77
    _tick_price(eng, pos, pos["entry_price"] * 1.04, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v8_data_dir)
    assert t["sol_chg_h1"] == 0.77
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_h1", "liq_entry", "mfe_pct", "mae_pct", "friction_pct"):
        assert k in t, k


def test_writes_only_v8_files(v8_data_dir, monkeypatch):
    eng = V8Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in v8_data_dir.iterdir())
    assert all(f.startswith("v8_") for f in files), files
    state = json.loads((v8_data_dir / v8.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0
