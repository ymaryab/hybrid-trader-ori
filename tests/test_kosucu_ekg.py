"""Kosucu EKG izleyici testleri: pasif kayit, islem yok, kosucu_ekg* disina yazmaz."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.kosucu_ekg as ekg_mod
from hibrit_trader.kosucu_ekg import KosucuEkg


@pytest.fixture(autouse=True)
def ekg_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="EP1", token="ET1", price=1.0, liq=50_000.0, h1=10.0):
    return SimpleNamespace(
        name="E / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=0.0, chg_h1=h1,
    )


def _tick(ekg, monkeypatch, pairs, now=None, pool_price=None):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(ekg_mod, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(ekg_mod, "fetch_pool_price", lambda c, ch, p: pool_price)
    if now is not None:
        monkeypatch.setattr(ekg_mod.time, "time", lambda: now)
    ekg.tick()


def _rows(data_dir):
    p = data_dir / ekg_mod.OUT_FILE
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


# ---- terfi tetikleri --------------------------------------------------------------

def test_promotes_on_h1_50(ekg_data_dir, monkeypatch):
    ekg = KosucuEkg(_settings())
    _tick(ekg, monkeypatch, _pair(h1=50.0))
    assert "EP1" in ekg.watch
    assert ekg.watch["EP1"]["trigger"] == "h1_50"


def test_no_promote_below_h1_50(ekg_data_dir, monkeypatch):
    ekg = KosucuEkg(_settings())
    _tick(ekg, monkeypatch, _pair(h1=49.9))
    assert ekg.watch == {}
    assert _rows(ekg_data_dir) == []


def test_promotes_on_growth_50pct(ekg_data_dir, monkeypatch):
    ekg = KosucuEkg(_settings())
    t0 = time.time()
    _tick(ekg, monkeypatch, _pair(price=1.0, h1=5.0), now=t0)
    assert ekg.watch == {}
    _tick(ekg, monkeypatch, _pair(price=1.49, h1=5.0), now=t0 + 60)
    assert ekg.watch == {}
    _tick(ekg, monkeypatch, _pair(price=1.5, h1=5.0), now=t0 + 120)
    assert ekg.watch["EP1"]["trigger"] == "buyume_50"


def test_max_watch_cap(ekg_data_dir, monkeypatch):
    ekg = KosucuEkg(_settings())
    pairs = [_pair(pool=f"P{i}", token=f"T{i}", h1=60.0)
             for i in range(ekg_mod.MAX_WATCH + 5)]
    _tick(ekg, monkeypatch, pairs)
    assert len(ekg.watch) == ekg_mod.MAX_WATCH


# ---- kayit ------------------------------------------------------------------------

def test_records_every_tick_from_scan(ekg_data_dir, monkeypatch):
    ekg = KosucuEkg(_settings())
    t0 = time.time()
    _tick(ekg, monkeypatch, _pair(h1=55.0, price=2.0, liq=80_000), now=t0)
    _tick(ekg, monkeypatch, _pair(h1=55.0, price=2.5, liq=90_000), now=t0 + 30)
    rows = _rows(ekg_data_dir)
    assert len(rows) == 2
    r = rows[-1]
    assert r["pool_address"] == "EP1"
    assert r["token_address"] == "ET1"
    assert r["price_usd"] == 2.5
    assert r["liquidity_usd"] == 90_000
    assert r["kaynak"] == "scan"
    assert r["trigger"] == "h1_50"
    assert r["izleme_dk"] == pytest.approx(0.5)


def test_fallback_pool_api_when_out_of_scan(ekg_data_dir, monkeypatch):
    ekg = KosucuEkg(_settings())
    t0 = time.time()
    _tick(ekg, monkeypatch, _pair(h1=55.0), now=t0)
    _tick(ekg, monkeypatch, [_pair(pool="BASKA", token="BT", h1=1.0)],
          now=t0 + 30, pool_price=3.3)
    r = _rows(ekg_data_dir)[-1]
    assert r["kaynak"] == "pool_api"
    assert r["price_usd"] == 3.3
    assert r["liquidity_usd"] is None


def test_no_record_when_fallback_price_missing(ekg_data_dir, monkeypatch):
    ekg = KosucuEkg(_settings())
    t0 = time.time()
    _tick(ekg, monkeypatch, _pair(h1=55.0), now=t0)
    _tick(ekg, monkeypatch, [], now=t0 + 30, pool_price=None)
    assert len(_rows(ekg_data_dir)) == 1


# ---- sure penceresi ---------------------------------------------------------------

def test_expires_after_watch_window(ekg_data_dir, monkeypatch):
    ekg = KosucuEkg(_settings())
    t0 = time.time()
    _tick(ekg, monkeypatch, _pair(h1=55.0), now=t0)
    assert "EP1" in ekg.watch
    _tick(ekg, monkeypatch, _pair(h1=55.0), now=t0 + ekg_mod.WATCH_SEC + 1)
    # suresi doldu, dusuruldu; ayni tick'te yeniden terfi edebilir ama eski kayit gitti
    w = ekg.watch.get("EP1")
    assert w is None or w["started_ts"] >= t0 + ekg_mod.WATCH_SEC


# ---- kalicilik + izolasyon --------------------------------------------------------

def test_state_survives_restart(ekg_data_dir, monkeypatch):
    ekg = KosucuEkg(_settings())
    _tick(ekg, monkeypatch, _pair(h1=55.0))
    ekg2 = KosucuEkg(_settings())
    assert "EP1" in ekg2.watch
    assert ekg2.watch["EP1"]["trigger"] == "h1_50"


def test_writes_only_ekg_files(ekg_data_dir, monkeypatch):
    ekg = KosucuEkg(_settings())
    _tick(ekg, monkeypatch, _pair(h1=55.0))
    files = sorted(p.name for p in ekg_data_dir.iterdir())
    assert all(f.startswith("kosucu_ekg") for f in files), files
