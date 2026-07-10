"""X1 senaryo motoru - X-serisi kosucu avcisi, sanal paralel yarisci.

Diger motorlara (v2/v4/v6/v7/v8/v9/golge) SIFIR dokunus. Sadece su dosyalara yazar:
  data/x1_state.json   (sanal bakiye + acik pozisyonlar)
  data/x1_trades.jsonl (her sanal kapanista kayit, tepe/nefes gecmisi dahil)

Felsefe: buyuk kosucuya bin, nefeste oturma, olum donusunde in.
EKG kalibrasyonu (2026-07-05): nefes medyan -%10, olum medyan -%61,
20x+ devler sig nefes alir (-%4.8); -%20 esigi nefeslerin %24.6'sini
oldurup olumlerin %88.6'sini yakaliyordu; kesintili gozlem nefesleri
sig gosterdigi icin 2 puan pay ile trail -%18.

GIRIS : chg_h1 >= 50 (V serisinin reddettigi bolge) + liq >= $20k
        (kosucu habitati, medyan ~$19k) + m5 > 0 (kosu hala canli) +
        safety ZORUNLU + rejim sol_h1 >= 0. Siralama en yuksek m5 once.
        3 slot, bilet min(bakiye/3, $70): vahsi sahada kucuk bilet,
        rug isirigi sinirli.
CIKIS : sabit tam tp YOK. Kismi kar kilidi: mfe >= +%15 gorulunce
        pozisyonun YARISI satilir (tp_yarim_15, bir kez), kalan yari
        trail ile kosar. Rug-hizi cokuslerde masadaki para yarilanir
        (2026-07-05 otopsisi: 4 rug = zararin %66'si).
        trail_18 (tepeden -%18) / stop_giris (+%10 hic gorulmemisken
        -%12) / timeout_360 (6 saat mutlak tavan).
COOLDOWN: kayip cikis 90dk, karli 30dk.
GIRIS TAZE-FIYAT TEYIDI (09 Tem gece): alim kaydedilmeden hemen once fiyat
        tazelenir (fast<=3s -> tek fetch -> tarama, fail-open). Taze fiyat
        taramanin +%2'den fazla ustundeyse giris iptal (taze_fiyat_kacti).
        Kayit: entry_price_source + entry_fresh_fark_pct.

Fill'ler sanal: gercek fiyat + v2'nin likidite-slippage modeli + gas.
Kadans v2 ile ayni; 3/16 interval faz kaydirmali (v9:1/8, ekg:1/4,
v6:3/8, golge:1/2, v7:5/8, v4:3/4, v8:7/8). X1_ENABLED=0 ile kapatilir.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from hibrit_trader.config import GAS_COST_USD
from hibrit_trader.entry_fresh import taze_teyit
from hibrit_trader.killswitch import is_active as kill_is_active
from hibrit_trader.live_sim import fetch_pool_snapshot
from hibrit_trader.momentum_session import (
    SCAN_INTERVAL_SEC,
    SOL_H1_CACHE_SEC,
    SOL_H1_STALE_MAX_SEC,
    SOL_USDC_POOL,
    _data_dir,
    _mom_slippage,
)
from hibrit_trader.paper import _now_iso, new_trade_id
from hibrit_trader.price_sanity import guard_price
from hibrit_trader.safety import check_token
from hibrit_trader.scanner import scan_all_cached as scan_all

log = logging.getLogger(__name__)

# ---- X1 esikleri (EKG-kalibreli kosucu avi) -----------------------------------
CHG_H1_MIN = float(os.getenv("X1_CHG_H1_MIN", "50"))    # kosucu habitati, ust bant yok
LIQ_MIN_USD = float(os.getenv("X1_LIQ_MIN_USD", "20000"))  # kosucu habitati (medyan ~$19k)
MAX_SLOTS = 3           # az pozisyon, genis nefes payi
START_BALANCE = float(os.getenv("X1_START_BALANCE", "1000"))
MAX_TICKET_USD = float(os.getenv("X1_MAX_TICKET_USD", "70"))  # vahsi sahada kucuk bilet
TRAIL_PCT = float(os.getenv("X1_TRAIL_PCT", "-18"))     # tepeden dusus esigi (EKG -20 + 2 puan pay)
EARLY_STOP_PCT = float(os.getenv("X1_EARLY_STOP_PCT", "-12"))  # yanlis binis freni
EARLY_MFE_PCT = float(os.getenv("X1_EARLY_MFE_PCT", "10"))     # bu gorulduyse fren devre disi
PARTIAL_MFE_PCT = float(os.getenv("X1_PARTIAL_MFE_PCT", "15"))  # kismi kar kilidi esigi
CEILING_SEC = 6 * 3600  # kosucular saatlerce kosuyor, 60dk bu ava dar
SOL_H1_MIN = float(os.getenv("MOM_SOL_H1_MIN", "0"))    # rejim esigi diger motorlarla ayni
COOLDOWN_LOSS_SEC = float(os.getenv("X1_COOLDOWN_LOSS_MIN", "90")) * 60
COOLDOWN_EXIT_SEC = float(os.getenv("X1_COOLDOWN_EXIT_MIN", "30")) * 60

STATE_FILE = "x1_state.json"
TRADES_FILE = "x1_trades.jsonl"


class X1Engine:
    """Sanal senaryo motoru. Kendi dosyalari, diger motorlara sifir dokunus."""

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
                log.critical("x1 state bozuk, yedege tasindi: %s", backup)
            except OSError:
                log.critical("x1 state bozuk ve yedeklenemedi, temiz baslaniyor")

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

        p = self._path("x1_engine.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("X1: baska bir instance calisiyor, motor baslatilmiyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    # ---- Ana dongu (v2 ile ayni kadans, 3/16 interval faz kaydirma) ------------
    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        log.warning(
            "X1 senaryo basladi (kosucu avcisi) - sanal $%.2f · slot %d · "
            "bilet<=$%.0f · giris liq>=$%.0f + h1>=%.0f + m5>0 · cikis trail %%%.0f / "
            "yarim tp mfe>=%%%.0f / erken fren %%%.0f (mfe<%.0f) / tavan %dsa",
            self.balance, MAX_SLOTS, MAX_TICKET_USD, LIQ_MIN_USD, CHG_H1_MIN,
            TRAIL_PCT, PARTIAL_MFE_PCT, EARLY_STOP_PCT, EARLY_MFE_PCT, CEILING_SEC // 3600,
        )
        self._save()
        time.sleep(SCAN_INTERVAL_SEC * 3 / 16)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("x1 tick hatasi")
            time.sleep(SCAN_INTERVAL_SEC)

    def tick(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
        self._save()

    # ---- Rejim (diger motorlarla ayni: esik 0, fail-open, kendi cache'i) --------
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

    # ---- Giris: kosucu tetigi (h1 >= 50, m5 > 0, en canli kosu once) ------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        if kill_is_active():
            return
        try:
            pairs = scan_all(self.settings.scan_chains)
        except Exception as e:
            log.warning("X1 giris tick atlandi, tarama hatasi: %r", e)
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
                continue  # kosucu habitati: V serisinin reddettigi bolge
            if getattr(pr, "chg_m5", 0.0) <= 0:
                continue  # kosu HALA canli olsun, olmus pumpa binme
            cands.append(pr)
        cands.sort(key=lambda pr: getattr(pr, "chg_m5", 0.0), reverse=True)  # en canli kosu once
        if not cands:
            return
        # Rejim FAIL-CLOSED (09 Tem): veri yoksa kapi KAPALI; son basarili
        # deger 10dk'ya kadar gecerli, sonrasinda giris yok.
        sol_h1 = None
        try:
            sol_h1 = self._sol_chg_h1(client)
        except Exception:
            log.debug("x1 rejim: sol_chg_h1 alinamadi", exc_info=True)
        if sol_h1 is None:
            ts, cached = self._sol_h1_cache
            if cached is not None and time.time() - ts <= SOL_H1_STALE_MAX_SEC:
                sol_h1 = cached
        if sol_h1 is None:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("X1 REJIM: sol_h1 verisi yok (fail-closed), giris kapali")
            return
        if sol_h1 < SOL_H1_MIN:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("X1 REJIM: sol_chg_h1 %.2f%% < %.2f%%, giris yok", sol_h1, SOL_H1_MIN)
            return
        if self._regime_logged:
            self._regime_logged = False
        budget_each = min(self.balance / empty, MAX_TICKET_USD)
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
            if self._open_position(pair, budget_each, sol_h1, client=client):
                empty -= 1
                held.add(pair.pool_address)
                held.add(pair.token_address)

    def _open_position(self, pair, usd: float, sol_h1: float | None = None,
                       client: httpx.Client | None = None) -> bool:
        gas = GAS_COST_USD.get(pair.chain, 0.1)
        if self.balance < usd + gas:
            return False
        taze = taze_teyit(pair, "X1", client)
        if taze.iptal:
            log.warning("X1 GIRIS IPTAL %s: taze fiyat taramanin %%%.2f ustunde (kaynak %s)",
                        pair.name, taze.fark_pct, taze.kaynak)
            return False
        slip = _mom_slippage(usd, pair.liquidity_usd)
        eff_price = taze.fiyat * (1 + slip)
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
            "sol_chg_h1": sol_h1,
            "entry_price_source": taze.kaynak,
            "entry_fresh_fark_pct": taze.fark_pct,
            "entry_slip_pct": round(slip * 100, 4),
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "last_price": eff_price,
            # tepe/nefes gecmisi (EKG kalibrasyonu icin)
            "peak_price": eff_price,
            "trough_price": None,
            "nefes_n": 0,
            "nefes_en_derin_pct": 0.0,
            "yarim_satildi": False,
        }
        self.balance -= (usd + gas)
        self.positions.append(pos)
        self._save()
        log.warning("X1 BUY %s $%.2f @ %.8g (h1 %.1f%%, m5 %.1f%%, liq $%.0f)",
                    pair.name, usd, eff_price, pair.chg_h1,
                    getattr(pair, "chg_m5", 0.0), pair.liquidity_usd)
        return True

    # ---- Cikis: tp_yarim_15 (kismi) / stop_giris / trail_18 / timeout_360 ---------
    def _manage_exits(self, client: httpx.Client) -> None:
        now = time.time()
        for pos in list(self.positions):
            price, liq = fetch_pool_snapshot(client, pos["chain"], pos["pool_address"])
            if price is None or price <= 0:
                price = pos["last_price"]
            price, ariza = guard_price(pos, price, now, "X1", liquidity_usd=liq)
            if ariza:
                continue  # veri arizasi: islem tetikleme, degerleme son gecerli fiyatta
            pos["last_price"] = price
            entry = pos["entry_price"]
            pnl_pct = (price / entry - 1) * 100 if entry > 0 else 0.0
            if pnl_pct > pos["mfe_pct"]:
                pos["mfe_pct"] = round(pnl_pct, 4)
            if pnl_pct < pos["mae_pct"]:
                pos["mae_pct"] = round(pnl_pct, 4)

            # tepe/nefes takibi: yeni tepe eski dususu "nefes" olarak muhurler
            peak = pos["peak_price"]
            if price >= peak:
                trough = pos.get("trough_price")
                if trough is not None and trough < peak:
                    depth = (trough / peak - 1) * 100
                    pos["nefes_n"] += 1
                    if depth < pos["nefes_en_derin_pct"]:
                        pos["nefes_en_derin_pct"] = round(depth, 4)
                    pos["trough_price"] = None
                pos["peak_price"] = price
            else:
                trough = pos.get("trough_price")
                pos["trough_price"] = price if trough is None else min(trough, price)

            dd_pct = (price / pos["peak_price"] - 1) * 100
            age = now - pos["opened_ts"]

            # kismi kar kilidi: mfe +%15'e ilk ulasista yarim sat, bir kez
            if not pos.get("yarim_satildi") and pos["mfe_pct"] >= PARTIAL_MFE_PCT:
                self._partial_take(pos, price, now)

            reason = None
            if pos["mfe_pct"] < EARLY_MFE_PCT and pnl_pct <= EARLY_STOP_PCT:
                reason = "stop_giris"   # yanlis binis: +%10 hic gorulmedi
            elif dd_pct <= TRAIL_PCT:
                reason = "trail_18"     # olum donusu: tepeden -%18
            elif age >= CEILING_SEC:
                reason = "timeout_360"
            if reason:
                self._close_position(pos, price, reason, now)

    def _partial_take(self, pos: dict, price: float, now: float) -> None:
        half_tokens = pos["amount_token"] / 2
        half_cost = pos["cost_usd"] / 2
        slip = _mom_slippage(half_cost, pos["liq_entry"])
        eff_price = price * (1 - slip)
        gas = GAS_COST_USD.get(pos["chain"], 0.1)
        proceeds = half_tokens * eff_price - gas
        pnl = proceeds - half_cost
        pnl_pct = (eff_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0.0
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
            "entry_price_source": pos.get("entry_price_source"),
            "entry_fresh_fark_pct": pos.get("entry_fresh_fark_pct"),
            "cost_usd": round(half_cost, 4),
            "proceeds_usd": round(proceeds, 4),
            "pnl_usd": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 3),
            "mfe_pct": pos["mfe_pct"],
            "mae_pct": pos["mae_pct"],
            "peak_price": pos["peak_price"],
            "nefes_n": pos["nefes_n"],
            "nefes_en_derin_pct": pos["nefes_en_derin_pct"],
            "hold_sec": round(now - pos["opened_ts"], 1),
            "exit_reason": "tp_yarim_15",
            "friction_pct": round(pos.get("entry_slip_pct", 0.0) + slip * 100, 4),
            "opened_at": pos["opened_at"],
            "closed_at": _now_iso(),
        })
        self.balance += proceeds
        self.realized_pnl += pnl
        pos["amount_token"] -= half_tokens
        pos["cost_usd"] = round(pos["cost_usd"] - half_cost, 4)
        pos["yarim_satildi"] = True
        self._save()
        log.warning("X1 YARIM SAT %s $%.2f kilitlendi @ %.8g (mfe %.1f%%), kalan yari trail ile",
                    pos["pair"], proceeds, eff_price, pos["mfe_pct"])

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
            "entry_price_source": pos.get("entry_price_source"),
            "entry_fresh_fark_pct": pos.get("entry_fresh_fark_pct"),
            "cost_usd": round(cost, 4),
            "proceeds_usd": round(proceeds, 4),
            "pnl_usd": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 3),
            "mfe_pct": pos["mfe_pct"],
            "mae_pct": pos["mae_pct"],
            "peak_price": pos["peak_price"],
            "nefes_n": pos["nefes_n"],
            "nefes_en_derin_pct": pos["nefes_en_derin_pct"],
            "hold_sec": hold_sec,
            "exit_reason": reason,
            "friction_pct": round(pos.get("entry_slip_pct", 0.0) + slip * 100, 4),
            "opened_at": pos["opened_at"],
            "closed_at": _now_iso(),
        })
        self.balance += proceeds
        self.realized_pnl += pnl
        cd = COOLDOWN_LOSS_SEC if pnl < 0 else COOLDOWN_EXIT_SEC
        if pos.get("token_address"):
            self._cooldown_until[pos["token_address"]] = now + cd
        try:
            self.positions.remove(pos)
        except ValueError:
            pass
        self._save()
        log.warning("X1 SELL %s pnl $%.2f (%.2f%%) - %s, hold %.0fs "
                    "(mfe %.1f%% mae %.1f%% nefes %d en_derin %.1f%%)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec,
                    pos["mfe_pct"], pos["mae_pct"],
                    pos["nefes_n"], pos["nefes_en_derin_pct"])
