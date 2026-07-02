"""V3 senaryo motoru testleri: 5 fark doğru, gerisi v2 ile aynı, tam izole."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v3_session as vs
from hibrit_trader.v3_session import V3Engine


@pytest.fixture(autouse=True)
def v3_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="VP1", token="VT1", price=1.0, liq=50_000.0, h1=8.0, m5=2.0):
    return SimpleNamespace(
        name="V / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(vs, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        vs, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(vs.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- FARK 1: h1 üst sınırı 15 ------------------------------------------------

def test_entry_rejects_h1_above_15(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=15.1)) == []


def test_entry_accepts_h1_in_band(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(h1=14.9))) == 1


def test_entry_keeps_h1_lower_bound(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=4.9)) == []


# ---- FARK 2: en düşük chg_h1 önce ---------------------------------------------

def test_candidates_sorted_lowest_h1_first(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    high = _pair(pool="PH", token="TH", h1=14.0)
    low = _pair(pool="PL", token="TL", h1=6.0)
    positions = _enter(eng, monkeypatch, [high, low])
    assert len(positions) == 2
    assert positions[0]["pool_address"] == "PL"  # düşük h1 önce girdi


# ---- FARK 3: rejim eşiği 0.5 ---------------------------------------------------

def test_regime_blocks_below_half(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.4) == []


def test_regime_allows_at_half(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(), sol_h1=0.5)) == 1


def test_regime_fail_open(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(), sol_h1=None)) == 1


# ---- FARK 4: karlı/nötr çıkış cooldown 45dk, stop 60dk -------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0, sol_h1=1.0)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(vs, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(vs.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def test_profit_exit_cooldown_45min(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.10, t0 + 60, monkeypatch)          # trail arm
    _tick_price(eng, pos, pos["entry_price"] * 1.10 * 0.96, t0 + 120, monkeypatch)  # trail sat
    assert eng.positions == []
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 120 + 45 * 60
    )


def test_stop_cooldown_stays_60min(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.97, t0 + 60, monkeypatch)  # -%3: stop
    trade = json.loads((v3_data_dir / vs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "stop_2"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(t0 + 60 + 3600)


# ---- FARK 5: breakeven tetiği +%1.5 --------------------------------------------

def test_breakeven_triggers_at_1_5(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.04, t0 + 60, monkeypatch)   # +%4: be arm
    assert pos["be_armed"]
    _tick_price(eng, pos, pos["entry_price"] * 1.014, t0 + 120, monkeypatch)  # +%1.4 < +%1.5
    trade = json.loads((v3_data_dir / vs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "breakeven"


def test_no_breakeven_above_1_5(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.04, t0 + 60, monkeypatch)    # +%4: be arm
    _tick_price(eng, pos, pos["entry_price"] * 1.016, t0 + 120, monkeypatch)  # +%1.6: tutar
    assert eng.positions == [pos]


# ---- Korunanlar: v2 kuralları birebir -------------------------------------------

def test_entry_keeps_m5_filter(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(m5=-1.0)) == []
    assert _enter(eng, monkeypatch, _pair(m5=0.0)) == []  # m5 > 0 şartı (eşit elenir)


def test_entry_keeps_liq_floor(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=39_000)) == []


def test_entry_keeps_cooldown(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    eng._cooldown_until["VT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


def test_stop_2_preserved(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.979, t0 + 60, monkeypatch)  # -%2.1
    assert eng.positions == []
    trade = json.loads((v3_data_dir / vs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "stop_2"


def test_trail_preserved(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.10, t0 + 60, monkeypatch)
    assert pos["trail_armed"]
    _tick_price(eng, pos, pos["entry_price"] * 1.10 * 0.96, t0 + 120, monkeypatch)
    trade = json.loads((v3_data_dir / vs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "trail"


def test_timeout_preserved(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.005, t0 + vs.CEILING_SEC + 1, monkeypatch)
    trade = json.loads((v3_data_dir / vs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "timeout_60"


# ---- Kayıt tam seti + izolasyon --------------------------------------------------

def test_trade_row_has_full_analysis_fields(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.97, pos["opened_ts"] + 60, monkeypatch)
    trade = json.loads((v3_data_dir / vs.TRADES_FILE).read_text().splitlines()[-1])
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_m5", "chg_h1", "liq_entry", "sol_chg_h1", "mfe_pct", "mae_pct"):
        assert k in trade, k
    assert trade["sol_chg_h1"] == 1.0


def test_writes_only_v3_files(v3_data_dir, monkeypatch):
    eng = V3Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.97, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in v3_data_dir.iterdir())
    assert all(f.startswith("v3_") for f in files), files  # momentum_* / golge_* YOK
    state = json.loads((v3_data_dir / vs.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0
