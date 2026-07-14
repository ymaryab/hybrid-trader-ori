"""V7C senaryo motoru: V7 iskeleti, major/likit evren + majore uygun h1 bandi 2..10.

Amac: evren farkinin etkisini olcmek. V7 memecoin taramasinda avlanirken V7C
ayni cikis/rejim/boyut kurallariyla major evrende avlanir; giris bandi major
oynakligina gore 2..10'a cekildi, baska hicbir kural degismez.

Diger motorlara SIFIR dokunus. Sadece su dosyalara yazar:
  data/v7c_state.json     (sanal bakiye + acik pozisyonlar)
  data/v7c_trades.jsonl   (her sanal kapanista kayit)
  data/v7c_universe.json  (major evren, gunde bir tazelenir)

EVREN: M1 altyapisi yeniden kullanilir. SEED_TOKENS gunde bir
DexScreener'dan dogrulanir (Jupiter hakem + tutarli havuz + hafif honeypot),
en likit havuzu >= V7C_MIN_LIQ_USD (varsayilan $3M) olanlar evrene girer.

KURALLAR (v7 iskeletinde iki fark: evren + h1 bandi):
  GIRIS : h1 bandi 2..10 (major oynakligina uygun; memecoin bandi 10..50
          majorlerde neredeyse hic tetiklenmiyordu), likidite esigi, safety
          check, taze teyit, kasaya oranli boyut (balance/empty), 5 slot,
          baslangic $1000.
  REJIM : sol_h1 < 0.5 iken giris yok (fail-closed, paylasimli cache).
  CIKIS : tp +%2 / -%10 felaket freni (her an) / 30dk sabir sonrasi -%2 stop /
          60dk tavan.

MOD: SABIT PAPER. BROKER_MODE zincirinden BAGIMSIZ; global mod live olsa
bile V7C paper kalir (exec katmani dogrudan PaperExecBroker).
V7C_ENABLED=0 ile kapatilir.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from hibrit_trader.broker import ExecOrder, PaperExecBroker, jupiter_referans_fiyat
from hibrit_trader.config import API, GAS_COST_USD
from hibrit_trader.dexscreener_scan import pair_from_dexscreener
from hibrit_trader.entry_fresh import (
    HuniSayac,
    rejim_reject_kaydet,
    safety_reject_kaydet,
    taze_teyit,
)
from hibrit_trader.killswitch import is_active as kill_is_active
from hibrit_trader.live_sim import fetch_pool_snapshot
from hibrit_trader.m1_session import SEED_TOKENS, _best_sane_pool, _light_honeypot_ok
from hibrit_trader.momentum_session import (
    SCAN_INTERVAL_SEC,
    _data_dir,
    _mom_slippage,
    sol_chg_h1,
)
from hibrit_trader.paper import _now_iso, new_trade_id
from hibrit_trader.price_sanity import guard_price
from hibrit_trader.safety import check_token

log = logging.getLogger(__name__)

# ---- V7C esikleri: v7 varsayilanlari; farklar evren likiditesi + h1 bandi 2..10
CHG_H1_MIN = 2.0
CHG_H1_MAX = 10.0
LIQ_MIN_USD = float(os.getenv("V7C_MIN_LIQ_USD", "3000000"))  # evren + giris esigi
UNIVERSE_REFRESH_SEC = 24 * 3600
MAX_SLOTS = 5
START_BALANCE = float(os.getenv("V7C_START_BALANCE", "1000"))
TP_PCT = 2.0            # +%2 UZERI satar (14 Tem: v7 ile birebir, esitlik satmaz)
GRACE_SEC = 120 * 60    # 14 Tem: v7 ile birebir 120dk
LATE_STOP_PCT = -2.0
CEILING_SEC = 120 * 60  # 14 Tem: v7 ile birebir 120dk
SOL_H1_MIN = 0.5
DAILY_LOSS_LIMIT_USD = float(os.getenv("MOM_DAILY_LOSS_LIMIT_USD", "0"))
COOLDOWN_LOSS_SEC = float(os.getenv("MOM_COOLDOWN_STOP_MIN", "60")) * 60
COOLDOWN_EXIT_SEC = float(os.getenv("MOM_COOLDOWN_EXIT_MIN", "15")) * 60

STATE_FILE = "v7c_state.json"
TRADES_FILE = "v7c_trades.jsonl"
UNIVERSE_FILE = "v7c_universe.json"


class V7CEngine:
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
        self._regime_logged = False
        self._kill_logged = False
        self._day_key: str = ""
        self._day_realized: float = 0.0
        self._limit_logged = False
        self._huni = HuniSayac("V7C")
        self._universe: list[dict] = []
        self._universe_ts: float = 0.0
        self._lock_fh = None
        # SABIT PAPER: BROKER_MODE ne olursa olsun exec katmani paper kalir.
        self._exec = PaperExecBroker()
        self._load()
        self._load_universe()
        self._restore_day_realized()

    # ---- Dosya isleri (v7 ile ayni: atomik save, aninda persist) ----------------
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
                log.critical("v7c state bozuk, yedege tasindi: %s", backup)
            except OSError:
                log.critical("v7c state bozuk ve yedeklenemedi, temiz baslaniyor")

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

    # ---- Gun ici realized PnL sayaci (v7 ile ayni) -------------------------------
    def _day_realized_add(self, pnl: float, now: float) -> None:
        key = time.strftime("%Y-%m-%d", time.gmtime(now))
        if key != self._day_key:
            self._day_key = key
            self._day_realized = 0.0
            self._limit_logged = False
        self._day_realized += pnl

    def _restore_day_realized(self) -> None:
        self._day_key = time.strftime("%Y-%m-%d", time.gmtime())
        try:
            p = self._path(TRADES_FILE)
            if not p.exists():
                return
            total = 0.0
            for ln in p.read_text().splitlines():
                if not ln.strip():
                    continue
                try:
                    t = json.loads(ln)
                    ts = float(t.get("ts") or 0.0)
                    if time.strftime("%Y-%m-%d", time.gmtime(ts)) == self._day_key:
                        total += float(t.get("pnl_usd") or 0.0)
                except Exception:
                    continue
            self._day_realized = total
        except Exception:
            log.debug("V7C gun ici pnl geri yuklenemedi", exc_info=True)

    def _entries_blocked(self) -> str | None:
        if kill_is_active():
            if not self._kill_logged:
                self._kill_logged = True
                log.critical("V7C: kill-switch AKTIF, yeni girisler durdu (cikislar suruyor)")
            return "kill_switch"
        if self._kill_logged:
            self._kill_logged = False
            log.warning("V7C: kill-switch kalkti, girisler serbest")
        if DAILY_LOSS_LIMIT_USD > 0:
            key = time.strftime("%Y-%m-%d", time.gmtime())
            if key != self._day_key:
                self._day_key = key
                self._day_realized = 0.0
                self._limit_logged = False
            if self._day_realized <= -DAILY_LOSS_LIMIT_USD:
                if not self._limit_logged:
                    self._limit_logged = True
                    log.critical(
                        "V7C: gunluk zarar limiti asildi ($%.2f <= -$%.2f), "
                        "bugun (UTC) yeni giris yok", self._day_realized, DAILY_LOSS_LIMIT_USD,
                    )
                return "daily_loss_limit"
        return None

    def _exec_fill(self, yon: str, token_address: str, *, usd: float = 0.0,
                   amount_token: float = 0.0, ref_fiyat: float = 0.0):
        """Paper exec: muhasebe paper kalir, devam True. Mod sabit paper oldugu
        icin live dallanmasi hicbir zaman calismaz (bilincli)."""
        try:
            self._exec.execute(ExecOrder(
                engine="V7C", yon=yon, token_address=token_address,
                usd=usd, amount_token=amount_token, ref_fiyat=ref_fiyat))
        except Exception as e:
            log.error("V7C yurutme hatasi (%s %s): %s", yon, token_address[:8], e)
        return True, None

    def _acquire_lock(self) -> bool:
        import fcntl

        p = self._path("v7c_engine.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("V7C: baska bir instance calisiyor, motor baslatilmiyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    # ---- Evren: M1 deseni, esik V7C_MIN_LIQ_USD ----------------------------------
    def _load_universe(self) -> None:
        p = self._path(UNIVERSE_FILE)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text())
            self._universe = list(data.get("tokens") or [])
            self._universe_ts = float(data.get("updated_ts") or 0.0)
        except Exception:
            log.warning("v7c universe dosyasi okunamadi, tazelenecek")

    def _refresh_universe(self, client: httpx.Client) -> None:
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
                ref = jupiter_referans_fiyat(addr)
                if ref is None:
                    log.warning("V7C EVREN: %s icin Jupiter hakem yok (fail-closed), disarida", sym)
                    continue
                best = _best_sane_pool(pairs, ref_fiyat=ref)
                if best is None:
                    log.warning("V7C EVREN: %s icin fiyati tutarli havuz yok, disarida", sym)
                    continue
                liq = float((best.get("liquidity") or {}).get("usd") or 0)
                if liq < LIQ_MIN_USD:
                    continue
                if not _light_honeypot_ok(client, addr):
                    log.warning("V7C EVREN: %s hafif honeypot kontrolunden gecemedi, disarida", sym)
                    continue
                tokens.append({
                    "symbol": sym,
                    "token_address": addr,
                    "pool_address": str(best.get("pairAddress") or ""),
                    "liq_usd": round(liq, 0),
                    "ref_fiyat": ref,
                })
            except Exception:
                log.debug("v7c universe: %s dogrulanamadi", sym, exc_info=True)
            time.sleep(0.4)
        if not tokens:
            log.warning("V7C EVREN: tazeleme bos dondu, eski evren korunuyor (n=%d)",
                        len(self._universe))
            self._universe_ts = time.time()
            return
        self._universe = tokens
        self._universe_ts = time.time()
        p = self._path(UNIVERSE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "updated_ts": round(self._universe_ts, 3),
            "updated_at": _now_iso(),
            "liq_min_usd": LIQ_MIN_USD,
            "tokens": tokens,
        }, ensure_ascii=False, indent=2)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(payload)
        os.replace(tmp, p)
        log.warning("V7C EVREN tazelendi: %d token (%s)", len(tokens),
                    ", ".join(t["symbol"] for t in tokens))

    def _scan_universe(self, client: httpx.Client) -> list:
        """Evren havuzlarini TEK istekle cek (M1 deseni, 30 havuz siniri)."""
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

    # ---- Ana dongu (11/16 faz kaydirma: diger motorlarla API carpismasin) --------
    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        log.warning(
            "V7C senaryo basladi (v7 kurallari, major evren) - sanal $%.2f · slot %d · "
            "evren+giris liq>=$%.0f · h1 %.0f..%.0f · rejim sol_h1>=%.1f · "
            "cikis tp+%.0f%% uzeri / %dm sabir sonrasi stop%%%.0f / tavan %dm · PAPER sabit",
            self.balance, MAX_SLOTS, LIQ_MIN_USD, CHG_H1_MIN, CHG_H1_MAX, SOL_H1_MIN,
            TP_PCT, GRACE_SEC // 60, LATE_STOP_PCT, CEILING_SEC // 60,
        )
        self._save()
        time.sleep(SCAN_INTERVAL_SEC * 11 / 16)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("v7c tick hatasi")
            time.sleep(SCAN_INTERVAL_SEC)

    def tick(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
        self._save()

    def _sol_chg_h1(self, client: httpx.Client) -> float | None:
        return sol_chg_h1(client)

    # ---- Giris (v7 ile birebir; tek fark: aday kaynagi major evren) ---------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        if self._entries_blocked():
            return
        try:
            pairs = self._scan_universe(client)
        except Exception as e:
            log.warning("V7C giris tick atlandi, tarama hatasi: %r", e)
            return
        held = {p["pool_address"] for p in self.positions}
        held |= {p["token_address"] for p in self.positions if p.get("token_address")}
        now = time.time()
        self._cooldown_until = {
            t: ts for t, ts in self._cooldown_until.items() if ts > now
        }
        cands = []
        liq_ok = 0
        for pr in pairs:
            if pr.pool_address in held or pr.token_address in held or pr.price_usd <= 0:
                continue
            if self._cooldown_until.get(pr.token_address, 0.0) > now:
                continue
            if pr.liquidity_usd < LIQ_MIN_USD:
                continue
            liq_ok += 1
            if not (CHG_H1_MIN <= getattr(pr, "chg_h1", 0.0) <= CHG_H1_MAX):
                continue
            cands.append(pr)
        cands.sort(key=lambda pr: pr.chg_h1, reverse=True)
        self._huni.ekle(len(pairs), liq_ok, len(cands), now)
        if not cands:
            return
        sol_h1 = self._sol_chg_h1(client)
        if sol_h1 is None:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("V7C REJIM: sol_h1 verisi yok (fail-closed), giris kapali")
            rejim_reject_kaydet(cands, "V7C", None)
            return
        if sol_h1 < SOL_H1_MIN:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("V7C REJIM: sol_chg_h1 %.2f%% < %.2f%%, giris yok", sol_h1, SOL_H1_MIN)
            rejim_reject_kaydet(cands, "V7C", sol_h1)
            return
        if self._regime_logged:
            self._regime_logged = False
        budget_each = self.balance / empty
        for pair in cands:
            if empty <= 0 or budget_each < 1.0:
                break
            try:
                report = check_token(client, pair.chain, pair.token_address)
            except Exception as e:
                safety_reject_kaydet(pair, "V7C", "safety_hata", type(e).__name__)
                continue
            time.sleep(0.2 if self._aggressive else 1.5)
            if not report.ok:
                safety_reject_kaydet(
                    pair, "V7C", report.kapi or "safety_red", "; ".join(report.reasons[:2])
                )
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
        taze = taze_teyit(pair, "V7C", client)
        if taze.iptal:
            log.warning("V7C GIRIS IPTAL %s: taze fiyat taramanin %%%.2f ustunde (kaynak %s)",
                        pair.name, taze.fark_pct, taze.kaynak)
            return False
        slip = _mom_slippage(usd, pair.liquidity_usd)
        eff_price = taze.fiyat * (1 + slip)
        self._exec_fill("al", pair.token_address, usd=usd, ref_fiyat=eff_price)
        amount_token = usd / eff_price
        now = time.time()
        pos = {
            "trade_id": new_trade_id(pair.pool_address, now),
            "pair": pair.name,
            "chain": pair.chain,
            "token_address": pair.token_address,
            "pool_address": pair.pool_address,
            "entry_price": eff_price,
            "amount_token": amount_token,
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
        }
        self.balance -= (usd + gas)
        self.positions.append(pos)
        self._save()
        log.warning("V7C BUY %s $%.2f @ %.8g (h1 %.1f%%, liq $%.0f)",
                    pair.name, usd, eff_price, pair.chg_h1, pair.liquidity_usd)
        return True

    # ---- Cikis: v7 ile birebir (tp_2 +%2 uzeri / stop_gec / timeout_120) ----------
    def _manage_exits(self, client: httpx.Client) -> None:
        now = time.time()
        for pos in list(self.positions):
            price, liq = fetch_pool_snapshot(client, pos["chain"], pos["pool_address"])
            if price is None or price <= 0:
                price = pos["last_price"]
            price, ariza = guard_price(pos, price, now, "V7C", liquidity_usd=liq)
            if ariza:
                continue
            pos["last_price"] = price
            entry = pos["entry_price"]
            pnl_pct = (price / entry - 1) * 100 if entry > 0 else 0.0
            if pnl_pct > pos["mfe_pct"]:
                pos["mfe_pct"] = round(pnl_pct, 4)
            if pnl_pct < pos["mae_pct"]:
                pos["mae_pct"] = round(pnl_pct, 4)
            age = now - pos["opened_ts"]

            reason = None
            if pnl_pct > TP_PCT:
                reason = "tp_2"
            elif age >= GRACE_SEC and pnl_pct <= LATE_STOP_PCT:
                reason = "stop_gec"
            elif age >= CEILING_SEC:
                reason = "timeout_120"
            if reason:
                self._close_position(pos, price, reason, now)

    def _close_position(self, pos: dict, price: float, reason: str, now: float) -> None:
        cost = pos["cost_usd"]
        slip = _mom_slippage(cost, pos["liq_entry"])
        eff_price = price * (1 - slip)
        self._exec_fill("sat", pos["token_address"],
                        amount_token=pos["amount_token"], ref_fiyat=eff_price)
        gas = GAS_COST_USD.get(pos["chain"], 0.1)
        proceeds = pos["amount_token"] * eff_price - gas
        pnl = proceeds - cost
        hold_sec = round(now - pos["opened_ts"], 1)
        pnl_pct = (eff_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0.0

        row = {
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
            "hold_sec": hold_sec,
            "exit_reason": reason,
            "friction_pct": round(pos.get("entry_slip_pct", 0.0) + slip * 100, 4),
            "opened_at": pos["opened_at"],
            "closed_at": _now_iso(),
        }
        self._append_trade(row)
        self.balance += proceeds
        self.realized_pnl += pnl
        self._day_realized_add(pnl, now)
        cd = COOLDOWN_LOSS_SEC if reason == "stop_gec" else COOLDOWN_EXIT_SEC
        if pos.get("token_address"):
            self._cooldown_until[pos["token_address"]] = now + cd
        try:
            self.positions.remove(pos)
        except ValueError:
            pass
        self._save()
        log.warning("V7C SELL %s pnl $%.2f (%.2f%%) - %s, hold %.0fs (mfe %.1f%% mae %.1f%%)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec, pos["mfe_pct"], pos["mae_pct"])
