"""M1 senaryo motoru testleri: v7 iskeleti, major evren olcekleri."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.m1_session as m1
from hibrit_trader.m1_session import M1Engine


@pytest.fixture(autouse=True)
def m1_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    # fast feed testte kapali: gercek thread/HTTP acilmasin, polling yolu test edilsin
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
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
    monkeypatch.setattr(m1.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- Giris bandi: h1 1.5..15, liq >= 3M --------------------------------------------

def test_entry_rejects_h1_above_15(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=15.1)) == []
    assert _enter(eng, monkeypatch, _pair(h1=120.0)) == []


def test_entry_accepts_h1_band_edges(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(h1=15.0))) == 1
    eng2 = M1Engine(_settings())
    assert len(_enter(eng2, monkeypatch, _pair(h1=1.5))) == 1


def test_entry_rejects_h1_below_1_5(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=1.4)) == []


def test_entry_rejects_liq_below_3m(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=2_900_000)) == []


# ---- Rejim: sol_h1 < 0.3 giris yok ---------------------------------------------------

def test_regime_threshold_0_3(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.29) == []
    assert len(_enter(eng, monkeypatch, _pair(), sol_h1=0.3)) == 1


# ---- Siralama: en yuksek m5 (taze ivme) once ----------------------------------------

def test_candidates_sorted_highest_m5_first(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    slow = _pair(pool="PS", token="TS", h1=10.0, m5=0.1)
    fresh = _pair(pool="PF", token="TF", h1=3.0, m5=1.2)
    positions = _enter(eng, monkeypatch, [slow, fresh])
    assert positions[0]["pool_address"] == "PF"


def test_entry_keeps_cooldown(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    eng._cooldown_until["MT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- Cikislar (major olcekli) --------------------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 200.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(m1, "fetch_pool_price", lambda c, ch, p: price)
    monkeypatch.setattr(m1.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / m1.TRADES_FILE).read_text().splitlines()[-1])


def test_tp_1_2_and_win_cooldown_30(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.013, t0 + 60, monkeypatch)
    t = _last(m1_data_dir)
    assert t["exit_reason"] == "tp_1_2"
    assert t["pnl_usd"] > 0
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + m1.COOLDOWN_EXIT_SEC
    )
    assert m1.COOLDOWN_EXIT_SEC == 30 * 60


def test_patience_holds_above_brake(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    # -%3.9: fren tetiklenmez, 20dk sabir tutar
    _tick_price(eng, pos, pos["entry_price"] * 0.961, pos["opened_ts"] + m1.GRACE_SEC - 1, monkeypatch)
    assert eng.positions == [pos]


def test_brake_fires_at_minus_4(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.955, t0 + 60, monkeypatch)
    assert eng.positions == []
    t = _last(m1_data_dir)
    assert t["exit_reason"] == "stop_felaket"
    # kayipli cikis: 60dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + m1.COOLDOWN_LOSS_SEC
    )
    assert m1.COOLDOWN_LOSS_SEC == 60 * 60


def test_late_stop_after_20min(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.98, t0 + m1.GRACE_SEC + 1, monkeypatch)
    t = _last(m1_data_dir)
    assert t["exit_reason"] == "stop_gec"


def test_timeout_90(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.005, pos["opened_ts"] + m1.CEILING_SEC + 1, monkeypatch)
    assert _last(m1_data_dir)["exit_reason"] == "timeout_90"


# ---- Friction major derinliginde ~sifir ----------------------------------------------

def test_friction_tiny_at_major_liquidity(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng, liq=5_000_000.0)
    _tick_price(eng, pos, pos["entry_price"] * 1.013, pos["opened_ts"] + 60, monkeypatch)
    t = _last(m1_data_dir)
    # $200 bilet / $5M havuz: slip %0.004 x2, beklenen %0.05-0.1 bandinin altinda
    assert t["friction_pct"] < 0.1


# ---- sol_h1 kaydi + tam set + izolasyon ----------------------------------------------

def test_sol_h1_recorded_in_trade(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    assert pos["sol_chg_h1"] == 0.77
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    t = _last(m1_data_dir)
    assert t["sol_chg_h1"] == 0.77
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_m5", "chg_h1", "liq_entry", "mfe_pct", "mae_pct", "friction_pct"):
        assert k in t, k


def test_writes_only_m1_files(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.013, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in m1_data_dir.iterdir())
    assert all(f.startswith("m1_") for f in files), files
    state = json.loads((m1_data_dir / m1.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0


# ---- Evren: kur, 3M alti eleme, gunde bir tazele --------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """DexScreener token sorgusuna sahte havuz dondurur, GoPlus'a bos (fail-open)."""

    def __init__(self, liq_by_addr):
        self.liq_by_addr = liq_by_addr

    def get(self, url, **kw):
        if "goplus" in url or "token_security" in url:
            return _FakeResp({"result": {}})
        addr = url.rsplit("/", 1)[-1]
        liq = self.liq_by_addr.get(addr)
        if liq is None:
            return _FakeResp({"pairs": []})
        return _FakeResp({"pairs": [{
            "chainId": "solana", "dexId": "raydium",
            "pairAddress": f"POOL_{addr[:6]}",
            "baseToken": {"address": addr, "symbol": "TK"},
            "quoteToken": {"symbol": "USDC"},
            "priceUsd": "1.0",
            "liquidity": {"usd": liq},
            "volume": {}, "priceChange": {"h1": 2.0, "m5": 0.5}, "txns": {},
        }]})


