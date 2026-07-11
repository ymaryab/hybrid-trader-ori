"""V6 senaryo motoru testleri: gölge birebir + TEK ek h1 bandı 10..50 + sol_h1 kaydı."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v6_session as v6
from hibrit_trader.v6_session import V6Engine


@pytest.fixture(autouse=True)
def v6_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    # fast feed testte kapali: gercek thread/HTTP acilmasin, polling yolu test edilsin
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    # rejim_reject_kaydet: paylasilan kuyruk temiz, gercek daemon thread acilmasin
    monkeypatch.setattr("hibrit_trader.entry_fresh._watch", {})
    monkeypatch.setattr("hibrit_trader.entry_fresh._start_recheck_thread", lambda: None)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="YP1", token="YT1", price=1.0, liq=150_000.0, h1=15.0, m5=-2.0):
    return SimpleNamespace(
        name="Y / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(v6, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        v6, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(v6.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- TEK fark: h1 bandi 10..50 ---------------------------------------------------

def test_entry_rejects_h1_above_50(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    # RMG tipi dikey pump: golge girerdi, V6 girmez
    assert _enter(eng, monkeypatch, _pair(h1=459658.0)) == []
    assert _enter(eng, monkeypatch, _pair(h1=50.1)) == []


def test_entry_accepts_h1_at_50(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(h1=50.0))) == 1


def test_entry_rejects_h1_below_10(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=9.9)) == []


# ---- Golge'den korunanlar ----------------------------------------------------------

def test_entry_golge_rules_preserved(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=99_000)) == []          # liq >= 100k
    assert len(_enter(eng, monkeypatch, _pair(h1=45.0, m5=-5.0))) == 1  # m5 sarti yok


# ---- Guclendirme 1: rejim esigi 0.5 (0..0.5 bandi kanitli kaybettiren) ---------------

def test_regime_threshold_0_5(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.0) == []
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.49) == []
    assert len(_enter(eng, monkeypatch, _pair(), sol_h1=0.5)) == 1


def test_rejim_kapaliyken_reject_kaydi(v6_data_dir, monkeypatch):
    import hibrit_trader.entry_fresh as ef
    from hibrit_trader.momentum_session import REJECTS_FILE
    eng = V6Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.2) == []
    rows = [json.loads(x) for x in
            (v6_data_dir / REJECTS_FILE).read_text().splitlines()]
    assert rows[-1]["reason"] == "rejim_reject"
    assert rows[-1]["engine"] == "V6"
    assert rows[-1]["sol_chg_h1"] == 0.2
    assert "YP1" in ef._watch


def test_candidates_sorted_highest_h1_first(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    low = _pair(pool="PL", token="TL", h1=12.0)
    high = _pair(pool="PH", token="TH", h1=40.0)
    positions = _enter(eng, monkeypatch, [low, high])
    assert positions[0]["pool_address"] == "PH"


def test_entry_keeps_cooldown(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    eng._cooldown_until["YT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- Cikislar (golge birebir) -------------------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(v6, "fetch_pool_snapshot", lambda c, ch, p: (price, None))
    monkeypatch.setattr(v6.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / v6.TRADES_FILE).read_text().splitlines()[-1])


def test_tp_2(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)
    t = _last(v6_data_dir)
    assert t["exit_reason"] == "tp_2"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + v6.COOLDOWN_EXIT_SEC
    )


def test_no_stop_in_first_30min(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.90, pos["opened_ts"] + v6.GRACE_SEC - 1, monkeypatch)
    assert eng.positions == [pos]  # fren YOK: -%10'da bile sabir tutar


def test_late_stop_after_30min(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.97, t0 + v6.GRACE_SEC + 1, monkeypatch)
    t = _last(v6_data_dir)
    assert t["exit_reason"] == "stop_gec"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + v6.GRACE_SEC + 1 + v6.COOLDOWN_LOSS_SEC
    )


def test_timeout_60(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.005, pos["opened_ts"] + v6.CEILING_SEC + 1, monkeypatch)
    assert _last(v6_data_dir)["exit_reason"] == "timeout_60"


# ---- sol_h1 kaydi + tam set + izolasyon ---------------------------------------------

def test_sol_h1_recorded_in_trade(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    assert pos["sol_chg_h1"] == 0.77
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v6_data_dir)
    assert t["sol_chg_h1"] == 0.77
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_h1", "liq_entry", "mfe_pct", "mae_pct", "friction_pct"):
        assert k in t, k


def test_writes_only_v6_files(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in v6_data_dir.iterdir())
    assert all(f.startswith("v6_") for f in files), files
    state = json.loads((v6_data_dir / v6.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0


# ---- Guclendirme 2: hizli goz (fast feed, 2s kadans) ------------------------------

class _StubFeed:
    def __init__(self, prices):
        self.prices = prices  # pool -> (price, sample_ts)
        self.added = []
        self.removed = []

    def get_price(self, pool, max_age_sec=None):
        return self.prices.get(pool)

    def add_pool(self, pool):
        self.added.append(pool)

    def remove_pool(self, pool):
        self.removed.append(pool)


def test_fast_exit_tick_closes_tp_with_fast_source(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    sample_ts = time.time() - 0.4
    feed = _StubFeed({pos["pool_address"]: (pos["entry_price"] * 1.021, sample_ts)})
    monkeypatch.setattr(v6, "get_feed", lambda: feed)
    eng.fast_exit_tick()
    t = _last(v6_data_dir)
    assert t["exit_reason"] == "tp_2"
    assert t["price_source"] == "fast"
    assert 0 <= t["tetik_gecikme_sec"] < 5
    assert feed.removed == [pos["pool_address"]]  # kapaninca feed'ten cikar


def test_fast_exit_tick_skips_position_without_fresh_price(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    monkeypatch.setattr(v6, "get_feed", lambda: _StubFeed({}))
    eng.fast_exit_tick()
    assert eng.positions == [pos]  # taze fiyat yoksa dokunmaz, 30s tick kapsar
    assert not (v6_data_dir / v6.TRADES_FILE).exists()


def test_fast_exit_tick_noop_when_feed_disabled(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    pos["last_price"] = pos["entry_price"] * 1.05  # tp'lik fiyat bile olsa
    eng.fast_exit_tick()  # autouse fixture ENABLED=False -> get_feed None
    assert len(eng.positions) == 1


def test_open_position_registers_pool_to_feed(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    feed = _StubFeed({})
    monkeypatch.setattr(v6, "get_feed", lambda: feed)
    pos = _open(eng)
    assert feed.added == [pos["pool_address"]]


def test_manage_exits_poll_path_records_source_and_null_delay(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v6_data_dir)
    assert t["price_source"] == "poll"
    assert t["tetik_gecikme_sec"] is None


def test_manage_exits_prefers_fast_feed_over_poll(v6_data_dir, monkeypatch):
    eng = V6Engine(_settings())
    pos = _open(eng)
    feed = _StubFeed({pos["pool_address"]: (pos["entry_price"] * 1.021, time.time())})
    monkeypatch.setattr(v6, "get_feed", lambda: feed)

    def _boom(c, ch, p):
        raise AssertionError("feed tazeyken polling zinciri cagrilmamali")

    monkeypatch.setattr(v6, "fetch_pool_snapshot", _boom)
    eng._manage_exits(client=SimpleNamespace())
    t = _last(v6_data_dir)
    assert t["exit_reason"] == "tp_2"
    assert t["price_source"] == "fast"


# ---- Kill-switch tek-seferlik log (M1 paterni) ---------------------------------------

def test_kill_switch_tek_seferlik_log(v6_data_dir, monkeypatch, caplog):
    import logging
    eng = V6Engine(_settings())
    monkeypatch.setattr(v6, "kill_is_active", lambda: True)
    with caplog.at_level(logging.WARNING, logger="hibrit_trader.v6_session"):
        eng._enter(client=SimpleNamespace())
        eng._enter(client=SimpleNamespace())
        assert sum("kill-switch AKTIF" in r.message for r in caplog.records) == 1
        monkeypatch.setattr(v6, "kill_is_active", lambda: False)
        monkeypatch.setattr(v6, "scan_all", lambda chains: [])
        eng._enter(client=SimpleNamespace())
        assert sum("kill-switch kalkti" in r.message for r in caplog.records) == 1
