"""X1 senaryo motoru testleri: kosucu avcisi, yarim tp mfe>=15, trail -18, 6sa tavan."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.x1_session as x1
from hibrit_trader.x1_session import X1Engine


@pytest.fixture(autouse=True)
def x1_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="XP1", token="XT1", price=1.0, liq=60_000.0, h1=80.0, m5=5.0):
    return SimpleNamespace(
        name="X / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(x1, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        x1, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(x1.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- Giris: kosucu tetigi -----------------------------------------------------------

def test_entry_rejects_h1_below_50(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=49.9)) == []


def test_entry_accepts_h1_50_and_above_no_upper_band(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(h1=50.0))) == 1
    assert len(_enter(eng, monkeypatch, _pair(pool="XP2", token="XT2", h1=459658.0))) == 2


def test_entry_rejects_liq_below_20k(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=19_000)) == []
    assert len(_enter(eng, monkeypatch, _pair(liq=20_000))) == 1


def test_entry_rejects_dead_run_m5_not_positive(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(m5=0.0)) == []
    assert _enter(eng, monkeypatch, _pair(m5=-3.0)) == []


def test_entry_regime_blocks(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []


def test_candidates_sorted_highest_m5_first(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    calm = _pair(pool="PC", token="TC", m5=2.0)
    alive = _pair(pool="PA", token="TA", m5=30.0)
    positions = _enter(eng, monkeypatch, [calm, alive])
    assert positions[0]["pool_address"] == "PA"


def test_three_slots_max_with_ticket_cap(x1_data_dir, monkeypatch):
    assert x1.MAX_SLOTS == 3
    eng = X1Engine(_settings())
    pairs = [_pair(pool=f"P{i}", token=f"T{i}") for i in range(5)]
    positions = _enter(eng, monkeypatch, pairs)
    # bilet tavani $70: kucuk bilet gas payi birakir, 3 slot tek tick'te dolar
    assert len(positions) == 3
    assert all(p["cost_usd"] == pytest.approx(70.0) for p in positions)
    # slotlar doluyken yeni giris yok
    _enter(eng, monkeypatch, _pair(pool="YENI", token="YT"))
    assert len(eng.positions) == 3


def test_ticket_uses_balance_third_when_below_cap(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    eng.balance = 150.0  # bakiye/3 = 50 < 70 tavani
    positions = _enter(eng, monkeypatch, _pair())
    assert positions[0]["cost_usd"] == pytest.approx(50.0)


def test_entry_keeps_cooldown(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    eng._cooldown_until["XT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


def test_safety_reject_blocks_entry(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    monkeypatch.setattr(x1, "scan_all", lambda chains: [_pair()])
    monkeypatch.setattr(
        x1, "check_token", lambda client, chain, token: SimpleNamespace(ok=False)
    )
    monkeypatch.setattr(x1.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: 1.0)
    eng._enter(client=SimpleNamespace())
    assert eng.positions == []


# ---- Cikislar: tp yok, trail -18, erken fren, 6sa tavan --------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(x1, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(x1.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / x1.TRADES_FILE).read_text().splitlines()[-1])


def test_no_full_tp_holds_half_through_big_gain(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 3.0, t0 + 600, monkeypatch)
    assert eng.positions == [pos]  # +%200'de bile tam satis yok, kalan yari kosar
    assert pos["yarim_satildi"] is True  # ama yarim tp kilitlendi
    assert pos["cost_usd"] == pytest.approx(50.0)


def test_partial_take_at_mfe_15_sells_half_once(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    e = pos["entry_price"]
    amount0 = pos["amount_token"]
    bal0 = eng.balance
    _tick_price(eng, pos, e * 1.14, t0 + 300, monkeypatch)    # mfe 14: tetiklenmez
    assert pos["yarim_satildi"] is False
    _tick_price(eng, pos, e * 1.16, t0 + 600, monkeypatch)    # mfe 16: yarim sat
    assert eng.positions == [pos]  # pozisyon acik kalir
    assert pos["yarim_satildi"] is True
    assert pos["amount_token"] == pytest.approx(amount0 / 2)
    assert pos["cost_usd"] == pytest.approx(50.0)
    assert eng.balance > bal0
    t = _last(x1_data_dir)
    assert t["exit_reason"] == "tp_yarim_15"
    assert t["cost_usd"] == pytest.approx(50.0)
    assert t["pnl_usd"] > 0
    # ikinci kez tetiklenmez
    _tick_price(eng, pos, e * 1.5, t0 + 900, monkeypatch)
    lines = (x1_data_dir / x1.TRADES_FILE).read_text().splitlines()
    assert len(lines) == 1
    assert pos["amount_token"] == pytest.approx(amount0 / 2)


def test_partial_then_trail_closes_remaining_half(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    e = pos["entry_price"]
    _tick_price(eng, pos, e * 2.0, t0 + 600, monkeypatch)     # yarim tp + tepe 2.0x
    assert pos["yarim_satildi"] is True
    _tick_price(eng, pos, e * 1.60, t0 + 900, monkeypatch)    # tepeden -20: kalan yari SAT
    assert eng.positions == []
    lines = [json.loads(l) for l in (x1_data_dir / x1.TRADES_FILE).read_text().splitlines()]
    assert [t["exit_reason"] for t in lines] == ["tp_yarim_15", "trail_18"]
    assert lines[1]["cost_usd"] == pytest.approx(50.0)
    assert lines[1]["pnl_usd"] > 0  # kalan yari da girisin ustunde cikti


def test_trail_18_fires_from_peak(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    e = pos["entry_price"]
    _tick_price(eng, pos, e * 2.0, t0 + 600, monkeypatch)     # tepe 2.0x
    _tick_price(eng, pos, e * 1.70, t0 + 900, monkeypatch)    # tepeden -15: tutar
    assert eng.positions == [pos]
    _tick_price(eng, pos, e * 1.63, t0 + 1200, monkeypatch)   # tepeden -18.5: SAT
    assert eng.positions == []
    t = _last(x1_data_dir)
    assert t["exit_reason"] == "trail_18"
    assert t["pnl_usd"] > 0  # karli cikis: 30dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 1200 + x1.COOLDOWN_EXIT_SEC
    )


def test_breath_within_trail_does_not_sell(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    e = pos["entry_price"]
    _tick_price(eng, pos, e * 2.0, t0 + 600, monkeypatch)
    _tick_price(eng, pos, e * 1.80, t0 + 900, monkeypatch)    # nefes -10
    _tick_price(eng, pos, e * 2.4, t0 + 1200, monkeypatch)    # yeni tepe: nefes muhurlendi
    assert eng.positions == [pos]
    assert pos["nefes_n"] == 1
    assert pos["nefes_en_derin_pct"] == pytest.approx(-10.0)


def test_early_stop_before_plus_10(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.87, t0 + 300, monkeypatch)
    assert eng.positions == []
    t = _last(x1_data_dir)
    assert t["exit_reason"] == "stop_giris"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 300 + x1.COOLDOWN_LOSS_SEC  # kayip: 90dk
    )


def test_early_stop_disabled_after_plus_10(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    e = pos["entry_price"]
    _tick_price(eng, pos, e * 1.11, t0 + 300, monkeypatch)    # +%11 gordu
    _tick_price(eng, pos, e * 0.93, t0 + 600, monkeypatch)    # tepeden -16.2: tutar
    assert eng.positions == [pos]
    _tick_price(eng, pos, e * 0.90, t0 + 900, monkeypatch)    # tepeden -18.9: trail satar
    assert _last(x1_data_dir)["exit_reason"] == "trail_18"


def test_timeout_6h(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    e = pos["entry_price"]
    _tick_price(eng, pos, e * 1.12, t0 + 3600, monkeypatch)   # 1 saat: tutar
    assert eng.positions == [pos]
    _tick_price(eng, pos, e * 1.12, t0 + x1.CEILING_SEC + 1, monkeypatch)
    assert _last(x1_data_dir)["exit_reason"] == "timeout_360"


# ---- Kayit: tam set + tepe/nefes gecmisi ------------------------------------------------

def test_trade_records_breath_history_and_full_set(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    e = pos["entry_price"]
    _tick_price(eng, pos, e * 1.5, t0 + 300, monkeypatch)
    _tick_price(eng, pos, e * 1.40, t0 + 600, monkeypatch)    # nefes -6.7
    _tick_price(eng, pos, e * 2.0, t0 + 900, monkeypatch)     # yeni tepe
    _tick_price(eng, pos, e * 1.85, t0 + 1200, monkeypatch)   # nefes -7.5
    _tick_price(eng, pos, e * 2.2, t0 + 1500, monkeypatch)    # yeni tepe
    _tick_price(eng, pos, e * 1.7, t0 + 1800, monkeypatch)    # tepeden -22.7: trail
    t = _last(x1_data_dir)
    assert t["exit_reason"] == "trail_18"
    assert t["nefes_n"] == 2
    assert t["nefes_en_derin_pct"] == pytest.approx(-7.5)
    assert t["sol_chg_h1"] == 0.77
    for k in ("entry_price", "exit_price", "pnl_usd", "pnl_pct", "chg_m5", "chg_h1",
              "liq_entry", "mfe_pct", "mae_pct", "peak_price", "friction_pct"):
        assert k in t, k


def test_writes_only_x1_files(x1_data_dir, monkeypatch):
    eng = X1Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.85, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in x1_data_dir.iterdir())
    assert all(f.startswith("x1_") for f in files), files
    state = json.loads((x1_data_dir / x1.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0
