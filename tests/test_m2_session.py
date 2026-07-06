"""M2 senaryo motoru testleri: v10 iskeleti (saf tp), major evren, M1 ile ortak evren dosyasi."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.m2_session as m2
from hibrit_trader.m2_session import M2Engine


@pytest.fixture(autouse=True)
def m2_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="MP1", token="MT1", price=1.0, liq=5_000_000.0, h1=3.0, m5=0.5):
    return SimpleNamespace(
        name="M / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(eng, "_scan_universe", lambda client: pairs)
    monkeypatch.setattr(m2.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- Giris bandi: h1 1.5..15, liq >= 3M --------------------------------------------

def test_entry_rejects_h1_above_15(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=15.1)) == []


def test_entry_accepts_h1_band_edges(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(h1=15.0))) == 1
    eng2 = M2Engine(_settings())
    assert len(_enter(eng2, monkeypatch, _pair(h1=1.5))) == 1


def test_entry_rejects_h1_below_1_5(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=1.4)) == []


def test_entry_rejects_liq_below_3m(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=2_900_000)) == []


# ---- Siralama: en yuksek h1 once (v10 mirasi) ----------------------------------------

def test_candidates_sorted_highest_h1_first(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    low = _pair(pool="PL", token="TL", h1=3.0, m5=2.0)
    high = _pair(pool="PH", token="TH", h1=12.0, m5=0.1)
    positions = _enter(eng, monkeypatch, [low, high])
    assert positions[0]["pool_address"] == "PH"


# ---- Rejim ve cooldown YOK ------------------------------------------------------------

def test_no_regime_filter_but_sol_h1_recorded(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    positions = _enter(eng, monkeypatch, _pair(), sol_h1=-2.5)
    assert len(positions) == 1  # sol_h1 negatifken bile giris var (filtre degil)
    assert positions[0]["sol_chg_h1"] == -2.5


def test_no_cooldown_after_exit(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.013, pos["opened_ts"] + 60, monkeypatch)
    assert eng.positions == []
    # ayni token hemen tekrar alinabilir: cooldown mekanizmasi hic yok
    assert not hasattr(eng, "_cooldown_until")
    assert len(_enter(eng, monkeypatch, _pair())) == 1


# ---- Cikis: SADECE tp_1_2 --------------------------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 200.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(m2, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(m2.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / m2.TRADES_FILE).read_text().splitlines()[-1])


def test_tp_1_2_fires(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.013, pos["opened_ts"] + 60, monkeypatch)
    t = _last(m2_data_dir)
    assert t["exit_reason"] == "tp_1_2"
    assert t["pnl_usd"] > 0


def test_no_stop_no_timeout_holds_forever(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # -%30'da bile satmaz (stop yok), 10 saat sonra bile satmaz (timeout yok)
    _tick_price(eng, pos, pos["entry_price"] * 0.70, t0 + 10 * 3600, monkeypatch)
    assert eng.positions == [pos]
    assert not (m2_data_dir / m2.TRADES_FILE).exists()
    # sonra toparlayip tp'ye degerse satar
    _tick_price(eng, pos, pos["entry_price"] * 1.013, t0 + 20 * 3600, monkeypatch)
    assert _last(m2_data_dir)["exit_reason"] == "tp_1_2"


def test_mae_tracks_drawdown_while_holding(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.90, pos["opened_ts"] + 60, monkeypatch)
    assert pos["mae_pct"] == pytest.approx(-10.0, abs=0.1)


# ---- Friction major derinliginde ~sifir ------------------------------------------------

def test_friction_tiny_at_major_liquidity(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    pos = _open(eng, liq=5_000_000.0)
    _tick_price(eng, pos, pos["entry_price"] * 1.013, pos["opened_ts"] + 60, monkeypatch)
    assert _last(m2_data_dir)["friction_pct"] < 0.1


# ---- Kayit tam set + izolasyon ---------------------------------------------------------

def test_trade_record_full_set(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    t = _last(m2_data_dir)
    assert t["sol_chg_h1"] == 0.77
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_m5", "chg_h1", "liq_entry", "mfe_pct", "mae_pct", "friction_pct"):
        assert k in t, k


def test_writes_only_m2_files(m2_data_dir, monkeypatch):
    eng = M2Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.013, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in m2_data_dir.iterdir())
    assert all(f.startswith("m2_") for f in files), files
    state = json.loads((m2_data_dir / m2.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0


# ---- Evren: M1 ile ortak dosya ----------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _universe_row(ts):
    return json.dumps({
        "updated_ts": ts,
        "tokens": [{"symbol": "SOL", "token_address": "A1", "pool_address": "P1", "liq_usd": 9e6}],
    })


def test_reads_m1_universe_file_fresh_no_refresh(m2_data_dir, monkeypatch):
    (m2_data_dir / m2.UNIVERSE_FILE).write_text(_universe_row(time.time()))
    eng = M2Engine(_settings())
    assert eng._universe[0]["symbol"] == "SOL"
    called = []
    monkeypatch.setattr(eng, "_refresh_universe", lambda c: called.append(1))

    class _C:
        def get(self, url, **kw):
            return _FakeResp({"pairs": [{
                "chainId": "solana", "dexId": "raydium", "pairAddress": "P1",
                "baseToken": {"address": "A1", "symbol": "SOL"},
                "quoteToken": {"symbol": "USDC"}, "priceUsd": "100.0",
                "liquidity": {"usd": 9e6}, "volume": {},
                "priceChange": {"h1": 2.0, "m5": 0.5}, "txns": {},
            }]})

    pairs = eng._scan_universe(_C())
    assert called == []
    assert pairs[0].pool_address == "P1"


def test_stale_memory_but_m1_refreshed_file_reloads_without_own_refresh(m2_data_dir, monkeypatch):
    # M2 bellekte bayat, ama M1 dosyayi tazelemis: M2 dosyadan okur, kendi tazelemez
    (m2_data_dir / m2.UNIVERSE_FILE).write_text(_universe_row(time.time() - 25 * 3600))
    eng = M2Engine(_settings())
    (m2_data_dir / m2.UNIVERSE_FILE).write_text(_universe_row(time.time()))
    called = []
    monkeypatch.setattr(eng, "_refresh_universe", lambda c: called.append(1))

    class _C:
        def get(self, url, **kw):
            return _FakeResp({"pairs": []})

    eng._scan_universe(_C())
    assert called == []
    assert time.time() - eng._universe_ts < 3600


def test_file_stale_beyond_takeover_m2_refreshes(m2_data_dir, monkeypatch):
    # M1 kapali senaryosu: dosya 25.5 saatten bayat, M2 tazelemeyi devralir
    (m2_data_dir / m2.UNIVERSE_FILE).write_text(_universe_row(time.time() - 26 * 3600))
    eng = M2Engine(_settings())
    called = []

    def _fake_refresh(client):
        called.append(1)
        eng._universe_ts = time.time()

    monkeypatch.setattr(eng, "_refresh_universe", _fake_refresh)

    class _C:
        def get(self, url, **kw):
            return _FakeResp({"pairs": []})

    eng._scan_universe(_C())
    assert called == [1]
