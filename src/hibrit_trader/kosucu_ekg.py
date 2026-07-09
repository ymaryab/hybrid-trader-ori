"""KOSUCU EKG izleyici - pasif gozlemci, islem YOK, motorlara sifir dokunus.

Amac: buyuk kosu yapan coinlerin kosu SIRASINDAKI geri cekilme (nefes)
davranisini kesintisiz kaydetmek; X-serisi cikis kurali bu veriyle tasarlanacak.

Kural: taramada chg_h1 >= +50 gosteren VEYA izlenirken ilk gorulme fiyatinin
+%50 ustune cikan her token "kosucu adayi" olur ve 6 saat boyunca her tick
fiyati data/kosucu_ekg.jsonl'a yazilir (token, ts, fiyat, liq). Ayni anda
izlenen kosucu tavani 20.

API yuku: motorlarla ayni fiyat akisi (scan_all, tick basina bir kez; kadans
SCAN_INTERVAL_SEC). Izlenen kosucu o tick'in tarama listesinde yoksa
fetch_pool_price kullanilir (30sn process-ici cache'li, motorlarla paylasik).
Agresif ek polling yok. Sadece su dosyalara yazar:
  data/kosucu_ekg.jsonl       (tick kayitlari)
  data/kosucu_ekg_state.json  (izleme listesi, restart dayanikli)

Kadans 1/4 interval faz kaydirmali (golge:1/2, v6:3/8, v7:5/8, v4:3/4,
v8:7/8, v9:1/8, ekg:1/4). EKG_ENABLED=0 ile kapatilir.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from hibrit_trader.live_sim import fetch_pool_price
from hibrit_trader.momentum_session import SCAN_INTERVAL_SEC, _data_dir
from hibrit_trader.paper import _now_iso
from hibrit_trader.scanner import scan_all_cached as scan_all

log = logging.getLogger(__name__)

H1_TRIGGER = float(os.getenv("EKG_H1_MIN", "50"))       # taramada h1 esigi
GROWTH_TRIGGER = float(os.getenv("EKG_GROWTH", "1.5"))  # izlenirken ilk fiyatin kati
WATCH_SEC = float(os.getenv("EKG_WATCH_HOURS", "6")) * 3600
MAX_WATCH = int(os.getenv("EKG_MAX_WATCH", "20"))
FIRST_SEEN_TTL = 24 * 3600  # buyume tetikleyicisi icin ilk gorulme hafizasi

OUT_FILE = "kosucu_ekg.jsonl"
STATE_FILE = "kosucu_ekg_state.json"


class KosucuEkg:
    """Pasif kayitci. Islem acmaz, sadece fiyat serisi yazar."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.watch: dict[str, dict] = {}        # pool -> izleme kaydi
        self.first_seen: dict[str, list] = {}   # pool -> [ts, price]
        self._lock_fh = None
        self._load()

    def _path(self, name: str) -> Path:
        return _data_dir() / name

    def _load(self) -> None:
        p = self._path(STATE_FILE)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text())
            self.watch = {
                k: v for k, v in (data.get("watch") or {}).items()
                if isinstance(v, dict) and "started_ts" in v
            }
            self.first_seen = dict(data.get("first_seen") or {})
        except Exception:
            log.critical("ekg state bozuk, temiz baslaniyor")

    def _save(self) -> None:
        p = self._path(STATE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "watch": self.watch,
            "first_seen": self.first_seen,
            "updated_at": _now_iso(),
        }, ensure_ascii=False)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(payload)
        os.replace(tmp, p)

    def _append(self, row: dict) -> None:
        p = self._path(OUT_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": round(time.time(), 3), "ts_iso": _now_iso(), **row}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _acquire_lock(self) -> bool:
        import fcntl

        p = self._path("kosucu_ekg.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("EKG: baska bir instance calisiyor, izleyici baslatilmiyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        log.warning(
            "KOSUCU EKG basladi (pasif, islem yok) - tetik h1>=%.0f veya buyume x%.2f · "
            "pencere %.0f saat · tavan %d es zamanli · cikti %s",
            H1_TRIGGER, GROWTH_TRIGGER, WATCH_SEC / 3600, MAX_WATCH, OUT_FILE,
        )
        self._save()
        time.sleep(SCAN_INTERVAL_SEC * 1 / 4)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("ekg tick hatasi")
            time.sleep(SCAN_INTERVAL_SEC)

    def tick(self) -> None:
        try:
            pairs = scan_all(self.settings.scan_chains)
        except Exception as e:
            log.warning("EKG tick atlandi, tarama hatasi: %r", e)
            return
        now = time.time()
        self._expire(now)
        self._promote(pairs, now)
        self._record(pairs, now)
        self._save()

    def _expire(self, now: float) -> None:
        for pool in [p for p, w in self.watch.items()
                     if now - w["started_ts"] >= WATCH_SEC]:
            w = self.watch.pop(pool)
            log.warning("EKG bitti %s (%.1f saat, tetik %s)",
                        w.get("pair"), WATCH_SEC / 3600, w.get("trigger"))
        self.first_seen = {
            p: fs for p, fs in self.first_seen.items()
            if now - fs[0] < FIRST_SEEN_TTL
        }

    def _promote(self, pairs, now: float) -> None:
        for pr in pairs:
            pool = pr.pool_address
            if not pool or pr.price_usd <= 0:
                continue
            fs = self.first_seen.setdefault(pool, [now, pr.price_usd])
            if pool in self.watch or len(self.watch) >= MAX_WATCH:
                continue
            trigger = None
            if getattr(pr, "chg_h1", 0.0) >= H1_TRIGGER:
                trigger = "h1_50"
            elif fs[1] > 0 and pr.price_usd / fs[1] >= GROWTH_TRIGGER:
                trigger = "buyume_50"
            if trigger:
                self.watch[pool] = {
                    "pair": pr.name,
                    "chain": pr.chain,
                    "token_address": pr.token_address,
                    "started_ts": round(now, 3),
                    "started_at": _now_iso(),
                    "first_price": pr.price_usd,
                    "trigger": trigger,
                    "chg_h1_start": round(getattr(pr, "chg_h1", 0.0), 2),
                }
                log.warning("EKG izlemeye alindi %s (tetik %s, h1 %.1f%%, liq $%.0f)",
                            pr.name, trigger, getattr(pr, "chg_h1", 0.0), pr.liquidity_usd)

    def _record(self, pairs, now: float) -> None:
        if not self.watch:
            return
        in_scan = {pr.pool_address: pr for pr in pairs}
        missing = [p for p in self.watch if p not in in_scan]
        prices_fallback: dict[str, float | None] = {}
        if missing:
            with httpx.Client(timeout=10.0) as client:
                for pool in missing:
                    prices_fallback[pool] = fetch_pool_price(
                        client, self.watch[pool]["chain"], pool
                    )
        for pool, w in self.watch.items():
            pr = in_scan.get(pool)
            if pr is not None:
                price, liq, src = pr.price_usd, pr.liquidity_usd, "scan"
            else:
                price, liq, src = prices_fallback.get(pool), None, "pool_api"
            if not price or price <= 0:
                continue
            self._append({
                "pool_address": pool,
                "token_address": w.get("token_address"),
                "pair": w.get("pair"),
                "chain": w.get("chain"),
                "price_usd": price,
                "liquidity_usd": round(liq, 2) if liq is not None else None,
                "kaynak": src,
                "trigger": w.get("trigger"),
                "izleme_dk": round((now - w["started_ts"]) / 60, 1),
            })
