"""V7 senaryo motoru testleri: v6 birebir + TEK fark -%10 felaket freni."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v7_session as v7
from hibrit_trader.v7_session import V7Engine


@pytest.fixture(autouse=True)
def v7_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    # fast feed testte kapali: giris teyidi gercek thread/HTTP acmasin
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    # rejim_reject_kaydet: paylasilan kuyruk temiz, gercek daemon thread acilmasin
    monkeypatch.setattr("hibrit_trader.entry_fresh._watch", {})
    monkeypatch.setattr("hibrit_trader.entry_fresh._start_recheck_thread", lambda: None)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="ZP1", token="ZT1", price=1.0, liq=150_000.0, h1=15.0, m5=-2.0):
    return SimpleNamespace(
        name="Z / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(v7, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        v7, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(v7.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- v6 bandi korunuyor: h1 10..50 ------------------------------------------------

def test_entry_rejects_h1_above_50(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    # RMG tipi dikey pump: v6 gibi v7 de girmez
    assert _enter(eng, monkeypatch, _pair(h1=459658.0)) == []
    assert _enter(eng, monkeypatch, _pair(h1=50.1)) == []


def test_entry_accepts_h1_at_50(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(h1=50.0))) == 1


def test_entry_rejects_h1_below_10(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=9.9)) == []


# ---- Golge'den korunanlar ----------------------------------------------------------

def test_entry_golge_rules_preserved(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=99_000)) == []          # liq >= 100k
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []       # rejim < 0.5
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.4) == []        # V-final: 0..0.4 bandi da kapali
    assert len(_enter(eng, monkeypatch, _pair(h1=45.0, m5=-5.0), sol_h1=0.5)) == 1  # esik dahil, m5 sarti yok


def test_rejim_kapaliyken_reject_kaydi(v7_data_dir, monkeypatch):
    import hibrit_trader.entry_fresh as ef
    from hibrit_trader.momentum_session import REJECTS_FILE
    eng = V7Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.2) == []
    rows = [json.loads(x) for x in
            (v7_data_dir / REJECTS_FILE).read_text().splitlines()]
    assert rows[-1]["reason"] == "rejim_reject"
    assert rows[-1]["engine"] == "V7"
    assert rows[-1]["sol_chg_h1"] == 0.2
    assert "ZP1" in ef._watch


def test_candidates_sorted_highest_h1_first(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    low = _pair(pool="PL", token="TL", h1=12.0)
    high = _pair(pool="PH", token="TH", h1=40.0)
    positions = _enter(eng, monkeypatch, [low, high])
    assert positions[0]["pool_address"] == "PH"


def test_entry_keeps_cooldown(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    eng._cooldown_until["ZT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- Cikislar (golge birebir) -------------------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(v7, "fetch_pool_snapshot", lambda c, ch, p: (price, None))
    monkeypatch.setattr(v7.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / v7.TRADES_FILE).read_text().splitlines()[-1])


def test_tp_2(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert t["exit_reason"] == "tp_2"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + v7.COOLDOWN_EXIT_SEC
    )


def test_patience_holds_above_brake(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    # -%9.9: fren tetiklenmez, sabir tutar (kurtarma bolgesi yasiyor)
    _tick_price(eng, pos, pos["entry_price"] * 0.901, pos["opened_ts"] + v7.GRACE_SEC - 1, monkeypatch)
    assert eng.positions == [pos]


def test_brake_fires_at_minus_10(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # TEK fark: ilk 30dk icinde -%11 -> sabir iptal, aninda stop_felaket
    _tick_price(eng, pos, pos["entry_price"] * 0.89, t0 + 60, monkeypatch)
    assert eng.positions == []
    t = _last(v7_data_dir)
    assert t["exit_reason"] == "stop_felaket"
    # kayip cikisi: 60dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + v7.COOLDOWN_LOSS_SEC
    )


def test_late_stop_after_30min(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.97, t0 + v7.GRACE_SEC + 1, monkeypatch)
    t = _last(v7_data_dir)
    assert t["exit_reason"] == "stop_gec"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + v7.GRACE_SEC + 1 + v7.COOLDOWN_LOSS_SEC
    )


def test_timeout_60(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.005, pos["opened_ts"] + v7.CEILING_SEC + 1, monkeypatch)
    assert _last(v7_data_dir)["exit_reason"] == "timeout_60"


# ---- sol_h1 kaydi + tam set + izolasyon (v6 ile ayni) --------------------------------

def test_sol_h1_recorded_in_trade(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    assert pos["sol_chg_h1"] == 0.77
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert t["sol_chg_h1"] == 0.77
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_h1", "liq_entry", "mfe_pct", "mae_pct", "friction_pct"):
        assert k in t, k


def test_writes_only_v7_files(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in v7_data_dir.iterdir())
    assert all(f.startswith("v7_") for f in files), files
    state = json.loads((v7_data_dir / v7.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0


# ---- Kill-switch tek-seferlik log (M1 paterni) ---------------------------------------

def test_kill_switch_tek_seferlik_log(v7_data_dir, monkeypatch, caplog):
    import logging
    eng = V7Engine(_settings())
    monkeypatch.setattr(v7, "kill_is_active", lambda: True)
    with caplog.at_level(logging.WARNING, logger="hibrit_trader.v7_session"):
        eng._enter(client=SimpleNamespace())
        eng._enter(client=SimpleNamespace())
        assert sum("kill-switch AKTIF" in r.message for r in caplog.records) == 1
        monkeypatch.setattr(v7, "kill_is_active", lambda: False)
        monkeypatch.setattr(v7, "scan_all", lambda chains: [])
        eng._enter(client=SimpleNamespace())
        assert sum("kill-switch kalkti" in r.message for r in caplog.records) == 1


# ---- Gunluk zarar kesicisi (M1 paterni; varsayilan 0 = kapali) -----------------------

def test_daily_loss_varsayilan_kapali(v7_data_dir, monkeypatch):
    assert v7.DAILY_LOSS_LIMIT_USD == 0.0
    eng = V7Engine(_settings())
    eng._day_realized = -10_000.0
    assert len(_enter(eng, monkeypatch, _pair())) == 1  # limit kapali, giris serbest


def test_daily_loss_limit_asilinca_giris_yok_tek_log(v7_data_dir, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(v7, "DAILY_LOSS_LIMIT_USD", 50.0)
    eng = V7Engine(_settings())
    eng._day_realized_add(-50.0, time.time())
    with caplog.at_level(logging.CRITICAL, logger="hibrit_trader.v7_session"):
        assert _enter(eng, monkeypatch, _pair()) == []
        assert _enter(eng, monkeypatch, _pair()) == []
    assert sum("zarar limiti" in r.message for r in caplog.records) == 1


def test_daily_loss_gun_devri_bloku_kaldirir(v7_data_dir, monkeypatch):
    monkeypatch.setattr(v7, "DAILY_LOSS_LIMIT_USD", 50.0)
    eng = V7Engine(_settings())
    eng._day_key = "2000-01-01"  # dun asilan limit bugunu bloklamasin
    eng._day_realized = -500.0
    assert len(_enter(eng, monkeypatch, _pair())) == 1
    assert eng._day_realized == 0.0


def test_daily_loss_kapanista_birikir(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.89, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert t["pnl_usd"] < 0
    assert eng._day_realized == pytest.approx(t["pnl_usd"], abs=1e-3)


def test_daily_loss_restartta_trades_dosyasindan_yuklenir(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.89, pos["opened_ts"] + 60, monkeypatch)
    kayip = _last(v7_data_dir)["pnl_usd"]
    eng2 = V7Engine(_settings())
    assert eng2._day_realized == pytest.approx(kayip)
