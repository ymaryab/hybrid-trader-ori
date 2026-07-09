"""fast_price servisi testleri: bellek tablosu, bayatlik, parse, backoff, kill switch."""

from __future__ import annotations

import json
import time

import pytest

import hibrit_trader.fast_price as fp
from hibrit_trader.fast_price import FastPriceFeed, get_feed


@pytest.fixture(autouse=True)
def fp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    return tmp_path


# ---- get_price: tazelik sozlesmesi -------------------------------------------------

def test_get_price_fresh_returns_price_and_ts():
    feed = FastPriceFeed()
    ts = time.time()
    feed._prices["P1"] = (1.5, ts)
    assert feed.get_price("P1") == (1.5, ts)


def test_get_price_stale_returns_none():
    feed = FastPriceFeed()
    feed._prices["P1"] = (1.5, time.time() - fp.STALE_SEC - 1)
    assert feed.get_price("P1") is None


def test_get_price_unknown_or_nonpositive_returns_none():
    feed = FastPriceFeed()
    assert feed.get_price("YOK") is None
    feed._prices["P0"] = (0.0, time.time())
    assert feed.get_price("P0") is None


# ---- _poll_once: DexScreener batched pairs parse -----------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return _FakeResp(self.payload)


def test_poll_once_stores_prices_by_pool():
    feed = FastPriceFeed()
    feed._pools = ["P1", "P2"]
    client = _FakeClient({"pairs": [
        {"pairAddress": "P1", "priceUsd": "100.5"},
        {"pairAddress": "P2", "priceUsd": "0.002"},
        {"pairAddress": "P3", "priceUsd": None},        # fiyatsiz: atlanir
        {"pairAddress": "", "priceUsd": "1"},           # adressiz: atlanir
    ]})
    feed._poll_once(client)
    assert feed.get_price("P1")[0] == 100.5
    assert feed.get_price("P2")[0] == 0.002
    assert feed.get_price("P3") is None
    assert "P1,P2" in client.urls[0]


def test_poll_once_partial_response_keeps_old_entries():
    feed = FastPriceFeed()
    ts_old = time.time()
    feed._prices["P2"] = (7.0, ts_old)
    feed._pools = ["P1", "P2"]
    feed._poll_once(_FakeClient({"pairs": [{"pairAddress": "P1", "priceUsd": "3.0"}]}))
    assert feed.get_price("P1")[0] == 3.0
    assert feed.get_price("P2") == (7.0, ts_old)  # eski kayit silinmez, bayatlik esigi korur


# ---- evren dosyasi okuma ------------------------------------------------------------

def test_load_pools_reads_m1_universe(fp_data_dir):
    (fp_data_dir / fp.UNIVERSE_FILE).write_text(json.dumps({
        "tokens": [
            {"symbol": "SOL", "pool_address": "PA"},
            {"symbol": "X", "pool_address": ""},
            {"symbol": "Y"},
        ],
    }))
    feed = FastPriceFeed()
    feed._load_pools()
    assert feed._pools == ["PA"]


def test_load_pools_missing_file_keeps_empty(fp_data_dir):
    feed = FastPriceFeed()
    feed._load_pools()
    assert feed._pools == []
    assert feed._pools_ts > 0  # tekrar denemeden once bekler, dosyayi dovmez


# ---- hata backoff'u -----------------------------------------------------------------

def test_backoff_doubles_and_caps():
    feed = FastPriceFeed()
    feed._register_error("http 429")
    assert feed._backoff == 2.0
    feed._register_error("http 429")
    assert feed._backoff == 4.0
    for _ in range(10):
        feed._register_error("http 429")
    assert feed._backoff == fp.BACKOFF_MAX_SEC
    assert feed._err_streak == 12


# ---- kill switch ---------------------------------------------------------------------

def test_get_feed_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(fp, "ENABLED", False)
    monkeypatch.setattr(fp, "_feed", None)
    assert get_feed() is None


# ---- dinamik havuz (v6 hizli goz) ---------------------------------------------------

def test_add_remove_pool_dynamic_watchlist():
    feed = FastPriceFeed()
    feed._pools = ["P1"]
    feed.add_pool("EXTRA1")
    feed.add_pool("P1")  # evrende zaten var: cift sayilmaz
    assert feed._watched_pools() == ["P1", "EXTRA1"]
    feed.remove_pool("EXTRA1")
    assert feed._watched_pools() == ["P1"]


def test_remove_pool_drops_cached_price():
    feed = FastPriceFeed()
    feed.add_pool("EXTRA1")
    feed._prices["EXTRA1"] = (5.0, time.time())
    feed.remove_pool("EXTRA1")
    assert feed.get_price("EXTRA1") is None


def test_poll_once_chunks_over_30_pools():
    feed = FastPriceFeed()
    feed._pools = [f"P{i}" for i in range(30)]
    for i in range(5):
        feed.add_pool(f"E{i}")
    client = _FakeClient({"pairs": []})
    feed._poll_once(client)
    assert len(client.urls) == 2  # 35 havuz -> 30 + 5 iki istek
    assert client.urls[0].count(",") == 29
    assert client.urls[1].count(",") == 4
