"""Gölge senaryo motoru testleri (2026-07-02 akşam revizyonu): tp_2 / stop_gec / timeout_60."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.golge_session as gs
from hibrit_trader.golge_session import GolgeEngine


@pytest.fixture(autouse=True)
def golge_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="GP1", token="GT1", price=1.0, liq=150_000.0, h1=15.0, m5=-2.0):
    # m5 varsayilan NEGATIF: golge girisinde m5 sarti olmadigini da sinar
    return SimpleNamespace(
        name="G / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(gs, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        gs, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(gs.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- Giriş: liq >= $100k VE chg_h1 >= 10 ------------------------------------

def test_entry_h1_and_liq(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    # m5 negatif ve h1 üst sınırsız: yine de girer
    assert len(_enter(eng, monkeypatch, _pair(h1=80.0, m5=-5.0))) == 1


def test_entry_rejects_h1_below_10(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=9.9)) == []


def test_entry_rejects_liq_below_100k(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=99_000)) == []


def test_entry_accepts_liq_at_100k(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(liq=100_000))) == 1


def test_candidates_sorted_highest_h1_first(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    low = _pair(pool="PL", token="TL", h1=12.0)
    high = _pair(pool="PH", token="TH", h1=40.0)
    positions = _enter(eng, monkeypatch, [low, high])
    assert len(positions) == 2
    assert positions[0]["pool_address"] == "PH"  # yüksek h1 önce girdi


def test_entry_keeps_regime_filter(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []


def test_entry_keeps_cooldown(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    eng._cooldown_until["GT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- Çıkış: tp_2 / stop_gec / timeout_60 -------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(gs, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(gs.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def test_tp_2_sells_at_plus_2(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)
    assert eng.positions == []
    trade = json.loads((golge_data_dir / gs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "tp_2"
    # kârlı çıkış: 15dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + gs.COOLDOWN_EXIT_SEC
    )


def test_holds_below_plus_2(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.019, pos["opened_ts"] + 60, monkeypatch)
    assert eng.positions == [pos]


def test_no_stop_in_first_30min(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.90, t0 + gs.GRACE_SEC - 1, monkeypatch)  # -%10!
    assert eng.positions == [pos]  # sabır penceresi: satmaz


def test_late_stop_after_30min(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.97, t0 + gs.GRACE_SEC + 1, monkeypatch)  # -%3
    assert eng.positions == []
    trade = json.loads((golge_data_dir / gs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "stop_gec"
    # kayıp çıkışı: 60dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + gs.GRACE_SEC + 1 + gs.COOLDOWN_LOSS_SEC
    )


def test_no_late_stop_above_minus_2(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.99, t0 + gs.GRACE_SEC + 60, monkeypatch)  # -%1
    assert eng.positions == [pos]  # -%2'nin üstünde: tutar


def test_timeout_60_unconditional(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # -%2 ile +%2 arasında sıkışan pozisyon 60dk'da kapanır
    _tick_price(eng, pos, pos["entry_price"] * 1.005, t0 + gs.CEILING_SEC + 1, monkeypatch)
    trade = json.loads((golge_data_dir / gs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "timeout_60"


def test_no_trail_or_breakeven(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # +%1.9 gördü, sonra girişin hemen üstüne düştü: eski be/trail satardı, yeni kural tutar
    _tick_price(eng, pos, pos["entry_price"] * 1.019, t0 + 60, monkeypatch)
    _tick_price(eng, pos, pos["entry_price"] * 1.001, t0 + 120, monkeypatch)
    assert eng.positions == [pos]
    assert "be_armed" not in pos and "trail_armed" not in pos


# ---- Kayıt tam seti + izolasyon ------------------------------------------------

def test_trade_row_has_analysis_fields(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    trade = json.loads((golge_data_dir / gs.TRADES_FILE).read_text().splitlines()[-1])
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_h1", "liq_entry", "mfe_pct", "mae_pct"):
        assert k in trade, k
    assert trade["mfe_pct"] >= 2.9


def test_writes_only_golge_files(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in golge_data_dir.iterdir())
    assert all(f.startswith("golge_") for f in files), files  # momentum_* / v3_* YOK
    state = json.loads((golge_data_dir / gs.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0
