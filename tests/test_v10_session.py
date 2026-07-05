"""V10 senaryo motoru testleri: saf tp_2, baska hicbir kural yok."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v10_session as v10
from hibrit_trader.v10_session import V10Engine


@pytest.fixture(autouse=True)
def v10_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="WP1", token="WT1", price=1.0, liq=350_000.0, h1=15.0, m5=-2.0):
    return SimpleNamespace(
        name="W / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(v10, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        v10, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(v10.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- Giris: liq >= 300k + h1 10..50 ---------------------------------------------------

def test_entry_rejects_liq_below_300k(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=299_000)) == []
    assert len(_enter(eng, monkeypatch, _pair(liq=300_000))) == 1


def test_entry_h1_band(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=9.9)) == []
    assert _enter(eng, monkeypatch, _pair(h1=50.1)) == []
    assert len(_enter(eng, monkeypatch, _pair(h1=10.0))) == 1
    assert len(_enter(eng, monkeypatch, _pair(pool="WP2", token="WT2", h1=50.0))) == 2


def test_candidates_sorted_highest_h1_first(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    low = _pair(pool="PL", token="TL", h1=12.0)
    high = _pair(pool="PH", token="TH", h1=40.0)
    positions = _enter(eng, monkeypatch, [low, high])
    assert positions[0]["pool_address"] == "PH"


def test_five_slots_budget_fifth(v10_data_dir, monkeypatch):
    assert v10.MAX_SLOTS == 5
    eng = V10Engine(_settings())
    positions = _enter(eng, monkeypatch, _pair())
    assert positions[0]["cost_usd"] == pytest.approx(1000 / 5)


def test_safety_reject_blocks_entry(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    monkeypatch.setattr(v10, "scan_all", lambda chains: [_pair()])
    monkeypatch.setattr(
        v10, "check_token", lambda client, chain, token: SimpleNamespace(ok=False)
    )
    monkeypatch.setattr(v10.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: 1.0)
    eng._enter(client=SimpleNamespace())
    assert eng.positions == []


# ---- Ilave kural YOK: rejim ve cooldown devre disi -------------------------------------

def test_no_regime_filter_enters_on_negative_sol(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    positions = _enter(eng, monkeypatch, _pair(), sol_h1=-5.0)
    assert len(positions) == 1
    assert positions[0]["sol_chg_h1"] == -5.0  # sadece kayit


def test_no_cooldown_reenters_immediately(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    assert eng.positions == []
    assert len(_enter(eng, monkeypatch, _pair())) == 1  # ayni token hemen geri alinir


# ---- Cikis: SADECE tp_2 ----------------------------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(v10, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(v10.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / v10.TRADES_FILE).read_text().splitlines()[-1])


def test_tp_2_fires(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    assert eng.positions == []
    assert _last(v10_data_dir)["exit_reason"] == "tp_2"


def test_no_stop_holds_deep_loss(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.50, t0 + 60, monkeypatch)   # -%50
    _tick_price(eng, pos, pos["entry_price"] * 0.10, t0 + 3600, monkeypatch)  # -%90
    assert eng.positions == [pos]  # stop yok, tutmaya devam


def test_no_timeout_holds_for_days(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.01, t0 + 72 * 3600, monkeypatch)
    assert eng.positions == [pos]  # 3 gun sonra bile timeout yok
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 72 * 3600 + 60, monkeypatch)
    assert _last(v10_data_dir)["exit_reason"] == "tp_2"  # tek cikis: +%2


# ---- Kayit + izolasyon -----------------------------------------------------------------

def test_sol_h1_recorded_full_set(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    pos = _open(eng)
    assert pos["sol_chg_h1"] == 0.77
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v10_data_dir)
    assert t["sol_chg_h1"] == 0.77
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_h1", "liq_entry", "mfe_pct", "mae_pct", "friction_pct"):
        assert k in t, k


def test_writes_only_v10_files(v10_data_dir, monkeypatch):
    eng = V10Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in v10_data_dir.iterdir())
    assert all(f.startswith("v10_") for f in files), files
    state = json.loads((v10_data_dir / v10.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0
