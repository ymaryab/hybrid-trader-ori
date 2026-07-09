"""Fiyat sanity bandi + rejim fail-closed + evren havuz secimi testleri (ORCA vakasi)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.m1_session as m1
from hibrit_trader.m1_session import M1Engine, _best_sane_pool
from hibrit_trader.m2_session import M2Engine
from hibrit_trader.price_sanity import REBASE_SEC, guard_price


# ---- guard_price: 5x bandi ----------------------------------------------------------

def _pos(last=1.0):
    return {"pair": "T / SOL", "last_price": last, "token_address": "TOK"}


def test_normal_step_passes():
    pos = _pos(1.0)
    price, ariza = guard_price(pos, 1.04, 1000.0, "T")
    assert (price, ariza) == (1.04, False)
    assert "veri_ariza_ts" not in pos


def test_band_edge_5x_passes():
    pos = _pos(1.0)
    price, ariza = guard_price(pos, 5.0, 1000.0, "T")
    assert (price, ariza) == (5.0, False)
    pos = _pos(1.0)
    price, ariza = guard_price(pos, 0.2, 1000.0, "T")
    assert (price, ariza) == (0.2, False)


def test_spike_up_rejected_returns_last():
    pos = _pos(1.0)
    price, ariza = guard_price(pos, 5.01, 1000.0, "T")
    assert (price, ariza) == (1.0, True)
    assert pos["veri_ariza_ts"] == 1000.0
    assert pos["last_price"] == 1.0  # degerleme son gecerli fiyatta


def test_orca_style_collapse_rejected():
    # ORCA vakasi: 6013 -> 1.22 tek adim (~4928x)
    pos = _pos(6013.0)
    price, ariza = guard_price(pos, 1.22, 1000.0, "T")
    assert (price, ariza) == (6013.0, True)


def test_recovery_clears_anomaly():
    pos = _pos(1.0)
    guard_price(pos, 50.0, 1000.0, "T")
    price, ariza = guard_price(pos, 1.02, 1010.0, "T")
    assert (price, ariza) == (1.02, False)
    assert "veri_ariza_ts" not in pos


def test_persistent_shift_rebases_after_window_with_hakem_approval():
    pos = _pos(1.0)
    guard_price(pos, 50.0, 1000.0, "T")
    # pencere icinde hala ariza
    price, ariza = guard_price(pos, 51.0, 1000.0 + REBASE_SEC - 1, "T")
    assert ariza and price == 1.0
    # pencere dolunca hakem onaylarsa yeni taban kabul
    price, ariza = guard_price(pos, 52.0, 1000.0 + REBASE_SEC, "T",
                               hakem=lambda tok: 52.0)
    assert (price, ariza) == (52.0, False)
    assert "veri_ariza_ts" not in pos


def test_rebase_hakem_reddederse_taban_degismez():
    # JTO/PYTH vakasi: kalici bogus carpan, hakem gercek fiyati soyler
    pos = _pos(1.0)
    guard_price(pos, 5000.0, 1000.0, "T")
    price, ariza = guard_price(pos, 5000.0, 1000.0 + REBASE_SEC, "T",
                               hakem=lambda tok: 1.0)
    assert ariza and price == 1.0
    # pencere bastan basladi: hemen sonraki tick yine re-base denemez
    assert pos["veri_ariza_ts"] == 1000.0 + REBASE_SEC


def test_rebase_hakem_ulasamazsa_fail_closed():
    pos = _pos(1.0)
    guard_price(pos, 50.0, 1000.0, "T")
    price, ariza = guard_price(pos, 52.0, 1000.0 + REBASE_SEC, "T",
                               hakem=lambda tok: None)
    assert ariza and price == 1.0


def test_rebase_hakem_hatasi_fail_closed():
    pos = _pos(1.0)
    guard_price(pos, 50.0, 1000.0, "T")

    def _patla(tok):
        raise RuntimeError("jupiter down")

    price, ariza = guard_price(pos, 52.0, 1000.0 + REBASE_SEC, "T", hakem=_patla)
    assert ariza and price == 1.0


def test_zero_or_missing_last_price_passes_through():
    pos = {"pair": "T", "last_price": 0}
    assert guard_price(pos, 123.0, 1000.0, "T") == (123.0, False)


# ---- motor entegrasyonu: ariza tick'inde islem tetiklenmez ---------------------------

@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _open_pos(entry=1.0):
    now = time.time()
    return {
        "trade_id": "t1", "pair": "T / SOL", "chain": "solana",
        "token_address": "TOK", "pool_address": "POOL",
        "entry_price": entry, "amount_token": 100.0, "cost_usd": 100.0,
        "opened_ts": now, "opened_at": "x", "chg_m5": 0.0, "chg_h1": 3.0,
        "liq_entry": 5_000_000.0, "sol_chg_h1": 1.0, "entry_slip_pct": 0.0,
        "mfe_pct": 0.0, "mae_pct": 0.0, "last_price": entry,
    }


def test_m2_spike_does_not_trigger_tp(data_dir):
    eng = M2Engine(_settings())
    pos = _open_pos(entry=1.0)
    eng.positions = [pos]
    # sapik fiyat +%600: tp tetiklenmemeli, last_price degismemeli
    assert eng._eval_position(pos, 7.0, time.time()) is None
    assert pos["last_price"] == 1.0
    assert pos["mfe_pct"] == 0.0
    # normal fiyat tp tetikler
    assert eng._eval_position(pos, 1.013, time.time()) == "tp_1_2"


def test_m1_collapse_does_not_trigger_stop(data_dir):
    eng = M1Engine(_settings())
    pos = _open_pos(entry=6013.0)
    pos["last_price"] = 6013.0
    eng.positions = [pos]
    assert eng._eval_position(pos, 1.22, time.time()) is None
    assert pos["last_price"] == 6013.0
    assert pos["mae_pct"] == 0.0


# ---- rejim fail-closed ----------------------------------------------------------------

def _pair(pool="MP1", token="MT1", price=1.0, liq=5_000_000.0, h1=3.0, m5=0.5):
    return SimpleNamespace(
        name="M / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _raise(*a, **k):
    raise RuntimeError("api down")


def test_m1_regime_fail_closed_no_data(data_dir, monkeypatch):
    eng = M1Engine(_settings())
    monkeypatch.setattr(eng, "_scan_universe", lambda client: [_pair()])
    monkeypatch.setattr(m1.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", _raise)
    eng._enter(client=SimpleNamespace())
    assert eng.positions == []


def test_m1_regime_recent_cache_allows_entry(data_dir, monkeypatch):
    eng = M1Engine(_settings())
    eng._sol_h1_cache = (time.time() - 60, 1.0)  # 1dk once basarili deger
    monkeypatch.setattr(eng, "_scan_universe", lambda client: [_pair()])
    monkeypatch.setattr(m1.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", _raise)
    eng._enter(client=SimpleNamespace())
    assert len(eng.positions) == 1


def test_m1_regime_stale_cache_blocks_entry(data_dir, monkeypatch):
    eng = M1Engine(_settings())
    eng._sol_h1_cache = (time.time() - 700, 1.0)  # 10dk+ eski deger
    monkeypatch.setattr(eng, "_scan_universe", lambda client: [_pair()])
    monkeypatch.setattr(m1.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", _raise)
    eng._enter(client=SimpleNamespace())
    assert eng.positions == []


# ---- evren havuz secimi: stabil-kota referansli sanity --------------------------------

def _ds_pair(addr, quote, price, liq):
    return {
        "pairAddress": addr,
        "quoteToken": {"symbol": quote},
        "priceUsd": str(price),
        "liquidity": {"usd": liq},
    }


def test_best_sane_pool_rejects_orca_style_liquid_but_insane():
    # ORCA vakasi: en likit havuzlar sapik fiyat, kucuk USDC/SOL havuzlari dogru
    pairs = [
        _ds_pair("BAD1", "JUP", 5997.10, 24_317_016),
        _ds_pair("BAD2", "MET", 6202.20, 4_141_265),
        _ds_pair("OK1", "SOL", 1.21, 712_397),
        _ds_pair("OK2", "USDC", 1.20, 656_741),
    ]
    best = _best_sane_pool(pairs)
    assert best is not None and best["pairAddress"] == "OK1"


def test_best_sane_pool_normal_token_picks_most_liquid():
    pairs = [
        _ds_pair("A", "SOL", 1.19, 9_000_000),
        _ds_pair("B", "USDC", 1.20, 2_000_000),
    ]
    best = _best_sane_pool(pairs)
    assert best is not None and best["pairAddress"] == "A"


def test_best_sane_pool_no_price_returns_none():
    assert _best_sane_pool([_ds_pair("A", "USDC", 0, 1_000_000)]) is None


def test_best_sane_pool_jupiter_hakem_bogus_medyani_ezer():
    # JTO vakasi: stabil kotali havuz YOK, cogunluk bogus (~5000x). Medyan
    # fallback bogus havuzu secerdi; Jupiter hakem referansi dogru havuzu secer.
    pairs = [
        _ds_pair("BAD1", "MET", 3335.40, 56_943_339),
        _ds_pair("BAD2", "JUP", 3205.39, 6_899_775),
        _ds_pair("BAD3", "JUP", 3199.81, 6_803_476),
        _ds_pair("OK1", "JitoSOL", 0.6401, 1_468_642),
    ]
    kor = _best_sane_pool(pairs)  # hakemsiz: medyan bogus, sapik havuz secilir
    assert kor is not None and kor["pairAddress"] == "BAD1"
    best = _best_sane_pool(pairs, ref_fiyat=0.6406)  # hakemli: dogru havuz
    assert best is not None and best["pairAddress"] == "OK1"


# ---- evren tazeleme: Jupiter hakem fail-closed ----------------------------------------

class _RefreshClient:
    def __init__(self, pairs):
        self._pairs = pairs

    def get(self, url, params=None, timeout=None, headers=None):
        if "/latest/dex/tokens/" in url:
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"pairs": self._pairs})
        raise RuntimeError("honeypot servisi testte yok")  # fail-open


def _refresh_pairs(addr):
    def p(pair_addr, quote, price, liq):
        return {
            "chainId": "solana", "pairAddress": pair_addr,
            "baseToken": {"address": addr}, "quoteToken": {"symbol": quote},
            "priceUsd": str(price), "liquidity": {"usd": liq},
        }
    return [
        p("BOGUS", "MET", 3335.40, 56_000_000),
        p("SANE", "SOL", 0.64, 5_000_000),
    ]


def test_refresh_hakem_yoksa_token_disarida(data_dir, monkeypatch):
    eng = M1Engine(_settings())
    monkeypatch.setattr(m1, "SEED_TOKENS", {"TST": "TOKADDR"})
    monkeypatch.setattr(m1, "jupiter_referans_fiyat", lambda addr: None)
    monkeypatch.setattr(m1.time, "sleep", lambda s: None)
    eng._refresh_universe(_RefreshClient(_refresh_pairs("TOKADDR")))
    assert eng._universe == []


def test_refresh_hakem_dogru_havuzu_sectirir(data_dir, monkeypatch):
    eng = M1Engine(_settings())
    monkeypatch.setattr(m1, "SEED_TOKENS", {"TST": "TOKADDR"})
    monkeypatch.setattr(m1, "jupiter_referans_fiyat", lambda addr: 0.6406)
    monkeypatch.setattr(m1.time, "sleep", lambda s: None)
    eng._refresh_universe(_RefreshClient(_refresh_pairs("TOKADDR")))
    assert len(eng._universe) == 1
    entry = eng._universe[0]
    assert entry["pool_address"] == "SANE"
    assert entry["ref_fiyat"] == 0.6406


# ---- giris kapisi: hakem referansindan 5x+ sapan fiyata giris yok ----------------------

def test_m1_giris_veri_ariza_reject(data_dir, monkeypatch):
    eng = M1Engine(_settings())
    eng._universe = [{"symbol": "M", "token_address": "MT1",
                      "pool_address": "MP1", "ref_fiyat": 0.64}]
    eng._universe_ts = time.time()
    monkeypatch.setattr(eng, "_scan_universe",
                        lambda client: [_pair(price=3335.4)])  # ~5200x sapik
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: 1.0)
    monkeypatch.setattr(m1.time, "sleep", lambda s: None)
    eng._enter(client=SimpleNamespace())
    assert eng.positions == []


def test_m1_giris_saglam_fiyat_gecer(data_dir, monkeypatch):
    eng = M1Engine(_settings())
    eng._universe = [{"symbol": "M", "token_address": "MT1",
                      "pool_address": "MP1", "ref_fiyat": 0.64}]
    eng._universe_ts = time.time()
    monkeypatch.setattr(eng, "_scan_universe",
                        lambda client: [_pair(price=0.65)])
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: 1.0)
    monkeypatch.setattr(m1.time, "sleep", lambda s: None)
    eng._enter(client=SimpleNamespace())
    assert len(eng.positions) == 1


def test_m2_giris_veri_ariza_reject(data_dir, monkeypatch):
    eng = M2Engine(_settings())
    eng._universe = [{"symbol": "M", "token_address": "MT1",
                      "pool_address": "MP1", "ref_fiyat": 0.64}]
    eng._universe_ts = time.time()
    monkeypatch.setattr(eng, "_scan_universe",
                        lambda client: [_pair(price=3335.4)])
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: 1.0)
    eng._enter(client=SimpleNamespace())
    assert eng.positions == []
