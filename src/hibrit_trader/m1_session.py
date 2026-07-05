"""M1 senaryo motoru: v7 iskeletinin MAJOR-TOKEN versiyonu, yeni av sahasi.

Diger motorlara SIFIR dokunus. Sadece su dosyalara yazar:
  data/m1_state.json     (sanal bakiye + acik pozisyonlar)
  data/m1_trades.jsonl   (her sanal kapanista kayit)
  data/m1_universe.json  (sabit evren, gunde bir tazelenir)

AV SAHASI (kritik fark): memecoin taramasi DEGIL. Sabit evren: Solana'nin
en likit majorlari (SOL, WIF, RAY, TRUMP vb). Aday listesi SEED_TOKENS'ta;
gunde bir DexScreener'dan dogrulanir, en likit havuzu >= $3M olanlar evrene
girer. Rug riski ~sifir, friction ~sifir olan gol.

KURALLAR (v7 iskeleti, major oynakligina olcekli):
  GIRIS : evrenden chg_h1 1.5..15 (majorde %1.5 anlamli; dikey anomali disari).
          Siralama: en yuksek m5 (taze ivme). 5 slot, esit bolusum.
  REJIM : sol_h1 < 0.3 iken giris yok (major evren SOL'la yuksek korelasyonlu).
  CIKIS : tp +%1.2 / -%4 felaket freni (her an) / 20dk sabir sonrasi -%1.5
          stop / 90dk tavan.
  CDOWN : kayipli cikis 60dk, karli/notr cikis 30dk.
  SAFETY: evren kurulurken hafif honeypot kontrolu (transfer hook / oynak
          transfer ucreti); evren zaten oturmus major oldugu icin fail-open.
          Giris aninda ekstra tarama yok.

Fill modeli v2 ile ayni (_mom_slippage + gas). $200 bilet / $3M+ havuz ile
slip ~%0.007 cikar (beklenen %0.05-0.1 bandinin da altinda); BUY/SELL
loglarinda slip ve friction ayrica yazilir, trade kaydinda friction_pct var.
Kadans SCAN_INTERVAL_SEC, 5/16 faz kaydirmali. M1_ENABLED=0 ile kapatilir.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from hibrit_trader.config import API, GAS_COST_USD
from hibrit_trader.dexscreener_scan import pair_from_dexscreener
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

log = logging.getLogger(__name__)

# ---- Evren adaylari (gunde bir canli dogrulanir; yanlis/olu adres dogal elenir) ----
SEED_TOKENS: dict[str, str] = {
    "SOL": "So11111111111111111111111111111111111111112",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    "RENDER": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
    "HNT": "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "MEW": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
    "BOME": "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82",
    "W": "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ",
    "KMNO": "KMNo3nJsBXfcpJTVhZcXLW7RmTwTt4GVFE7suUBo9sS",
    "WEN": "WENWENvqqNya429ubCdR81ZmD69brwQaaBYY6p3LCpk",
    "PENGU": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
    "TRUMP": "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",
    "MSOL": "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "JITOSOL": "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
    "FARTCOIN": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
    "PUMP": "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",
    "MOODENG": "ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzPJBY",
    "GIGA": "63LfDmNb3MQ8mw9MtZ2To9bEA2M71kZUUGq5tiJxcqj9",
    "SPX": "J3NKxxXZcnNiMjKw9hYb2K4LUxgwB6t1FtPtQVsv3KFr",
}

# ---- M1 esikleri (major olcekli) ---------------------------------------------
CHG_H1_MIN = float(os.getenv("M1_CHG_H1_MIN", "1.5"))
CHG_H1_MAX = float(os.getenv("M1_CHG_H1_MAX", "15"))
UNIVERSE_LIQ_MIN = float(os.getenv("M1_UNIVERSE_LIQ_MIN", "3000000"))
UNIVERSE_REFRESH_SEC = 24 * 3600
MAX_SLOTS = 5
START_BALANCE = float(os.getenv("M1_START_BALANCE", "1000"))
TP_PCT = 1.2            # major olcekli hedef; friction ~%0.01 oldugu icin net pozitif
GRACE_SEC = 20 * 60     # ilk 20dk asagida stop yok (sabir)
LATE_STOP_PCT = -1.5    # 20dk sonrasi: girisin -%1.5 alti SAT
DISASTER_PCT = float(os.getenv("M1_DISASTER_PCT", "-4"))  # her an mutlak taban
CEILING_SEC = 90 * 60   # 90dk tavan
SOL_H1_MIN = float(os.getenv("M1_SOL_H1_MIN", "0.3"))
COOLDOWN_LOSS_SEC = float(os.getenv("M1_COOLDOWN_LOSS_MIN", "60")) * 60
COOLDOWN_EXIT_SEC = float(os.getenv("M1_COOLDOWN_EXIT_MIN", "30")) * 60

STATE_FILE = "m1_state.json"
TRADES_FILE = "m1_trades.jsonl"
UNIVERSE_FILE = "m1_universe.json"


def _light_honeypot_ok(client: httpx.Client, token_address: str) -> bool:
    """Hafif kontrol: sadece tuzak sinyalleri. Evren oturmus major, fail-open."""
    try:
        url = f"{API['goplus']}/solana/token_security"
        resp = client.get(url, params={"contract_addresses": token_address}, timeout=15)
        resp.raise_for_status()
        d = (resp.json().get("result") or {}).get(token_address)
        if not d:
            return True
        if (d.get("transfer_fee_upgradable") or {}).get("status") == "1":
            return False
        if d.get("transfer_hook") not in (None, [], ""):
            return False
        return True
    except Exception:
        return True


class M1Engine:
    """Sanal major-token motoru. Kendi dosyalari, diger motorlara sifir dokunus."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.balance: float = START_BALANCE
        self.start_balance: float = START_BALANCE
        self.realized_pnl: float = 0.0
        self.positions: list[dict] = []
        self.created_ts: float = time.time()
        self._cooldown_until: dict[str, float] = {}
        self._sol_h1_cache: tuple[float, float | None] = (0.0, None)
        self._regime_logged = False
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
                log.critical("m1 state bozuk, yedege tasindi: %s", backup)
            except OSError:
                log.critical("m1 state bozuk ve yedeklenemedi, temiz baslaniyor")

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

        p = self._path("m1_engine.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("M1: baska bir instance calisiyor, motor baslatilmiyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    # ---- Evren: sabit major listesi, gunde bir tazele ---------------------------
    def _load_universe(self) -> None:
        p = self._path(UNIVERSE_FILE)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text())
            self._universe = list(data.get("tokens") or [])
            self._universe_ts = float(data.get("updated_ts") or 0.0)
        except Exception:
            log.warning("m1 universe dosyasi okunamadi, tazelenecek")

    def _refresh_universe(self, client: httpx.Client) -> None:
        """SEED adaylarini DexScreener'dan dogrula: en likit havuzu >= $3M olanlar evrene."""
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
                best = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
                liq = float((best.get("liquidity") or {}).get("usd") or 0)
                if liq < UNIVERSE_LIQ_MIN:
                    continue
                if not _light_honeypot_ok(client, addr):
                    log.warning("M1 EVREN: %s hafif honeypot kontrolunden gecemedi, disarida", sym)
                    continue
                tokens.append({
                    "symbol": sym,
                    "token_address": addr,
                    "pool_address": str(best.get("pairAddress") or ""),
                    "liq_usd": round(liq, 0),
                })
            except Exception:
                log.debug("m1 universe: %s dogrulanamadi", sym, exc_info=True)
            time.sleep(0.4)
        if not tokens:
            log.warning("M1 EVREN: tazeleme bos dondu, eski evren korunuyor (n=%d)", len(self._universe))
            self._universe_ts = time.time()  # bos donuste de 24 saat bekle, API'yi dovme
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
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(payload)
        os.replace(tmp, p)
        log.warning("M1 EVREN tazelendi: %d token (%s)", len(tokens),
                    ", ".join(t["symbol"] for t in tokens))

    def _scan_universe(self, client: httpx.Client) -> list:
        """Evren havuzlarini TEK istekle cek (pairs endpoint, 30 havuz siniri)."""
        if time.time() - self._universe_ts > UNIVERSE_REFRESH_SEC:
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

    # ---- Ana dongu (5/16 interval faz kaydirma) ---------------------------------
    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        log.warning(
            "M1 senaryo basladi (major evren) - sanal $%.2f · slot %d · "
            "evren liq>=$%.0f · giris h1 %.1f..%.1f (m5 sirali) · rejim sol_h1>=%.1f · "
            "cikis tp+%.1f%% / fren %%%.0f / %dm sabir sonrasi stop%%%.1f / tavan %dm",
            self.balance, MAX_SLOTS, UNIVERSE_LIQ_MIN, CHG_H1_MIN, CHG_H1_MAX,
            SOL_H1_MIN, TP_PCT, DISASTER_PCT, GRACE_SEC // 60, LATE_STOP_PCT,
            CEILING_SEC // 60,
        )
        self._save()
        time.sleep(SCAN_INTERVAL_SEC * 5 / 16)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("m1 tick hatasi")
            time.sleep(SCAN_INTERVAL_SEC)

    def tick(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
        self._save()

    # ---- Rejim (v7 ile ayni mekanik, esik 0.3) ----------------------------------
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

    # ---- Giris: evrenden h1 1.5..15, en taze ivme (m5) once ----------------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        if kill_is_active():
            return
        try:
            pairs = self._scan_universe(client)
        except Exception:
            log.debug("m1 scan hatasi", exc_info=True)
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
            if pr.liquidity_usd < UNIVERSE_LIQ_MIN:
                continue  # havuz gun ici boslamis olabilir, slip garantisi icin taze kontrol
            if not (CHG_H1_MIN <= getattr(pr, "chg_h1", 0.0) <= CHG_H1_MAX):
                continue
            cands.append(pr)
        cands.sort(key=lambda pr: getattr(pr, "chg_m5", 0.0), reverse=True)  # taze ivme once
        if not cands:
            return
        sol_h1 = None
        try:
            sol_h1 = self._sol_chg_h1(client)
        except Exception:
            log.debug("m1 rejim: sol_chg_h1 alinamadi, filtre atlandi", exc_info=True)
        if sol_h1 is not None and sol_h1 < SOL_H1_MIN:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("M1 REJIM: sol_chg_h1 %.2f%% < %.2f%%, giris yok", sol_h1, SOL_H1_MIN)
            return
        if sol_h1 is not None and self._regime_logged:
            self._regime_logged = False
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
            "sol_chg_h1": sol_h1,
            "entry_slip_pct": round(slip * 100, 4),
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "last_price": eff_price,
        }
        self.balance -= (usd + gas)
        self.positions.append(pos)
        self._save()
        # slip dogrulama logu: major havuz derinliginde ~%0.005-0.1 beklenir
        log.warning("M1 BUY %s $%.2f @ %.8g (h1 %.2f%%, m5 %.2f%%, liq $%.0f, slip %%%.4f)",
                    pair.name, usd, eff_price, pair.chg_h1,
                    getattr(pair, "chg_m5", 0.0), pair.liquidity_usd, slip * 100)
        return True

    # ---- Cikis: tp_1_2 / stop_felaket (-%4) / stop_gec (20dk, -%1.5) / timeout_90 --
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
            age = now - pos["opened_ts"]

            reason = None
            if pnl_pct >= TP_PCT:
                reason = "tp_1_2"
            elif pnl_pct <= DISASTER_PCT:
                reason = "stop_felaket"
            elif age >= GRACE_SEC and pnl_pct <= LATE_STOP_PCT:
                reason = "stop_gec"
            elif age >= CEILING_SEC:
                reason = "timeout_90"
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
        friction_pct = round(pos.get("entry_slip_pct", 0.0) + slip * 100, 4)

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
        log.warning("M1 SELL %s pnl $%.2f (%.2f%%) - %s, hold %.0fs "
                    "(mfe %.1f%% mae %.1f%% friction %%%.4f)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec,
                    pos["mfe_pct"], pos["mae_pct"], friction_pct)
