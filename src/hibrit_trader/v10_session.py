"""V10 senaryo motoru - saf tp_2 denemesi, sanal paralel yarisci.

Diger motorlara (v2/v4/v6/v7/v8/v9/x1/golge) SIFIR dokunus. Sadece su dosyalara yazar:
  data/v10_state.json   (sanal bakiye + acik pozisyonlar)
  data/v10_trades.jsonl (her sanal kapanista kayit)

Kurallar, SADECE bunlar (kullanicinin acik talebi, 2026-07-06):
  GIRIS : liq >= $300k VE 10 <= chg_h1 <= 50. Siralama en yuksek h1 once.
          5 slot, her islemde bakiye/5. Safety taramasi ZORUNLU.
  CIKIS : +%2 gorunce sat (tp_2). BASKA HICBIR cikis kurali yok:
          sabir yok, stop yok, timeout yok. +%2'ye ulasana kadar tutulur.
  Rejim filtresi YOK, cooldown YOK. sol_chg_h1 SADECE kayit (filtre degil).

Fill'ler sanal: gercek fiyat + v2'nin likidite-slippage modeli + gas.
Kadans v2 ile ayni; 7/16 interval faz kaydirmali (v9:1/8, x1:3/16, ekg:1/4,
v6:3/8, golge:1/2, v7:5/8, v4:3/4, v8:7/8). V10_ENABLED=0 ile kapatilir.
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

# ---- V10 esikleri (saf kurallar, baska hicbir sey yok) ------------------------
CHG_H1_MIN = float(os.getenv("V10_CHG_H1_MIN", "10"))
CHG_H1_MAX = float(os.getenv("V10_CHG_H1_MAX", "50"))
LIQ_MIN_USD = float(os.getenv("V10_LIQ_MIN_USD", "300000"))
MAX_SLOTS = 5
START_BALANCE = float(os.getenv("V10_START_BALANCE", "1000"))
TP_PCT = 2.0            # TEK cikis kurali: +%2 gorunce sat

STATE_FILE = "v10_state.json"
TRADES_FILE = "v10_trades.jsonl"


class V10Engine:
    """Sanal senaryo motoru. Kendi dosyalari, diger motorlara sifir dokunus."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.balance: float = START_BALANCE
        self.start_balance: float = START_BALANCE
        self.realized_pnl: float = 0.0
        self.positions: list[dict] = []
        self.created_ts: float = time.time()
        self._aggressive = os.getenv("PAPER_AGGRESSIVE", "0") == "1"
        self._sol_h1_cache: tuple[float, float | None] = (0.0, None)
        self._lock_fh = None
        self._load()

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
                log.critical("v10 state bozuk, yedege tasindi: %s", backup)
            except OSError:
                log.critical("v10 state bozuk ve yedeklenemedi, temiz baslaniyor")

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

        p = self._path("v10_engine.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("V10: baska bir instance calisiyor, motor baslatilmiyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    # ---- Ana dongu (v2 ile ayni kadans, 7/16 interval faz kaydirma) ------------
    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        log.warning(
            "V10 senaryo basladi (saf tp_2) - sanal $%.2f · slot %d · "
            "giris liq>=$%.0f + h1 %.0f..%.0f · cikis SADECE tp+%.0f%% "
            "(stop yok, timeout yok, rejim yok, cooldown yok)",
            self.balance, MAX_SLOTS, LIQ_MIN_USD, CHG_H1_MIN, CHG_H1_MAX, TP_PCT,
        )
        self._save()
        time.sleep(SCAN_INTERVAL_SEC * 7 / 16)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("v10 tick hatasi")
            time.sleep(SCAN_INTERVAL_SEC)

    def tick(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
        self._save()

    # ---- sol_chg_h1: SADECE kayit icin (filtre degil) ---------------------------
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

    # ---- Giris: liq >= $300k + h1 10..50, en yuksek h1 once ----------------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        if kill_is_active():
            return
        try:
            pairs = scan_all(self.settings.scan_chains)
        except Exception:
            log.debug("v10 scan hatasi", exc_info=True)
            return
        held = {p["pool_address"] for p in self.positions}
        held |= {p["token_address"] for p in self.positions if p.get("token_address")}
        cands = []
        for pr in pairs:
            if pr.pool_address in held or pr.token_address in held or pr.price_usd <= 0:
                continue
            if pr.liquidity_usd < LIQ_MIN_USD:
                continue
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
            log.debug("v10 sol_chg_h1 alinamadi, kayit bos", exc_info=True)
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
        log.warning("V10 BUY %s $%.2f @ %.8g (h1 %.1f%%, liq $%.0f)",
                    pair.name, usd, eff_price, pair.chg_h1, pair.liquidity_usd)
        return True

    # ---- Cikis: SADECE tp_2 (baska hicbir kural yok) ------------------------------
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
            if pnl_pct >= TP_PCT:
                self._close_position(pos, price, "tp_2", now)

    def _close_position(self, pos: dict, price: float, reason: str, now: float) -> None:
        cost = pos["cost_usd"]
        slip = _mom_slippage(cost, pos["liq_entry"])
        eff_price = price * (1 - slip)
        gas = GAS_COST_USD.get(pos["chain"], 0.1)
        proceeds = pos["amount_token"] * eff_price - gas
        pnl = proceeds - cost
        hold_sec = round(now - pos["opened_ts"], 1)
        pnl_pct = (eff_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0.0

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
            "friction_pct": round(pos.get("entry_slip_pct", 0.0) + slip * 100, 4),
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
        log.warning("V10 SELL %s pnl $%.2f (%.2f%%) - %s, hold %.0fs (mfe %.1f%% mae %.1f%%)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec,
                    pos["mfe_pct"], pos["mae_pct"])
