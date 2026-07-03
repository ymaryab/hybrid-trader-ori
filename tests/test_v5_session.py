"""V5 senaryo motoru testleri: gölge zemini + stop_felaket + tp yarım/koşucu + shadow."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v5_session as v5
from hibrit_trader.v5_session import V5Engine


@pytest.fixture(autouse=True)
def v5_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="XP1", token="XT1", price=1.0, liq=150_000.0, h1=15.0, m5=-2.0):
    return SimpleNamespace(
        name="X / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(v5, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        v5, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(v5.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- Giriş: gölge ile birebir ---------------------------------------------------

def test_entry_golge_rules_preserved(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=9.9)) == []            # h1 >= 10
    assert _enter(eng, monkeypatch, _pair(liq=99_000)) == []        # liq >= 100k
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []     # rejim < 0
    assert len(_enter(eng, monkeypatch, _pair(h1=80.0, m5=-5.0))) == 1  # m5/h1-max yok


def test_candidates_sorted_highest_h1_first(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    low = _pair(pool="PL", token="TL", h1=12.0)
    high = _pair(pool="PH", token="TH", h1=40.0)
    positions = _enter(eng, monkeypatch, [low, high])
    assert positions[0]["pool_address"] == "PH"


def test_entry_keeps_cooldown(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    eng._cooldown_until["XT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- Çıkışlar ---------------------------------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(v5, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(v5.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _trades(data_dir):
    return [json.loads(l) for l in (data_dir / v5.TRADES_FILE).read_text().splitlines()]


# F1: stop_felaket

def test_disaster_floor_cancels_patience(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # ilk 30dk içinde -%9: gölge tutar, V5 anında keser
    _tick_price(eng, pos, pos["entry_price"] * 0.91, t0 + 60, monkeypatch)
    assert eng.positions == []
    t = _trades(v5_data_dir)[-1]
    assert t["exit_reason"] == "stop_felaket"
    # kayıp çıkışı: 60dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + v5.COOLDOWN_LOSS_SEC
    )


def test_patience_holds_above_disaster_floor(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # -%7.9: taban tetiklenmez, sabır penceresi tutar (kurtarma bölgesi yaşıyor)
    _tick_price(eng, pos, pos["entry_price"] * 0.921, t0 + 60, monkeypatch)
    assert eng.positions == [pos]


def test_stop_gec_preserved_after_30min(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.97, t0 + v5.GRACE_SEC + 1, monkeypatch)
    assert _trades(v5_data_dir)[-1]["exit_reason"] == "stop_gec"


def test_timeout_60_preserved(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.005, t0 + v5.CEILING_SEC + 1, monkeypatch)
    assert _trades(v5_data_dir)[-1]["exit_reason"] == "timeout_60"


# F2: tp yarım + koşucu

def test_tp_sells_half_and_keeps_runner(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    amount0 = pos["amount_token"]
    cost0 = pos["cost_usd"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)
    # pozisyon kapanmadı, yarıya indi ve koşucu oldu
    assert eng.positions == [pos]
    assert pos["runner"] is True
    assert pos["amount_token"] == pytest.approx(amount0 / 2)
    assert pos["cost_usd"] == pytest.approx(cost0 / 2, abs=0.01)
    t = _trades(v5_data_dir)[-1]
    assert t["exit_reason"] == "tp_2_yarim"
    assert t["pnl_usd"] > 0
    assert t["cost_usd"] == pytest.approx(cost0 / 2, abs=0.01)
    # yarım satış cooldown BAŞLATMAZ (token hâlâ pozisyonda)
    assert pos["token_address"] not in eng._cooldown_until


def test_runner_trails_from_peak(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)   # yarım tp
    _tick_price(eng, pos, pos["entry_price"] * 1.10, t0 + 120, monkeypatch)  # koşucu tepe +%10
    assert eng.positions == [pos]
    _tick_price(eng, pos, pos["entry_price"] * 1.10 * 0.96, t0 + 180, monkeypatch)  # tepeden -%4
    assert eng.positions == []
    t = _trades(v5_data_dir)[-1]
    assert t["exit_reason"] == "trail"
    assert t["runner"] is True
    assert t["pnl_usd"] > 0


def test_runner_breakeven_floor(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)    # yarım tp
    _tick_price(eng, pos, pos["entry_price"] * 1.014, t0 + 120, monkeypatch)  # giriş+%1.4
    t = _trades(v5_data_dir)[-1]
    assert t["exit_reason"] == "breakeven"
    assert t["runner"] is True


def test_runner_timeout_60(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)
    # koşucu +%1.6 ile sıkıştı (trail/be tetiklenmedi): 60dk tavanında kapanır
    _tick_price(eng, pos, pos["entry_price"] * 1.016, t0 + v5.CEILING_SEC + 1, monkeypatch)
    t = _trades(v5_data_dir)[-1]
    assert t["exit_reason"] == "timeout_60"
    assert t["runner"] is True


def test_tp_does_not_refire_on_runner(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)
    _tick_price(eng, pos, pos["entry_price"] * 1.025, t0 + 120, monkeypatch)  # yine +%2 üstü
    assert eng.positions == [pos]  # ikinci yarım satış YOK
    assert len(_trades(v5_data_dir)) == 1


def test_realized_matches_balance_after_full_cycle(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)
    _tick_price(eng, pos, pos["entry_price"] * 1.10, t0 + 120, monkeypatch)
    _tick_price(eng, pos, pos["entry_price"] * 1.10 * 0.96, t0 + 180, monkeypatch)
    # bakiye = start - maliyet - giriş gas + iki satışın proceeds'i; realized tutarlı
    trades = _trades(v5_data_dir)
    assert len(trades) == 2
    total_pnl = sum(t["pnl_usd"] for t in trades)
    assert eng.realized_pnl == pytest.approx(total_pnl, abs=0.01)
    from hibrit_trader.config import GAS_COST_USD
    gas = GAS_COST_USD.get("solana", 0.1)  # giriş gas'ı realized'a değil bakiyeye yansır
    assert eng.balance == pytest.approx(1000.0 + total_pnl - gas, abs=0.01)


# F3: shadow izleme

def test_shadow_registered_and_written(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.91, t0 + 60, monkeypatch)  # stop_felaket
    assert pos["trade_id"] in eng._shadow_watch
    # 20dk sonra poll: shadow satırı yazılır
    exit_price = pos["entry_price"] * 0.91
    monkeypatch.setattr(v5, "fetch_pool_price", lambda c, ch, p: exit_price * 1.05)
    monkeypatch.setattr(v5.time, "time", lambda: t0 + 60 + 1201)
    eng._poll_shadow(client=SimpleNamespace())
    rows = [json.loads(l) for l in (v5_data_dir / v5.SHADOW_FILE).read_text().splitlines()]
    assert rows[-1]["trade_id"] == pos["trade_id"]
    assert rows[-1]["exit_reason"] == "stop_felaket"
    assert rows[-1]["max_vs_exit_pct"] == pytest.approx(5.0, abs=0.1)
    assert pos["trade_id"] not in eng._shadow_watch


def test_half_tp_does_not_register_shadow(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    assert eng._shadow_watch == {}  # koşucu içeride, izlemeye gerek yok


# ---- Kayıt + izolasyon ---------------------------------------------------------------

def test_trade_rows_have_full_fields(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)
    _tick_price(eng, pos, pos["entry_price"] * 1.014, t0 + 120, monkeypatch)
    for t in _trades(v5_data_dir):
        for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
                  "chg_h1", "liq_entry", "mfe_pct", "mae_pct", "runner"):
            assert k in t, k


def test_writes_only_v5_files(v5_data_dir, monkeypatch):
    eng = V5Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.91, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in v5_data_dir.iterdir())
    assert all(f.startswith("v5_") for f in files), files
    state = json.loads((v5_data_dir / v5.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0
