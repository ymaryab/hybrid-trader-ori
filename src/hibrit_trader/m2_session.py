"""M2 senaryo motoru: v10 iskeletinin MAJOR-TOKEN uyarlamasi (saf tp).

Diger motorlara SIFIR dokunus. Sadece su dosyalara yazar:
  data/m2_state.json     (sanal bakiye + acik pozisyonlar)
  data/m2_trades.jsonl   (her sanal kapanista kayit)
Evren dosyasi M1 ile ORTAK (data/m1_universe.json): ayni sabit major evren,
ayni gunluk tazeleme. Yazim atomik oldugu icin paylasim guvenli; M2 normalde
sadece okur, M1 kapaliysa 30dk pay ile tazelemeyi kendi devralir.

AV SAHASI: M1 ile ayni. Solana'nin en likit majorlari (liq >= $3M).

KURALLAR (v10 iskeleti, major olcekli, v10 talimatindaki sadelik AYNEN korunur):
  GIRIS : evrenden chg_h1 1.5..15. Siralama en yuksek h1 once.
          5 slot, her islemde butcenin 1/5'i.
  CIKIS : +%1.2 gorunce sat (tp_1_2). BASKA HICBIR cikis kurali yok:
          stop yok, sabir yok, timeout yok. tp'ye ulasana kadar tutulur.
  Rejim filtresi YOK, cooldown YOK. sol_chg_h1 SADECE kayit (filtre degil).
  SAFETY: hafif; evren kurulurken honeypot kontrolu yeter, giris aninda tarama yok.

Fill modeli v2 ile ayni (_mom_slippage + gas); major derinlikte slip ~%0.007.
Kadans SCAN_INTERVAL_SEC, 9/16 faz kaydirmali. M2_ENABLED=0 ile kapatilir.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from hibrit_trader.broker import golge_olcum
from hibrit_trader.config import API, GAS_COST_USD
from hibrit_trader.dexscreener_scan import pair_from_dexscreener
from hibrit_trader.fast_price import get_feed
from hibrit_trader.killswitch import is_active as kill_is_active
from hibrit_trader.live_sim import fetch_pool_price
from hibrit_trader.m1_session import (
    SEED_TOKENS,
    UNIVERSE_FILE,
    UNIVERSE_LIQ_MIN,
    UNIVERSE_REFRESH_SEC,
    _best_sane_pool,
    _light_honeypot_ok,
)
from hibrit_trader.momentum_session import (
    SCAN_INTERVAL_SEC,
    SOL_H1_CACHE_SEC,
    SOL_USDC_POOL,
    _data_dir,
    _mom_slippage,
)
from hibrit_trader.paper import _now_iso, new_trade_id
from hibrit_trader.price_sanity import guard_price

log = logging.getLogger(__name__)

# ---- M2 esikleri (saf kurallar, baska hicbir sey yok) --------------------------
CHG_H1_MIN = float(os.getenv("M2_CHG_H1_MIN", "1.5"))
CHG_H1_MAX = float(os.getenv("M2_CHG_H1_MAX", "15"))
MAX_SLOTS = 5
START_BALANCE = float(os.getenv("M2_START_BALANCE", "1000"))
TP_PCT = 1.2            # TEK cikis kurali: +%1.2 gorunce sat
# M1 kapaliysa evren tazelemeyi M2 devralir; 30dk pay M1'e oncelik verir
UNIVERSE_TAKEOVER_SEC = UNIVERSE_REFRESH_SEC + 1800
# Hizli cikis kadansi: 30s tam tick arasinda fast feed'ten tp kontrolu
EXIT_INTERVAL_SEC = float(os.getenv("M_EXIT_INTERVAL_SEC", "2"))

STATE_FILE = "m2_state.json"
TRADES_FILE = "m2_trades.jsonl"


class M2Engine:
    """Sanal major-token motoru (saf tp). Kendi dosyalari, digerlerine sifir dokunus."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.balance: float = START_BALANCE
        self.start_balance: float = START_BALANCE
        self.realized_pnl: float = 0.0
        self.positions: list[dict] = []
        self.created_ts: float = time.time()
        self._sol_h1_cache: tuple[float, float | None] = (0.0, None)
        self._universe: list[dict] = []
        self._universe_ts: float = 0.0
        self._lock_fh = None
        self._load()
        self._load_universe()

    # ---- Dosya isleri (v2 hardening desenleri: atomik save, aninda persist) ----
    def _path(self, name: str) -> Path:
        return _data_dir() / name

    def _load(self) -> None:
        p = self._path(STATE_FILE)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text())
            self.balance = float(data.get("balance", START_BALANCE))
            self.start_balance = float(data.get("start_balance", START_BALANCE))
            self.realized_pnl = float(data.get("realized_pnl", 0.0))
            self.created_ts = float(data.get("created_ts", time.time()))
            self.positions = [
                pos for pos in (data.get("positions") or [])
                if isinstance(pos, dict) and "entry_price" in pos and "pool_address" in pos
            ]
        except Exception:
            backup = p.with_name(f"{p.name}.corrupt-{int(time.time())}")
            try:
                p.rename(backup)
                log.critical("m2 state bozuk, yedege tasindi: %s", backup)
            except OSError:
                log.critical("m2 state bozuk ve yedeklenemedi, temiz baslaniyor")

    def _save(self) -> None:
        p = self._path(STATE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "balance": round(self.balance, 4),
            "start_balance": round(self.start_balance, 2),
            "realized_pnl": round(self.realized_pnl, 4),
            "created_ts": round(self.created_ts, 3),
            "positions": self.positions,
            "updated_at": _now_iso(),
        }, ensure_ascii=False, indent=2)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(payload)
        os.replace(tmp, p)

    def _append_trade(self, row: dict) -> None:
        p = self._path(TRADES_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": round(time.time(), 3), "ts_iso": _now_iso(), **row}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _acquire_lock(self) -> bool:
        import fcntl

        p = self._path("m2_engine.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("M2: baska bir instance calisiyor, motor baslatilmiyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    # ---- Evren: M1 ile ortak dosya, M2 normalde okur ----------------------------
    def _load_universe(self) -> None:
        p = self._path(UNIVERSE_FILE)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text())
            self._universe = list(data.get("tokens") or [])
            self._universe_ts = float(data.get("updated_ts") or 0.0)
        except Exception:
            log.warning("m2 universe dosyasi okunamadi, tazelenecek")

    def _refresh_universe(self, client: httpx.Client) -> None:
        """M1 kapaliysa devralinan tazeleme: SEED adaylari, en likit havuz >= $3M."""
        tokens: list[dict] = []
        for sym, addr in SEED_TOKENS.items():
            try:
                r = client.get(f"{API['dexscreener']}/latest/dex/tokens/{addr}")
                r.raise_for_status()
                pairs = [
                    p for p in (r.json().get("pairs") or [])
                    if p.get("chainId") == "solana"
                    and (p.get("baseToken") or {}).get("address") == addr
                ]
                if not pairs:
                    continue
                best = _best_sane_pool(pairs)
                if best is None:
                    log.warning("M2 EVREN: %s icin fiyati tutarli havuz yok (veri arizasi), disarida", sym)
                    continue
                liq = float((best.get("liquidity") or {}).get("usd") or 0)
                if liq < UNIVERSE_LIQ_MIN:
                    continue
                if not _light_honeypot_ok(client, addr):
                    log.warning("M2 EVREN: %s hafif honeypot kontrolunden gecemedi, disarida", sym)
                    continue
                tokens.append({
                    "symbol": sym,
                    "token_address": addr,
                    "pool_address": str(best.get("pairAddress") or ""),
                    "liq_usd": round(liq, 0),
                })
            except Exception:
                log.debug("m2 universe: %s dogrulanamadi", sym, exc_info=True)
            time.sleep(0.4)
        if not tokens:
            log.warning("M2 EVREN: tazeleme bos dondu, eski evren korunuyor (n=%d)", len(self._universe))
            self._universe_ts = time.time()  # bos donuste de bekle, API'yi dovme
            return
        self._universe = tokens
        self._universe_ts = time.time()
        p = self._path(UNIVERSE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "updated_ts": round(self._universe_ts, 3),
            "updated_at": _now_iso(),
            "liq_min_usd": UNIVERSE_LIQ_MIN,
            "tokens": tokens,
        }, ensure_ascii=False, indent=2)
        tmp = p.with_name(p.name + ".m2tmp")
        tmp.write_text(payload)
        os.replace(tmp, p)
        log.warning("M2 EVREN tazelendi: %d token (%s)", len(tokens),
                    ", ".join(t["symbol"] for t in tokens))

    def _scan_universe(self, client: httpx.Client) -> list:
        """Evren havuzlarini TEK istekle cek (pairs endpoint, 30 havuz siniri)."""
        if time.time() - self._universe_ts > UNIVERSE_REFRESH_SEC:
            self._load_universe()  # M1 tazelemis olabilir, once dosyadan oku
        if time.time() - self._universe_ts > UNIVERSE_TAKEOVER_SEC:
            self._refresh_universe(client)
        if not self._universe:
            return []
        pools = ",".join(t["pool_address"] for t in self._universe[:30] if t.get("pool_address"))
        if not pools:
            return []
        r = client.get(f"{API['dexscreener']}/latest/dex/pairs/solana/{pools}")
        r.raise_for_status()
        data = r.json()
        items = data.get("pairs") or ([data["pair"]] if data.get("pair") else [])
        out = []
        for item in items:
            pr = pair_from_dexscreener(item)
            if pr is not None:
                out.append(pr)
        return out

    # ---- Ana dongu (9/16 interval faz kaydirma) ---------------------------------
    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        log.warning(
            "M2 senaryo basladi (major evren, saf tp) - sanal $%.2f · slot %d · "
            "evren liq>=$%.0f · giris h1 %.1f..%.1f (h1 sirali) · cikis SADECE tp+%.1f%% "
            "(stop yok, timeout yok, rejim yok, cooldown yok)",
            self.balance, MAX_SLOTS, UNIVERSE_LIQ_MIN, CHG_H1_MIN, CHG_H1_MAX, TP_PCT,
        )
        self._save()
        time.sleep(SCAN_INTERVAL_SEC * 9 / 16)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("m2 tick hatasi")
            deadline = time.time() + SCAN_INTERVAL_SEC
            while True:
                kalan = deadline - time.time()
                if kalan <= 0:
                    break
                time.sleep(min(EXIT_INTERVAL_SEC, kalan))
                if time.time() >= deadline:
                    break
                try:
                    self.fast_exit_tick()
                except Exception:
                    log.exception("m2 hizli cikis tick hatasi")

    def tick(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
        self._save()

    # ---- sol_chg_h1: SADECE kayit icin (filtre degil) ---------------------------
    def _sol_chg_h1(self, client: httpx.Client) -> float | None:
        ts, cached = self._sol_h1_cache
        if time.time() - ts < SOL_H1_CACHE_SEC:
            return cached
        url = f"{API['geckoterminal']}/networks/solana/pools/{SOL_USDC_POOL}"
        resp = client.get(url, headers={"accept": "application/json"}, timeout=10)
        resp.raise_for_status()
        chg = (resp.json()["data"]["attributes"].get("price_change_percentage") or {}).get("h1")
        val = round(float(chg), 3) if chg is not None else None
        self._sol_h1_cache = (time.time(), val)
        return val

    # ---- Giris: evrenden h1 1.5..15, en yuksek h1 once ---------------------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        if kill_is_active():
            return
        try:
            pairs = self._scan_universe(client)
        except Exception:
            log.debug("m2 scan hatasi", exc_info=True)
            return
        held = {p["pool_address"] for p in self.positions}
        held |= {p["token_address"] for p in self.positions if p.get("token_address")}
        cands = []
        for pr in pairs:
            if pr.pool_address in held or pr.token_address in held or pr.price_usd <= 0:
                continue
            if pr.liquidity_usd < UNIVERSE_LIQ_MIN:
                continue  # havuz gun ici boslamis olabilir, slip garantisi icin taze kontrol
            if not (CHG_H1_MIN <= getattr(pr, "chg_h1", 0.0) <= CHG_H1_MAX):
                continue
            cands.append(pr)
        cands.sort(key=lambda pr: pr.chg_h1, reverse=True)  # en yuksek h1 once
        if not cands:
            return
        sol_h1 = None
        try:
            sol_h1 = self._sol_chg_h1(client)  # sadece kayit, filtre degil
        except Exception:
            log.debug("m2 sol_chg_h1 alinamadi, kayit bos", exc_info=True)
        budget_each = self.balance / empty
        for pair in cands:
            if empty <= 0 or budget_each < 1.0:
                break
            # safety: evren kurulurken kontrol edildi, giris aninda ekstra tarama yok
            if self._open_position(pair, budget_each, sol_h1):
                empty -= 1
                held.add(pair.pool_address)
                held.add(pair.token_address)

    def _open_position(self, pair, usd: float, sol_h1: float | None = None) -> bool:
        gas = GAS_COST_USD.get(pair.chain, 0.1)
        if self.balance < usd + gas:
            return False
        slip = _mom_slippage(usd, pair.liquidity_usd)
        eff_price = pair.price_usd * (1 + slip)
        now = time.time()
        pos = {
            "trade_id": new_trade_id(pair.pool_address, now),
            "pair": pair.name,
            "chain": pair.chain,
            "token_address": pair.token_address,
            "pool_address": pair.pool_address,
            "entry_price": eff_price,
            "amount_token": usd / eff_price,
            "cost_usd": round(usd, 4),
            "opened_ts": now,
            "opened_at": _now_iso(),
            "chg_m5": round(getattr(pair, "chg_m5", 0.0), 2),
            "chg_h1": round(pair.chg_h1, 2),
            "liq_entry": round(pair.liquidity_usd, 2),
            "sol_chg_h1": sol_h1,   # sadece kayit
            "entry_slip_pct": round(slip * 100, 4),
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "last_price": eff_price,
        }
        self.balance -= (usd + gas)
        self.positions.append(pos)
        self._save()
        log.warning("M2 BUY %s $%.2f @ %.8g (h1 %.2f%%, m5 %.2f%%, liq $%.0f, slip %%%.4f)",
                    pair.name, usd, eff_price, pair.chg_h1,
                    getattr(pair, "chg_m5", 0.0), pair.liquidity_usd, slip * 100)
        golge_olcum("M2", "al", pair.token_address, eff_price, usd=usd)
        return True

    # ---- Cikis: SADECE tp_1_2 (baska hicbir kural yok) ----------------------------
    def _eval_position(self, pos: dict, price: float, now: float) -> str | None:
        """Fiyati isle (last_price/mfe/mae) ve cikis nedeni dondur (yoksa None)."""
        price, ariza = guard_price(pos, price, now, "M2")
        if ariza:
            return None  # veri arizasi: islem tetikleme, degerleme son gecerli fiyatta
        pos["last_price"] = price
        entry = pos["entry_price"]
        pnl_pct = (price / entry - 1) * 100 if entry > 0 else 0.0
        if pnl_pct > pos["mfe_pct"]:
            pos["mfe_pct"] = round(pnl_pct, 4)
        if pnl_pct < pos["mae_pct"]:
            pos["mae_pct"] = round(pnl_pct, 4)
        return "tp_1_2" if pnl_pct >= TP_PCT else None

    def _manage_exits(self, client: httpx.Client) -> None:
        now = time.time()
        feed = get_feed()
        for pos in list(self.positions):
            rec = feed.get_price(pos["pool_address"]) if feed is not None else None
            if rec is not None:
                price, sample_ts = rec
                src = "fast"
            else:
                price = fetch_pool_price(client, pos["chain"], pos["pool_address"])
                sample_ts, src = None, "poll"
            if price is None or price <= 0:
                price = pos["last_price"]
                sample_ts, src = None, "poll"
            reason = self._eval_position(pos, price, now)
            if reason:
                pos["_price_src"] = src
                pos["_price_ts"] = sample_ts
                self._close_position(pos, price, reason, now)

    def fast_exit_tick(self) -> None:
        """1-2s kadansli tp kontrolu. SADECE fast feed'te taze fiyati olan
        pozisyonlara bakar; taze fiyat yoksa dokunmaz, 30s tick kapsar
        (motor hicbir kosulda kor kalmaz). Kapanis olmadikca disk yazilmaz."""
        if not self.positions:
            return
        feed = get_feed()
        if feed is None:
            return
        now = time.time()
        for pos in list(self.positions):
            rec = feed.get_price(pos["pool_address"])
            if rec is None:
                continue
            price, sample_ts = rec
            reason = self._eval_position(pos, price, now)
            if reason:
                pos["_price_src"] = "fast"
                pos["_price_ts"] = sample_ts
                self._close_position(pos, price, reason, now)

    def _close_position(self, pos: dict, price: float, reason: str, now: float) -> None:
        cost = pos["cost_usd"]
        slip = _mom_slippage(cost, pos["liq_entry"])
        eff_price = price * (1 - slip)
        gas = GAS_COST_USD.get(pos["chain"], 0.1)
        proceeds = pos["amount_token"] * eff_price - gas
        pnl = proceeds - cost
        hold_sec = round(now - pos["opened_ts"], 1)
        pnl_pct = (eff_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0.0
        friction_pct = round(pos.get("entry_slip_pct", 0.0) + slip * 100, 4)
        # hiz kenari olcumu: fast yolunda tetik gecikmesi = simdi - feed ornek zamani
        price_src = pos.pop("_price_src", "poll")
        price_ts = pos.pop("_price_ts", None)
        tetik_gecikme = round(now - price_ts, 3) if price_ts else None

        # v2 hardening deseni: once kayit, sonra mutasyon, aninda save
        self._append_trade({
            "trade_id": pos["trade_id"],
            "pair": pos["pair"],
            "chain": pos["chain"],
            "token_address": pos["token_address"],
            "pool_address": pos["pool_address"],
            "entry_price": pos["entry_price"],
            "exit_price": eff_price,
            "chg_m5": pos["chg_m5"],
            "chg_h1": pos["chg_h1"],
            "liq_entry": pos["liq_entry"],
            "sol_chg_h1": pos.get("sol_chg_h1"),
            "cost_usd": round(cost, 4),
            "proceeds_usd": round(proceeds, 4),
            "pnl_usd": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 3),
            "mfe_pct": pos["mfe_pct"],
            "mae_pct": pos["mae_pct"],
            "hold_sec": hold_sec,
            "exit_reason": reason,
            "friction_pct": friction_pct,
            "price_source": price_src,
            "tetik_gecikme_sec": tetik_gecikme,
            "opened_at": pos["opened_at"],
            "closed_at": _now_iso(),
        })
        self.balance += proceeds
        self.realized_pnl += pnl
        try:
            self.positions.remove(pos)
        except ValueError:
            pass
        self._save()
        log.warning("M2 SELL %s pnl $%.2f (%.2f%%) - %s, hold %.0fs "
                    "(mfe %.1f%% mae %.1f%% friction %%%.4f)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec,
                    pos["mfe_pct"], pos["mae_pct"], friction_pct)
        golge_olcum("M2", "sat", pos["token_address"], eff_price,
                    amount_token=pos["amount_token"])
