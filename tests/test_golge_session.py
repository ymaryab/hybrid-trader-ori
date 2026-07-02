"""Gölge senaryo motoru testleri: iki fark doğru, gerisi v2 ile aynı, tam izole."""

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


def _pair(pool="GP1", token="GT1", price=1.0, liq=50_000.0, h1=15.0, m5=-2.0):
    # m5 varsayilan NEGATIF: golge girisinde m5 sarti olmadigini da sinar
    return SimpleNamespace(
        name="G / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pair, sol_h1=1.0):
    monkeypatch.setattr(gs, "scan_all", lambda chains: [pair])
    monkeypatch.setattr(
        gs, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(gs.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- FARK 1: giriş tek şart chg_h1 >= 10 -----------------------------------

def test_entry_single_condition_h1(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    # m5 negatif ve h1 > 50 OLSA BILE girer (v2'de ikisi de elerdi)
    assert len(_enter(eng, monkeypatch, _pair(h1=80.0, m5=-5.0))) == 1


def test_entry_rejects_h1_below_10(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=9.9)) == []


def test_entry_keeps_liq_floor(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=39_000)) == []


def test_entry_keeps_regime_filter(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []


def test_entry_keeps_cooldown(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    eng._cooldown_until["GT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- FARK 2: stop yok, 20dk sabır ------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(gs, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(gs.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def test_no_stop_at_minus_2(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.90, t0 + 60, monkeypatch)  # -%10!
    assert eng.positions == [pos]  # v2 stop_2 satardı, gölge tutuyor
    assert pos["dip_since"] == t0 + 60


def test_patience_20min_sells_below_entry(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.95, t0 + 60, monkeypatch)
    _tick_price(eng, pos, pos["entry_price"] * 0.95, t0 + 60 + gs.PATIENCE_SEC, monkeypatch)
    assert eng.positions == []
    trade = json.loads((golge_data_dir / gs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "sabir_20"
    # kayıp çıkışı: 60dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + gs.PATIENCE_SEC + gs.COOLDOWN_LOSS_SEC
    )


def test_recovery_above_entry_resets_patience(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.95, t0 + 60, monkeypatch)
    _tick_price(eng, pos, pos["entry_price"] * 1.01, t0 + 600, monkeypatch)  # üstüne döndü
    assert pos["dip_since"] is None
    _tick_price(eng, pos, pos["entry_price"] * 0.98, t0 + 700, monkeypatch)  # yeni düşüş
    _tick_price(eng, pos, pos["entry_price"] * 0.98, t0 + 700 + gs.PATIENCE_SEC - 1, monkeypatch)
    assert eng.positions == [pos]  # sayaç sıfırlandığı için henüz satmaz


# ---- Korunanlar: v2 çıkış kuralları birebir ---------------------------------

def test_breakeven_preserved(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.04, t0 + 60, monkeypatch)   # +%4: be arm
    assert pos["be_armed"]
    _tick_price(eng, pos, pos["entry_price"] * 1.005, t0 + 120, monkeypatch)  # geri döndü
    trade = json.loads((golge_data_dir / gs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "breakeven"


def test_trail_preserved(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.10, t0 + 60, monkeypatch)   # +%10: trail arm
    assert pos["trail_armed"]
    _tick_price(eng, pos, pos["entry_price"] * 1.10 * 0.96, t0 + 120, monkeypatch)  # tepeden -%4
    trade = json.loads((golge_data_dir / gs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "trail"


def test_timeout_preserved(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # hep girişin üstünde ama kilitler kurulmadan 60dk doldu
    _tick_price(eng, pos, pos["entry_price"] * 1.01, t0 + gs.CEILING_SEC + 1, monkeypatch)
    trade = json.loads((golge_data_dir / gs.TRADES_FILE).read_text().splitlines()[-1])
    assert trade["exit_reason"] == "timeout_60"


# ---- İzolasyon: sadece golge_* dosyaları ------------------------------------

def test_writes_only_golge_files(golge_data_dir, monkeypatch):
    eng = GolgeEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.95,
                pos["opened_ts"] + 60 + gs.PATIENCE_SEC + 1, monkeypatch)
    _tick_price(eng, pos, pos["entry_price"] * 0.95,
                pos["opened_ts"] + 120 + gs.PATIENCE_SEC + 1, monkeypatch)
    files = sorted(p.name for p in golge_data_dir.iterdir())
    assert all(f.startswith("golge_") for f in files), files  # momentum_* YOK
    state = json.loads((golge_data_dir / gs.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0
