"""V5 senaryo motoru — gölgenin veri-dayalı yükseltmesi, beşinci paralel sanal yarışçı.

Gölge/v2/v3/v4 motorlarına SIFIR dokunuş. Sadece şu dosyalara yazar:
  data/v5_state.json   (sanal bakiye + açık pozisyonlar)
  data/v5_trades.jsonl (her sanal kapanışta kayıt)
  data/v5_shadow.jsonl (kapanış sonrası 20dk fiyat izi, v2 deseni)

GÖLGE'DEN AYNEN KORUNAN (kanıt: çalışıyor):
  liq >= $100k, chg_h1 >= 10, en yüksek h1 önce, tp +%2, 30dk sabır,
  5 slot, eşit bölüşüm, safety, rejim (sol_h1 < 0), cooldown 60/15dk.

DÜZELTMELER (gölge verisindeki somut zaaflara karşılık):
  F1 stop_felaket: sabır penceresi sınırsız düşüşe izin veriyordu (KITTY
     -%27). Pozisyon HERHANGİ bir anda -%8'e ulaşırsa sabır iptal, anında
     sat. Kurtarılan 11 işlemin en derini -%8.67'ydi; -%8 tabanı kurtarma
     bölgesini yaşatır, çöküşleri keser.
  F2 tp_2_yarim: +%2'de pozisyonun TAMAMI satılmaz; yarısı satılır (kâr
     kilidi), kalan yarı KOŞUCU olur: tepeden -%3 trail + giriş+%1.5
     breakeven koruması + 60dk tavan. Aynı tokena 5-7 kez tekrar giriş
     friction'ı katlıyordu; koşuyu içeride sürmek daha ucuz.
  F3 shadow izleme: her nihai kapanış 20dk fiyat iziyle ölçülür ki
     "tp sonrası koşu kaçırma" sorusu artık veriyle cevaplansın.

Çıkış sebepleri: tp_2_yarim (yarım satış) / trail / breakeven / stop_felaket
/ stop_gec / timeout_60. Fill'ler sanal: gerçek fiyat + v2'nin likidite-
slippage modeli + gas. Kadans v2 ile aynı; 1/8 interval faz kaydırmalı.
V5_ENABLED=0 ile kapatılır.
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
    SHADOW_MARKS,
    SOL_H1_CACHE_SEC,
    SOL_USDC_POOL,
    _data_dir,
    _mom_slippage,
)
from hibrit_trader.paper import _now_iso, new_trade_id
from hibrit_trader.safety import check_token
from hibrit_trader.scanner import scan_all

log = logging.getLogger(__name__)

# ---- V5 eşikleri (gölge ile aynı zemin + düzeltme sabitleri) -----------------
CHG_H1_MIN = float(os.getenv("V5_CHG_H1_MIN", "10"))
LIQ_MIN_USD = float(os.getenv("V5_LIQ_MIN_USD", "100000"))
MAX_SLOTS = 5
START_BALANCE = float(os.getenv("V5_START_BALANCE", "1000"))
TP_PCT = 2.0             # gölge ile aynı tetik; V5'te yarım satış (F2)
GRACE_SEC = 30 * 60      # ilk 30dk aşağıda -%2 stop yok (sabır, gölge ile aynı)
LATE_STOP_PCT = -2.0     # 30dk sonrası: girişin -%2 altı SAT
DISASTER_PCT = -8.0      # F1: her an geçerli mutlak taban, sabırı iptal eder
TRAIL_PCT = 3.0          # F2 koşucu: tepeden -%3
BE_STOP_PCT = 1.5        # F2 koşucu: giriş+%1.5 breakeven koruması
CEILING_SEC = 60 * 60    # 60dk tavan (koşucu dahil)
SOL_H1_MIN = float(os.getenv("MOM_SOL_H1_MIN", "0"))
COOLDOWN_LOSS_SEC = float(os.getenv("MOM_COOLDOWN_STOP_MIN", "60")) * 60
COOLDOWN_EXIT_SEC = float(os.getenv("MOM_COOLDOWN_EXIT_MIN", "15")) * 60

STATE_FILE = "v5_state.json"
TRADES_FILE = "v5_trades.jsonl"
SHADOW_FILE = "v5_shadow.jsonl"


class V5Engine:
    """Sanal senaryo motoru. Kendi dosyaları, diğer motorlara sıfır dokunuş."""

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
        self._shadow_watch: dict[str, dict] = {}  # F3: in-memory, restart'ta kayıp kabul
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
                log.critical("v5 state bozuk, yedeğe taşındı: %s", backup)
            except OSError:
                log.critical("v5 state bozuk ve yedeklenemedi, temiz başlanıyor")

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

    def _append(self, name: str, row: dict) -> None:
        p = self._path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": round(time.time(), 3), "ts_iso": _now_iso(), **row}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _acquire_lock(self) -> bool:
        import fcntl

        p = self._path("v5_engine.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("V5: başka bir instance çalışıyor, motor başlatılmıyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    # ---- Ana döngü (v2 ile aynı kadans, 1/8 interval faz kaydırma) ------------
    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        log.warning(
            "V5 senaryo başladı — sanal $%.2f · slot %d · giriş liq>=$%.0f + chg_h1>=%.0f "
            "· tp+%.0f%% YARIM + koşucu trail -%.0f/be+%.1f · sabır %dm (taban %%%.0f) "
            "· tavan %dm",
            self.balance, MAX_SLOTS, LIQ_MIN_USD, CHG_H1_MIN,
            TP_PCT, TRAIL_PCT, BE_STOP_PCT, GRACE_SEC // 60, DISASTER_PCT,
            CEILING_SEC // 60,
        )
        self._save()
        time.sleep(SCAN_INTERVAL_SEC / 8)  # v2:0, gölge:1/2, v3:1/4, v4:3/4, v5:1/8
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("v5 tick hatası")
            time.sleep(SCAN_INTERVAL_SEC)

    def tick(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
            try:
                self._poll_shadow(client)  # F3: hata motoru asla kırmasın
            except Exception:
                log.debug("v5 shadow poll hatası", exc_info=True)
        self._save()

    # ---- Rejim (gölge ile aynı: eşik 0, fail-open, kendi cache'i) --------------
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

    # ---- Giriş (gölge ile birebir: liq >= $100k VE chg_h1 >= 10) ---------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        if kill_is_active():
            return
        try:
            pairs = scan_all(self.settings.scan_chains)
        except Exception:
            log.debug("v5 scan hatası", exc_info=True)
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
            if getattr(pr, "chg_h1", 0.0) < CHG_H1_MIN:
                continue
            cands.append(pr)
        cands.sort(key=lambda pr: pr.chg_h1, reverse=True)  # en güçlü trend önce
        if not cands:
            return
        sol_h1 = None
        try:
            sol_h1 = self._sol_chg_h1(client)
        except Exception:
            log.debug("v5 rejim: sol_chg_h1 alınamadı, filtre atlandı", exc_info=True)
        if sol_h1 is not None and sol_h1 < SOL_H1_MIN:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("V5 REJIM: sol_chg_h1 %.2f%% < %.2f%%, giriş yok", sol_h1, SOL_H1_MIN)
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
            time.sleep(0.2 if self._aggressive else 1.5)
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
            "entry_slip_pct": round(slip * 100, 4),
            "runner": False,      # F2: tp sonrası yarım pozisyon bayrağı
            "peak_price": eff_price,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "last_price": eff_price,
        }
        self.balance -= (usd + gas)
        self.positions.append(pos)
        self._save()
        log.warning("V5 BUY %s $%.2f @ %.8g (h1 %.1f%%, liq $%.0f)",
                    pair.name, usd, eff_price, pair.chg_h1, pair.liquidity_usd)
        return True

    # ---- Çıkış: tp yarım + koşucu / stop_felaket / stop_gec / timeout_60 -------
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
            age = now - pos["opened_ts"]

            if pos.get("runner"):
                # F2 koşucu: trail tepeden -%3, breakeven giriş+%1.5, 60dk tavan
                reason = None
                if price <= pos["peak_price"] * (1 - TRAIL_PCT / 100):
                    reason = "trail"
                elif price <= entry * (1 + BE_STOP_PCT / 100):
                    reason = "breakeven"
                elif age >= CEILING_SEC:
                    reason = "timeout_60"
                if reason:
                    self._close_position(pos, price, reason, now)
                continue

            if pnl_pct >= TP_PCT:
                self._take_half_profit(pos, price, now)  # F2: yarısını sat, koşucu bırak
                continue
            reason = None
            if pnl_pct <= DISASTER_PCT:
                reason = "stop_felaket"   # F1: mutlak taban, sabırı iptal eder
            elif age >= GRACE_SEC and pnl_pct <= LATE_STOP_PCT:
                reason = "stop_gec"       # 30dk doldu ve girişin -%2 altı
            elif age >= CEILING_SEC:
                reason = "timeout_60"
            if reason:
                self._close_position(pos, price, reason, now)

    def _take_half_profit(self, pos: dict, price: float, now: float) -> None:
        """F2: +%2 tetiğinde yarım satış; kalan yarı trail'li koşucuya döner."""
        half_cost = pos["cost_usd"] / 2
        half_amount = pos["amount_token"] / 2
        slip = _mom_slippage(half_cost, pos["liq_entry"])
        eff_price = price * (1 - slip)
        gas = GAS_COST_USD.get(pos["chain"], 0.1)
        proceeds = half_amount * eff_price - gas
        pnl = proceeds - half_cost
        pnl_pct = (eff_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0.0

        # Önce kayıt, sonra mutasyon (v2 hardening deseni)
        self._append_trade_row(pos, eff_price, half_cost, proceeds, pnl, pnl_pct,
                               "tp_2_yarim", now)
        self.balance += proceeds
        self.realized_pnl += pnl
        pos["amount_token"] = half_amount
        pos["cost_usd"] = round(half_cost, 4)
        pos["runner"] = True
        pos["peak_price"] = max(price, pos["entry_price"])  # trail tabanı tp anından
        self._save()
        log.warning("V5 TP YARIM %s +$%.2f (%.2f%%), kalan yarı koşucuda (trail -%.0f%%)",
                    pos["pair"], pnl, pnl_pct, TRAIL_PCT)

    def _close_position(self, pos: dict, price: float, reason: str, now: float) -> None:
        cost = pos["cost_usd"]
        slip = _mom_slippage(cost, pos["liq_entry"])
        eff_price = price * (1 - slip)
        gas = GAS_COST_USD.get(pos["chain"], 0.1)
        proceeds = pos["amount_token"] * eff_price - gas
        pnl = proceeds - cost
        pnl_pct = (eff_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0.0

        # Önce kayıt, sonra mutasyon, anında save
        self._append_trade_row(pos, eff_price, cost, proceeds, pnl, pnl_pct, reason, now)
        self.balance += proceeds
        self.realized_pnl += pnl
        cd = COOLDOWN_LOSS_SEC if reason in ("stop_gec", "stop_felaket") else COOLDOWN_EXIT_SEC
        if pos.get("token_address"):
            self._cooldown_until[pos["token_address"]] = now + cd
        try:
            self.positions.remove(pos)
        except ValueError:
            pass
        self._save()
        log.warning("V5 SELL %s pnl $%.2f (%.2f%%) — %s%s, hold %.0fs (mfe %.1f%% mae %.1f%%)",
                    pos["pair"], pnl, pnl_pct, reason,
                    " (koşucu)" if pos.get("runner") else "",
                    now - pos["opened_ts"], pos["mfe_pct"], pos["mae_pct"])
        # F3: nihai kapanışı 20dk fiyat izine al (hata kırmaz)
        try:
            self._register_shadow(pos, price, eff_price, reason, now)
        except Exception:
            log.debug("v5 shadow kayıt hatası", exc_info=True)

    def _append_trade_row(self, pos: dict, eff_price: float, cost: float,
                          proceeds: float, pnl: float, pnl_pct: float,
                          reason: str, now: float) -> None:
        self._append(TRADES_FILE, {
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
            "runner": bool(pos.get("runner")),
            "hold_sec": round(now - pos["opened_ts"], 1),
            "exit_reason": reason,
            "opened_at": pos["opened_at"],
            "closed_at": _now_iso(),
        })

    # ---- F3: shadow tracker (v2 deseni, kapanış sonrası 20dk fiyat izi) --------
    def _register_shadow(self, pos: dict, raw_price: float, eff_price: float,
                         reason: str, now: float) -> None:
        self._shadow_watch[pos["trade_id"]] = {
            "trade_id": pos["trade_id"],
            "pair": pos["pair"],
            "chain": pos["chain"],
            "pool_address": pos["pool_address"],
            "entry_price": pos["entry_price"],
            "exit_price_raw": raw_price,
            "exit_price_eff": eff_price,
            "exit_reason": reason,
            "exit_ts": now,
            "samples": {},
            "wmax": raw_price,
            "wmin": raw_price,
        }

    def _poll_shadow(self, client: httpx.Client) -> None:
        if not self._shadow_watch:
            return
        now = time.time()
        for tid in list(self._shadow_watch.keys()):
            w = self._shadow_watch[tid]
            elapsed = now - w["exit_ts"]
            price = fetch_pool_price(client, w["chain"], w["pool_address"])
            if price is not None and price > 0:
                if price > w["wmax"]:
                    w["wmax"] = price
                if price < w["wmin"]:
                    w["wmin"] = price
                for m in SHADOW_MARKS:
                    if elapsed >= m and m not in w["samples"]:
                        w["samples"][m] = price
            if elapsed >= SHADOW_MARKS[-1]:
                s = w["samples"]
                base = w["exit_price_raw"]
                self._append(SHADOW_FILE, {
                    "trade_id": w["trade_id"],
                    "pair": w["pair"],
                    "chain": w["chain"],
                    "pool_address": w["pool_address"],
                    "entry_price": w["entry_price"],
                    "exit_price_raw": base,
                    "exit_price_eff": w["exit_price_eff"],
                    "exit_reason": w["exit_reason"],
                    "exit_ts_ms": int(w["exit_ts"] * 1000),
                    "t60": s.get(60),
                    "t300": s.get(300),
                    "t600": s.get(600),
                    "t900": s.get(900),
                    "t1200": s.get(1200),
                    "window_max": w["wmax"],
                    "window_min": w["wmin"],
                    "max_vs_exit_pct": round((w["wmax"] / base - 1) * 100, 3) if base > 0 else None,
                    "min_vs_exit_pct": round((w["wmin"] / base - 1) * 100, 3) if base > 0 else None,
                    "samples_taken": len(s),
                })
                del self._shadow_watch[tid]