def test_universe_refresh_filters_liq_and_persists(m1_data_dir, monkeypatch):
    monkeypatch.setattr(m1.time, "sleep", lambda s: None)
    seeds = dict(list(m1.SEED_TOKENS.items())[:3])
    monkeypatch.setattr(m1, "SEED_TOKENS", seeds)
    addrs = list(seeds.values())
    liqs = {addrs[0]: 9_000_000.0, addrs[1]: 500_000.0, addrs[2]: 3_100_000.0}
    eng = M1Engine(_settings())
    eng._refresh_universe(_FakeClient(liqs))
    assert [t["token_address"] for t in eng._universe] == [addrs[0], addrs[2]]
    saved = json.loads((m1_data_dir / m1.UNIVERSE_FILE).read_text())
    assert len(saved["tokens"]) == 2
    assert saved["tokens"][0]["liq_usd"] == 9_000_000.0


def test_universe_loaded_from_file_no_refresh_when_fresh(m1_data_dir, monkeypatch):
    (m1_data_dir / m1.UNIVERSE_FILE).write_text(json.dumps({
        "updated_ts": time.time(),
        "tokens": [{"symbol": "SOL", "token_address": "A1", "pool_address": "P1", "liq_usd": 9e6}],
    }))
    eng = M1Engine(_settings())
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
    assert called == []  # taze evren: tazeleme cagrilmadi
    assert len(pairs) == 1
    assert pairs[0].pool_address == "P1"


def test_universe_stale_triggers_refresh(m1_data_dir, monkeypatch):
    (m1_data_dir / m1.UNIVERSE_FILE).write_text(json.dumps({
        "updated_ts": time.time() - 25 * 3600,
        "tokens": [{"symbol": "SOL", "token_address": "A1", "pool_address": "P1", "liq_usd": 9e6}],
    }))
    eng = M1Engine(_settings())
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


# ---- Hizli cikis kadansi (fast feed) ----------------------------------------------------

class _StubFeed:
    def __init__(self, prices):
        self.prices = prices  # pool -> (price, sample_ts)

    def get_price(self, pool, max_age_sec=None):
        return self.prices.get(pool)


def test_fast_exit_tick_closes_tp_with_fast_source(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    sample_ts = time.time() - 0.4
    feed = _StubFeed({pos["pool_address"]: (pos["entry_price"] * 1.013, sample_ts)})
    monkeypatch.setattr(m1, "get_feed", lambda: feed)
    eng.fast_exit_tick()
    t = _last(m1_data_dir)
    assert t["exit_reason"] == "tp_1_2"
    assert t["price_source"] == "fast"
    assert 0 <= t["tetik_gecikme_sec"] < 5


def test_fast_exit_tick_fires_disaster_stop_too(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    feed = _StubFeed({pos["pool_address"]: (pos["entry_price"] * 0.955, time.time())})
    monkeypatch.setattr(m1, "get_feed", lambda: feed)
    eng.fast_exit_tick()
    assert _last(m1_data_dir)["exit_reason"] == "stop_felaket"


def test_fast_exit_tick_skips_position_without_fresh_price(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    monkeypatch.setattr(m1, "get_feed", lambda: _StubFeed({}))
    eng.fast_exit_tick()
    assert eng.positions == [pos]  # taze fiyat yoksa dokunmaz, 30s tick kapsar
    assert not (m1_data_dir / m1.TRADES_FILE).exists()


def test_fast_exit_tick_noop_when_feed_disabled(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    pos["last_price"] = pos["entry_price"] * 1.05  # tp'lik fiyat bile olsa
    eng.fast_exit_tick()  # autouse fixture ENABLED=False -> get_feed None
    assert len(eng.positions) == 1


def test_manage_exits_poll_path_records_source_and_null_delay(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.013, pos["opened_ts"] + 60, monkeypatch)
    t = _last(m1_data_dir)
    assert t["price_source"] == "poll"
    assert t["tetik_gecikme_sec"] is None


def test_manage_exits_prefers_fast_feed_over_poll(m1_data_dir, monkeypatch):
    eng = M1Engine(_settings())
    pos = _open(eng)
    feed = _StubFeed({pos["pool_address"]: (pos["entry_price"] * 1.013, time.time())})
    monkeypatch.setattr(m1, "get_feed", lambda: feed)

    def _boom(c, ch, p):
        raise AssertionError("feed tazeyken polling zinciri cagrilmamali")

    monkeypatch.setattr(m1, "fetch_pool_price", _boom)
    eng._manage_exits(client=SimpleNamespace())
    t = _last(m1_data_dir)
    assert t["exit_reason"] == "tp_1_2"
    assert t["price_source"] == "fast"
