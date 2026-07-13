"""Hizli fiyat servisi: major evren havuzlarini ~1s kadansla bellekte tutar.

Kaynak: DexScreener batched pairs endpoint'i (girisle AYNI kaynak/sema,
tek istekte 30 havuz, resmi limit 300 istek/dk; 1 Hz = limitin 1/5'i).
M1 ve M2 cikis kontrolleri fiyati buradan okur; feed bayat/kapali/hatali
ise motorlar mevcut polling zincirine (fetch_pool_price) geri duser,
yani HICBIR kosulda kor kalmazlar.

Tek instance (process ici singleton daemon thread). Kullanicilar: M1/M2,
v6 ve v7 (12 Tem canli asimetri B2: v7 canli para tasidigi icin hizli goz
eklendi). X1/live_sim bu modulu import ETMEZ. FAST_PRICE_ENABLED=0 ile
tamamen kapatilir (get_feed None doner, motorlar eski davranista kalir).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import httpx

from hibrit_trader.config import API
from hibrit_trader.momentum_session import _data_dir

log = logging.getLogger(__name__)

ENABLED = os.getenv("FAST_PRICE_ENABLED", "1") != "0"
INTERVAL_SEC = float(os.getenv("FAST_PRICE_INTERVAL_SEC", "1.0"))
STALE_SEC = float(os.getenv("FAST_PRICE_STALE_SEC", "10"))
UNIVERSE_RELOAD_SEC = 60.0
BACKOFF_MAX_SEC = 60.0
UNIVERSE_FILE = "m1_universe.json"  # M1/M2'nin ortak major evreni


class FastPriceFeed:
    """Bellek-ici fiyat tablosu: {pool_address: (price_usd, sample_ts)}."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prices: dict[str, tuple[float, float]] = {}
        self._pools: list[str] = []
        # dinamik: acik pozisyon havuzlari, pool -> pozisyon sayaci.
        # 13 Tem T3 otopsisi: duz set iken ortak havuzda ilk kapatan motor
        # digerlerinin fast gozunu kor ediyordu (refcount tamiri).
        self._extra_pools: dict[str, int] = {}
        self._pools_ts: float = 0.0
        self._thread: threading.Thread | None = None
        self._backoff: float = 0.0
        self._err_streak: int = 0

    def add_pool(self, pool_address: str) -> None:
        """Evren disi bir havuzu gecici izlemeye al (pozisyon acilinca)."""
        if pool_address:
            with self._lock:
                self._extra_pools[pool_address] = self._extra_pools.get(pool_address, 0) + 1

    def remove_pool(self, pool_address: str) -> None:
        """Gecici izlemeyi birak (pozisyon kapaninca). Fiyat kaydi ancak
        havuzu izleyen SON pozisyon kapaninca silinir (sayac sifir)."""
        with self._lock:
            kalan = self._extra_pools.get(pool_address, 0) - 1
            if kalan > 0:
                self._extra_pools[pool_address] = kalan
                return
            self._extra_pools.pop(pool_address, None)
            self._prices.pop(pool_address, None)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="fast-price", daemon=True
            )
        self._thread.start()

    def get_price(self, pool_address: str,
                  max_age_sec: float = STALE_SEC) -> tuple[float, float] | None:
        """(price_usd, sample_ts) dondurur; taze kayit yoksa None (fallback sinyali)."""
        with self._lock:
            rec = self._prices.get(pool_address)
        if rec is None:
            return None
        price, ts = rec
        if price <= 0 or time.time() - ts > max_age_sec:
            return None
        return price, ts

    # ---- ic dongu ---------------------------------------------------------------
    def _load_pools(self) -> None:
        p = _data_dir() / UNIVERSE_FILE
        try:
            data = json.loads(p.read_text())
            pools = [str(t.get("pool_address") or "") for t in (data.get("tokens") or [])]
            self._pools = [x for x in pools if x][:30]
        except Exception:
            log.debug("fast_price: evren dosyasi okunamadi", exc_info=True)
        self._pools_ts = time.time()

    def _watched_pools(self) -> list[str]:
        with self._lock:
            extra = sorted(set(self._extra_pools) - set(self._pools))
        return self._pools + extra

    def _poll_once(self, client: httpx.Client) -> None:
        watched = self._watched_pools()
        # pairs endpoint istek basina 30 havuz kabul eder; fazlasi chunk'lanir
        for i in range(0, len(watched), 30):
            pools = ",".join(watched[i:i + 30])
            r = client.get(f"{API['dexscreener']}/latest/dex/pairs/solana/{pools}")
            r.raise_for_status()
            data = r.json()
            items = data.get("pairs") or ([data["pair"]] if data.get("pair") else [])
            now = time.time()
            fresh: dict[str, tuple[float, float]] = {}
            for item in items:
                try:
                    addr = str(item.get("pairAddress") or "")
                    price = float(item.get("priceUsd") or 0)
                except (TypeError, ValueError):
                    continue
                if addr and price > 0:
                    fresh[addr] = (price, now)
            if fresh:
                with self._lock:
                    self._prices.update(fresh)

    def _register_error(self, what: str) -> None:
        self._err_streak += 1
        self._backoff = min(max(self._backoff * 2, 2.0), BACKOFF_MAX_SEC)
        if self._err_streak == 1 or self._err_streak % 10 == 0:
            log.warning(
                "FAST-PRICE hata (%s, streak %d), backoff %.0fs; "
                "motorlar polling fallback'te", what, self._err_streak, self._backoff,
            )

    def _run(self) -> None:
        log.warning("FAST-PRICE feed basladi (kadans %.1fs, bayat esigi %.0fs)",
                    INTERVAL_SEC, STALE_SEC)
        with httpx.Client(timeout=5.0) as client:
            while True:
                t0 = time.time()
                try:
                    if t0 - self._pools_ts > UNIVERSE_RELOAD_SEC:
                        self._load_pools()
                    if self._watched_pools():
                        self._poll_once(client)
                    if self._err_streak:
                        log.warning("FAST-PRICE toparlandi (%d hatadan sonra)",
                                    self._err_streak)
                    self._err_streak = 0
                    self._backoff = 0.0
                except httpx.HTTPStatusError as e:
                    self._register_error(f"http {e.response.status_code}")
                except Exception as e:  # noqa: BLE001 - feed asla olmemeli
                    self._register_error(repr(e))
                delay = max(INTERVAL_SEC - (time.time() - t0), 0.05) + self._backoff
                time.sleep(delay)


_feed: FastPriceFeed | None = None
_feed_lock = threading.Lock()


def get_feed() -> FastPriceFeed | None:
    """Singleton feed (gerekiyorsa baslatir). FAST_PRICE_ENABLED=0 ise None."""
    global _feed
    if not ENABLED:
        return None
    with _feed_lock:
        if _feed is None:
            _feed = FastPriceFeed()
            _feed.start()
    return _feed
