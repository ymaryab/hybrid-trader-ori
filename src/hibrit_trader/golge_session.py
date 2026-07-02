"""GÖLGE senaryo testi — v2 ile YAN YANA koşan tamamen sanal alternatif strateji.

v2 motoruna, kurallarına, state'ine SIFIR dokunuş. Sadece şu dosyalara yazar:
  data/golge_state.json   (sanal bakiye + açık pozisyonlar)
  data/golge_trades.jsonl (her sanal kapanışta kayıt)

v2'den İKİ farkı var, gerisi birebir aynı:
  FARK 1 GİRİŞ : m5>0 ve h1 5..50 yerine TEK şart chg_h1 >= 10.
                 Sıralama en yüksek chg_h1'den. Korunanlar: liq >= $40k,
                 safety, cooldown (kayıp çıkışı 60dk / diğer 15dk), rejim
                 filtresi (SOL h1 < 0 giriş yok), 5 slot, eşit bölüşüm, $1000.
  FARK 2 ÇIKIŞ : -%2 stop YOK. Girişin altına düşen pozisyon 20 dakika izlenir;
                 giriş üstüne dönerse sayaç sıfırlanır, 20dk boyunca altta
                 kalırsa satılır (sabir_20). Breakeven (+%3 kilidi), trail
                 (+%5 sonrası tepeden -%3) ve timeout_60 v2 ile birebir aynı.

Fill'ler sanal: gerçek fiyat + v2'nin likidite-slippage modeli + gas.
Kadans: v2 ile AYNI (SCAN_INTERVAL_SEC, varsayılan 30sn); adil kıyas.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from hibrit_trader.config import GAS_COST_USD
from hibrit_trader.killswitch import is_active as kill_is_active
from hibrit_trader.live_sim import fetch_pool_price
from hibrit_trader.momentum_session import (
    SCAN_INTERVAL_SEC,
    SOL_H1_CACHE_SEC,
    SOL_USDC_POOL,
    _data_dir,
    _mom_slippage,
)
from hibrit_trader.paper import _now_iso, new_trade_id
from hibrit_trader.safety import check_token
from hibrit_trader.scanner import scan_all

log = logging.getLogger(__name__)

# ---- Gölge eşikleri (v2 ile paylaşılanlar momentum_session'dan import edilir) ----
CHG_H1_MIN = float(os.getenv("GOLGE_CHG_H1_MIN", "10"))   # TEK giriş şartı (liq hariç)
LIQ_MIN_USD = float(os.getenv("MOM_LIQ_MIN_USD", "40000"))  # v2 ile aynı taban
MAX_SLOTS = 5
START_BALANCE = float(os.getenv("GOLGE_START_BALANCE", "1000"))
BE_ARM_PCT = 3.0        # v2 ile birebir
BE_STOP_PCT = 0.75      # v2 ile birebir
TRAIL_ARM_PCT = 5.0     # v2 ile birebir
TRAIL_PCT = 3.0         # v2 ile birebir
CEILING_SEC = 60 * 60   # v2 ile birebir
PATIENCE_SEC = 20 * 60  # FARK 2: girişin altında kesintisiz 20dk -> sat
SOL_H1_MIN = float(os.getenv("MOM_SOL_H1_MIN", "0"))      # rejim eşiği v2 ile aynı
COOLDOWN_LOSS_SEC = float(os.getenv("MOM_COOLDOWN_STOP_MIN", "60")) * 60
COOLDOWN_EXIT_SEC = float(os.getenv("MOM_COOLDOWN_EXIT_MIN", "15")) * 60

STATE_FILE = "golge_state.json"
TRADES_FILE = "golge_trades.jsonl"


class GolgeEngine:
    """Sanal senaryo motoru. Kendi dosyaları, v2'ye sıfır dokunuş."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.balance: float = START_BALANCE
        self.start_balance: float = START_BALANCE
        self.realized_pnl: float = 0.0
        self.positions: list[dict] = []
        self.created_ts: float = time.time()
        self._aggressive = os.getenv("PAPER_AGGRESSIVE", "0") == "1"
        self._cooldown_until: dict[str, float] = {}
        self._sol_h1_cache: tuple[float, float | None] = (0.0, None)
        self._regime_logged = False
        self._lock_fh = None
        self._load()

    # ---- Dosya işleri (v2 hardening desenleri: atomik save, anında persist) ----
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
                log.critical("golge state bozuk, yedeğe taşındı: %s", backup)
            except OSError:
                log.critical("golge state bozuk ve yedeklenemedi, temiz başlanıyor")

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

        p = self._path("golge_engine.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("GOLGE: başka bir instance çalışıyor, motor başlatılmıyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    # ---- Ana döngü (v2 ile aynı kadans) --------------------------------------
    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        log.warning(
            "GOLGE senaryo başladı — sanal $%.2f · slot %d · TEK giriş şartı chg_h1>=%.0f "
            "(liq>=$%.0f) · stop YOK, sabır %dm · be/trail/timeout v2 ile aynı",
            self.balance, MAX_SLOTS, CHG_H1_MIN, LIQ_MIN_USD, PATIENCE_SEC // 60,
        )
        self._save()
        time.sleep(SCAN_INTERVAL_SEC / 2)  # v2 ile faz kaydır, API yükünü yay
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("golge tick hatası")
            time.sleep(SCAN_INTERVAL_SEC)

    def tick(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
        self._save()

    # ---- Rejim (v2 ile aynı mantık, kendi cache'i) ----------------------------
    def _sol_chg_h1(self, client: httpx.Client) -> float | None:
        from hibrit_trader.config import API

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

    # ---- Giriş (FARK 1: tek şart chg_h1 >= 10) --------------------------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        if kill_is_active():  # v2 ile aynı acil fren
            return
        try:
            pairs = scan_all(self.settings.scan_chains)
        except Exception:
            log.debug("golge scan hatası", exc_info=True)
            return
        held = {p["pool_address"] for p in self.positions}
        held |= {p["token_address"] for p in self.positions if p.get("token_address")}
        now = time.time()
        self._cooldown_until = {
            t: ts for t, ts in self._cooldown_until.items() if ts > now
        }
        cands = []
        for pr in pairs:
            if pr.pool_address in held or pr.token_address in held or pr.price_usd <= 0:
                continue
            if self._cooldown_until.get(pr.token_address, 0.0) > now:
                continue
            if pr.liquidity_usd < LIQ_MIN_USD:
                continue
            if getattr(pr, "chg_h1", 0.0) < CHG_H1_MIN:  # TEK şart
                continue
            cands.append(pr)
        cands.sort(key=lambda pr: pr.chg_h1, reverse=True)  # en güçlü trend önce
        if not cands:
            return
        # Rejim filtresi: v2 ile aynı (fail-open)
        sol_h1 = None
        try:
            sol_h1 = self._sol_chg_h1(client)
        except Exception:
            log.debug("golge rejim: sol_chg_h1 alınamadı, filtre atlandı", exc_info=True)
        if sol_h1 is not None and sol_h1 < SOL_H1_MIN:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("GOLGE REJIM: sol_chg_h1 %.2f%% < %.2f%%, giriş yok", sol_h1, SOL_H1_MIN)
            return
        if sol_h1 is not None and self._regime_logged:
            self._regime_logged = False
        budget_each = self.balance / empty
        for pair in cands:
            if empty <= 0 or budget_each < 1.0:
                break
            try:
                report = check_token(client, pair.chain, pair.token_address)
            except Exception:
                continue
            time.sleep(0.2 if self._aggressive else 1.5)  # v2 ile aynı rate limit
            if not report.ok:
                continue
            if self._open_position(pair, budget_each):
                empty -= 1
                held.add(pair.pool_address)
                held.add(pair.token_address)

    def _open_position(self, pair, usd: float) -> bool:
        gas = GAS_COST_USD.get(pair.chain, 0.1)
        if self.balance < usd + gas:
            return False
        slip = _mom_slippage(usd, pair.liquidity_usd)  # v2 ile aynı model
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
            "entry_slip_pct": round(slip * 100, 4),
            "peak_price": eff_price,
            "be_armed": False,
            "trail_armed": False,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "last_price": eff_price,
            "dip_since": None,      # FARK 2: girişin altına düştüğü an (sabır sayacı)
        }
        self.balance -= (usd + gas)
        self.positions.append(pos)
        self._save()
        log.warning("GOLGE BUY %s $%.2f @ %.8g (h1 %.1f%%, liq $%.0f)",
                    pair.name, usd, eff_price, pair.chg_h1, pair.liquidity_usd)
        return True

    # ---- Çıkış (FARK 2: stop yok, 20dk sabır; be/trail/timeout v2 ile aynı) ----
    def _manage_exits(self, client: httpx.Client) -> None:
        now = time.time()
        for pos in list(self.positions):
            price = fetch_pool_price(client, pos["chain"], pos["pool_address"])
            if price is None or price <= 0:
                price = pos["last_price"]
            pos["last_price"] = price
            entry = pos["entry_price"]
            pnl_pct = (price / entry - 1) * 100 if entry > 0 else 0.0
            if pnl_pct > pos["mfe_pct"]:
                pos["mfe_pct"] = round(pnl_pct, 4)
            if pnl_pct < pos["mae_pct"]:
                pos["mae_pct"] = round(pnl_pct, 4)
            if price > pos["peak_price"]:
                pos["peak_price"] = price
            if pnl_pct >= BE_ARM_PCT:
                pos["be_armed"] = True
            if pnl_pct >= TRAIL_ARM_PCT:
                pos["trail_armed"] = True
            # Sabır sayacı: girişin altındaysa işlet, üstüne dönünce sıfırla
            if price < entry:
                if pos.get("dip_since") is None:
                    pos["dip_since"] = now
            else:
                pos["dip_since"] = None
            age = now - pos["opened_ts"]

            reason = None
            if pos["trail_armed"] and price <= pos["peak_price"] * (1 - TRAIL_PCT / 100):
                reason = "trail"
            elif pos["be_armed"] and price <= entry * (1 + BE_STOP_PCT / 100):
                reason = "breakeven"
            elif pos.get("dip_since") and now - pos["dip_since"] >= PATIENCE_SEC and price < entry:
                reason = "sabir_20"   # 20dk kesintisiz girişin altında: sat
            elif age >= CEILING_SEC:
                reason = "timeout_60"
            if reason:
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

        # v2 hardening deseni: önce kayıt, sonra mutasyon, anında save
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
            "cost_usd": round(cost, 4),
            "proceeds_usd": round(proceeds, 4),
            "pnl_usd": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 3),
            "mfe_pct": pos["mfe_pct"],
            "mae_pct": pos["mae_pct"],
            "hold_sec": hold_sec,
            "exit_reason": reason,
            "friction_pct": round(pos.get("entry_slip_pct", 0.0) + slip * 100, 4),
            "opened_at": pos["opened_at"],
            "closed_at": _now_iso(),
        })
        self.balance += proceeds
        self.realized_pnl += pnl
        cd = COOLDOWN_LOSS_SEC if reason == "sabir_20" else COOLDOWN_EXIT_SEC
        if pos.get("token_address"):
            self._cooldown_until[pos["token_address"]] = now + cd
        try:
            self.positions.remove(pos)
        except ValueError:
            pass
        self._save()
        log.warning("GOLGE SELL %s pnl $%.2f (%.2f%%) — %s, hold %.0fs (mfe %.1f%% mae %.1f%%)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec, pos["mfe_pct"], pos["mae_pct"])
