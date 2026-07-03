"""V4 melez senaryo motoru testleri: v3 girişi + v2 çıkışı + kademeli trail + akıllı timeout."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v4_session as v4
from hibrit_trader.v4_session import V4Engine


@pytest.fixture(autouse=True)
def v4_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="WP1", token="WT1", price=1.0, liq=50_000.0, h1=8.0, m5=2.0):
    return SimpleNamespace(
        name="W / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(v4, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        v4, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(v4.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- Giriş: v3'ün kanıtlı filtreleri ------------------------------------------

def test_entry_band_and_filters(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=15.1)) == []          # üst sınır 15
    assert _enter(eng, monkeypatch, _pair(h1=4.9)) == []           # alt sınır 5
    assert _enter(eng, monkeypatch, _pair(m5=-1.0)) == []          # m5 > 0
    assert _enter(eng, monkeypatch, _pair(liq=39_000)) == []       # liq >= 40k
    assert len(_enter(eng, monkeypatch, _pair())) == 1             # bant içi girer


def test_candidates_sorted_lowest_h1_first(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    high = _pair(pool="PH", token="TH", h1=14.0)
    low = _pair(pool="PL", token="TL", h1=6.0)
    positions = _enter(eng, monkeypatch, [high, low])
    assert len(positions) == 2
    assert positions[0]["pool_address"] == "PL"


def test_regime_blocks_below_half(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.4) == []
    assert len(_enter(eng, monkeypatch, _pair(), sol_h1=0.5)) == 1


def test_entry_keeps_cooldown(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    eng._cooldown_until["WT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- Çıkış motoru ----------------------------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0, sol_h1=1.0)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(v4, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(v4.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last_trade(data_dir):
    return json.loads((data_dir / v4.TRADES_FILE).read_text().splitlines()[-1])


def test_stop_2_preserved(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.979, t0 + 60, monkeypatch)
    assert _last_trade(v4_data_dir)["exit_reason"] == "stop_2"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(t0 + 60 + 3600)


def test_breakeven_at_1_5(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.04, t0 + 60, monkeypatch)    # be arm
    _tick_price(eng, pos, pos["entry_price"] * 1.014, t0 + 120, monkeypatch)  # +%1.4
    assert _last_trade(v4_data_dir)["exit_reason"] == "breakeven"
    # karlı/nötr çıkış: 45dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 120 + 45 * 60
    )


# ---- Kademeli trail ----------------------------------------------------------------

def test_trail_stage1_minus_3(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.10, t0 + 60, monkeypatch)   # +%10: kademe 1
    assert pos["trail_kademe"] == 1
    _tick_price(eng, pos, pos["entry_price"] * 1.10 * 0.96, t0 + 120, monkeypatch)  # tepeden -%4
    trade = _last_trade(v4_data_dir)
    assert trade["exit_reason"] == "trail"
    assert trade["trail_kademe"] == 1


def test_trail_stage2_widens_to_6(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.25, t0 + 60, monkeypatch)   # +%25: kademe 2
    assert pos["trail_kademe"] == 2
    # tepeden -%4: kademe 1 satardı, kademe 2 (-%6) tutar
    _tick_price(eng, pos, pos["entry_price"] * 1.25 * 0.96, t0 + 120, monkeypatch)
    assert eng.positions == [pos]
    # tepeden -%7: kademe 2 satar
    _tick_price(eng, pos, pos["entry_price"] * 1.25 * 0.93, t0 + 180, monkeypatch)
    trade = _last_trade(v4_data_dir)
    assert trade["exit_reason"] == "trail"
    assert trade["trail_kademe"] == 2


def test_trail_stage2_is_one_way(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.25, t0 + 60, monkeypatch)   # kademe 2 kilidi
    _tick_price(eng, pos, pos["entry_price"] * 1.20, t0 + 120, monkeypatch)  # pnl %20'ye indi
    assert pos["trail_kademe"] == 2  # geri kademe 1'e düşmez


# ---- Akıllı timeout -----------------------------------------------------------------

def test_timeout_60_when_not_in_profit(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # -%2 ile 0 arası: stop tetiklenmez ama 60dk'da karda değil, kapanır
    _tick_price(eng, pos, pos["entry_price"] * 0.99, t0 + v4.CEILING_1_SEC + 1, monkeypatch)
    assert _last_trade(v4_data_dir)["exit_reason"] == "timeout_60"


def test_timeout_extends_to_120_in_profit(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # karda (+%1, kilitler kurulmadan): 60dk'da KAPANMAZ
    _tick_price(eng, pos, pos["entry_price"] * 1.01, t0 + v4.CEILING_1_SEC + 1, monkeypatch)
    assert eng.positions == [pos]
    # 120dk dolunca koşulsuz kapanır
    _tick_price(eng, pos, pos["entry_price"] * 1.01, t0 + v4.CEILING_2_SEC + 1, monkeypatch)
    assert _last_trade(v4_data_dir)["exit_reason"] == "timeout_120"


def test_timeout_60_fires_when_profit_fades_after_60(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.01, t0 + v4.CEILING_1_SEC + 1, monkeypatch)
    assert eng.positions == [pos]  # karda: uzadı
    # 70. dakikada kar eridi (0'ın altı): ölü ağırlık, kapanır
    _tick_price(eng, pos, pos["entry_price"] * 0.995, t0 + 70 * 60, monkeypatch)
    assert _last_trade(v4_data_dir)["exit_reason"] == "timeout_60"


# ---- Kayıt tam seti + izolasyon -------------------------------------------------------

def test_trade_row_has_full_analysis_fields(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.97, pos["opened_ts"] + 60, monkeypatch)
    trade = _last_trade(v4_data_dir)
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_m5", "chg_h1", "liq_entry", "sol_chg_h1", "mfe_pct", "mae_pct",
              "trail_kademe"):
        assert k in trade, k
    assert trade["sol_chg_h1"] == 1.0
    assert trade["trail_kademe"] == 0


def test_writes_only_v4_files(v4_data_dir, monkeypatch):
    eng = V4Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.97, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in v4_data_dir.iterdir())
    assert all(f.startswith("v4_") for f in files), files
    state = json.loads((v4_data_dir / v4.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0
