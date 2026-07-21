"""V7HT paper motoru — v7hizli klonu + giris tavanlari (21 Tem A/B).

A/B amaci: v7hizli kural seti AYNEN korunur (TP+%2 tek cikis, stop yok),
SADECE girise h1<=150 ve m5<=75 tavanlari eklenir. Cuval kuyrugu
(BULLCAT tipi pompa girisleri) tavanla kesilirse fark defterde gorunur.

Kural seti (kullanici, 2026-07-15):
  GIRIS : rejim SOL_h1>=0.35 · momentum 10<=chg_h1<=50 · liq>=$100k
          taze fiyat<=+%2 · safety + kasa dagilim + bos slot (max 5)
          cooldown normal 15dk / stop 60dk (stop yok, ama tutarlilik icin sabit)
  CIKIS : SADECE tp_2 (giristen +%2 gorulunce sat)
          stop YOK, zaman asimi YOK, felaket YOK
          Satis slippage: normal 150 bps, stop_felaket 1000 bps (tutarlilik icin sabit)

MOD: SABIT PAPER. BROKER_MODE ne olursa olsun exec paper. Canli para tasimaz.
V7HT_ENABLED=0 ile kapatilir.

Diger motorlara SIFIR dokunus. Sadece su dosyalara yazar:
  data/v7ht_state.json   (sanal bakiye + acik pozisyonlar)
  data/v7ht_trades.jsonl (her sanal kapanista kayit)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from hibrit_trader.broker import ExecOrder, PaperExecBroker, init_motor_exec, make_exec_broker  # noqa: F401
from hibrit_trader.config import GAS_COST_USD
from hibrit_trader.entry_fresh import (
    HuniSayac,
    rejim_reject_kaydet,
    safety_reject_kaydet,
    taze_teyit,
)
from hibrit_trader.fast_price import get_feed
from hibrit_trader.killswitch import is_active as kill_is_active
from hibrit_trader.killswitch import notify
from hibrit_trader.uyari_notify import kritik_uyari
from hibrit_trader.live_sim import fetch_pool_snapshot
from hibrit_trader import aday_paylastir
from hibrit_trader.momentum_session import (
    SCAN_INTERVAL_SEC,
    _data_dir,
    _mom_slippage,
    sol_chg_h1,
    yas_str,
)
from hibrit_trader.paper import _now_iso, new_trade_id
from hibrit_trader.price_sanity import guard_price
from hibrit_trader.safety import check_token
from hibrit_trader.scanner import scan_all_cached as scan_all

log = logging.getLogger(__name__)

# ---- V7HT esikleri (kullanici kural seti 2026-07-15) --------------------
CHG_H1_MIN = float(os.getenv("V7HT_CHG_H1_MIN", "5"))
CHG_H1_MAX = float(os.getenv("V7HT_CHG_H1_MAX", "45"))
LIQ_MIN_USD = float(os.getenv("V7HT_LIQ_MIN_USD", "100000"))
MAX_SLOTS = 5
START_BALANCE = float(os.getenv("V7HT_START_BALANCE", "1000"))
TP_PCT = float(os.getenv("V7HT_TP_PCT", "2.0"))
SOL_H1_MIN = float(os.getenv("V7HT_SOL_H1_MIN", "0.35"))
# A/B tavanlari (R1 kalibrasyonuyla ayni): pompa/parabolik giris reddi
H1_MAX = float(os.getenv("V7HT_H1_MAX", "150"))
M5_MAX = float(os.getenv("V7HT_M5_MAX", "75"))
# Cuval tasfiyesi: 21 Tem kullanici karari 30dk (ilk kalibrasyon 360 idi).
# 0 = devre disi.
TIMEOUT_MIN = float(os.getenv("V7HT_TIMEOUT_MIN", "30"))
DAILY_LOSS_LIMIT_USD = float(os.getenv("MOM_DAILY_LOSS_LIMIT_USD", "0"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("MOM_DAILY_LOSS_LIMIT_PCT", "25"))
COOLDOWN_LOSS_SEC = float(os.getenv("MOM_COOLDOWN_STOP_MIN", "60")) * 60
COOLDOWN_EXIT_SEC = float(os.getenv("MOM_COOLDOWN_EXIT_MIN", "15")) * 60

# 2s hizli cikis kadansi (v6/v7d ile ayni)
EXIT_INTERVAL_SEC = float(os.getenv("M_EXIT_INTERVAL_SEC", "2"))
# Satis slippage tablosu (kullanici karari): normal 150 / stop_felaket 1000
# stop_gec 300 tutuluyor, motor tetiklemiyor ama tablo tutarli olsun.
EXIT_SLIPPAGE_BPS = {"tp_2": 150, "stop_gec": 300, "stop_felaket": 1000,
                     "timeout_cuval": 300}
STOP_RETRY_ADET = 3
STOP_RETRY_SEC = 3.0
SAT_COOLDOWN_SEC = 20.0
KOR_FIYAT_SEC = 120.0
KOR_ALARM_ARALIK_SEC = 60.0

STATE_FILE = "v7ht_state.json"
TRADES_FILE = "v7ht_trades.jsonl"


class V7HTEngine:
    """TP=+%2 tek cikis paper motoru. Kendi dosyalari, sifir dokunus."""

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
        self._day_limit_usd: float | None = None
        self._limit_belirsiz_logged = False
        self._yuklenen_gun_limiti: tuple | None = None
        self._huni = HuniSayac("V7HT")
        self._lock_fh = None
        self._son_exec_neden: str | None = None
        self._belirsiz_aday: dict | None = None
        # 16 Tem: CANLI_MOTOR env swap altyapisi. Default paper; CANLI_MOTOR=v7ht
        # secilirse make_exec_broker (live/dryrun) devreye girer.
        self._exec, self._exec_arizali = init_motor_exec("v7ht")
        self._load()
        self._restore_day_realized()
        if (self._yuklenen_gun_limiti
                and self._yuklenen_gun_limiti[0] == self._day_key
                and self._yuklenen_gun_limiti[1]):
            self._day_limit_usd = float(self._yuklenen_gun_limiti[1])

    # ---- Dosya isleri ---------------------------------------------------------
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
            self._yuklenen_gun_limiti = (data.get("day_limit_key"),
                                         data.get("day_limit_usd"))
        except Exception:
            backup = p.with_name(f"{p.name}.corrupt-{int(time.time())}")
            try:
                p.rename(backup)
                log.critical("v7ht state bozuk, yedege tasindi: %s", backup)
            except OSError:
                log.critical("v7ht state bozuk ve yedeklenemedi, temiz baslaniyor")

    def _save(self) -> None:
        p = self._path(STATE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "balance": round(self.balance, 4),
            "start_balance": round(self.start_balance, 2),
            "realized_pnl": round(self.realized_pnl, 4),
            "created_ts": round(self.created_ts, 3),
            "positions": self.positions,
            "day_limit_key": self._day_key or None,
            "day_limit_usd": (round(self._day_limit_usd, 4)
                              if self._day_limit_usd is not None else None),
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

    # ---- Gun ici realized PnL sayaci -----------------------------------------
    def _day_realized_add(self, pnl: float, now: float) -> None:
        key = time.strftime("%Y-%m-%d", time.gmtime(now))
        if key != self._day_key:
            self._day_key = key
            self._day_realized = 0.0
            self._limit_logged = False
            self._day_limit_usd = None
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
            log.debug("V7HT gun ici pnl geri yuklenemedi", exc_info=True)

    def _entries_blocked(self) -> str | None:
        if self._exec_arizali:
            return "exec_arizali"
        if kill_is_active():
            if not self._kill_logged:
                self._kill_logged = True
                log.critical("V7HT: kill-switch AKTIF, yeni girisler durdu (cikislar suruyor)")
            return "kill_switch"
        if self._kill_logged:
            self._kill_logged = False
            log.warning("V7HT: kill-switch kalkti, girisler serbest")
        if DAILY_LOSS_LIMIT_USD > 0 or self._pct_limit_aktif():
            key = time.strftime("%Y-%m-%d", time.gmtime())
            if key != self._day_key:
                self._day_key = key
                self._day_realized = 0.0
                self._limit_logged = False
                self._day_limit_usd = None
            limit, kesin = self._gun_limiti()
            if limit is None and not kesin:
                if not self._limit_belirsiz_logged:
                    self._limit_belirsiz_logged = True
                    log.critical("V7HT: gun limiti hesaplanamadi, yeni giris kapali (fail-closed)")
                return "daily_limit_belirsiz"
            if self._limit_belirsiz_logged:
                self._limit_belirsiz_logged = False
                log.warning("V7HT: gun limiti hesaplandi, belirsizlik kalkti")
            if limit is not None and self._day_realized <= -limit:
                if not self._limit_logged:
                    self._limit_logged = True
                    log.critical(
                        "V7HT: gunluk zarar limiti asildi ($%.2f <= -$%.2f), "
                        "bugun yeni giris yok", self._day_realized, limit,
                    )
                return "daily_loss_limit"
        return None

    def _pct_limit_aktif(self) -> bool:
        return DAILY_LOSS_LIMIT_PCT > 0 and getattr(self._exec, "mode", "paper") == "live"

    def _canli_mtm(self) -> float | None:
        try:
            from hibrit_trader import canli_gosterge
            snap = canli_gosterge.son()
            if snap and float(snap.get("mtm") or 0.0) > 0:
                return float(snap["mtm"])
        except Exception:
            log.debug("V7HT canli MTM okunamadi", exc_info=True)
        return None

    def _gun_limiti(self) -> tuple[float | None, bool]:
        if self._day_limit_usd is not None:
            return self._day_limit_usd, True
        usd = DAILY_LOSS_LIMIT_USD if DAILY_LOSS_LIMIT_USD > 0 else None
        if not self._pct_limit_aktif():
            self._day_limit_usd = usd
            return usd, True
        mtm = self._canli_mtm()
        if mtm is None:
            return usd, False
        limit = mtm * DAILY_LOSS_LIMIT_PCT / 100.0
        if usd is not None:
            limit = min(limit, usd)
        self._day_limit_usd = limit
        self._save()
        log.warning("V7HT gun limiti sabitlendi: MTM $%.2f x %%%g = $%.2f",
                    mtm, DAILY_LOSS_LIMIT_PCT, limit)
        return limit, True

    def _exec_fill(self, yon: str, token_address: str, *, usd: float = 0.0,
                   amount_token: float = 0.0, ref_fiyat: float = 0.0,
                   slippage_bps: int = 50, acilis_ts: float | None = None):
        self._son_exec_neden = None
        try:
            fill = self._exec.execute(ExecOrder(
                engine="V7", yon=yon, token_address=token_address,
                usd=usd, amount_token=amount_token, ref_fiyat=ref_fiyat,
                slippage_bps=slippage_bps, acilis_ts=acilis_ts))
        except Exception as e:
            log.error("V7HT yurutme hatasi (%s %s): %s", yon, token_address[:8], e)
            fill = None
        if self._exec.mode != "live":
            return True, None
        if fill is None or not fill.ok:
            self._son_exec_neden = fill.neden if fill is not None else "exec_hata"
            return False, None
        return True, fill

    def _acquire_lock(self) -> bool:
        import fcntl
        p = self._path("v7ht_engine.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("V7HT: baska bir instance calisiyor, motor baslatilmiyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    # ---- Ana dongu -----------------------------------------------------------
    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        log.warning(
            "V7HT paper basladi (TP=+%%%.1f tek cikis, stop yok) - sanal $%.2f · "
            "slot %d · giris liq>=$%.0f + h1 %.0f..%.0f · rejim>=%.2f",
            TP_PCT, self.balance, MAX_SLOTS, LIQ_MIN_USD, CHG_H1_MIN, CHG_H1_MAX, SOL_H1_MIN,
        )
        self._save()
        feed = get_feed()
        if feed is not None:
            for pos in self.positions:
                feed.add_pool(pos["pool_address"])
        # v7d 7/8 kullaniyor, v7 1/1, momentum 5/8, v6 farkli. v7ht faz: 6/8.
        time.sleep(SCAN_INTERVAL_SEC * 3 / 8)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("v7ht tick hatasi")
            deadline = time.time() + SCAN_INTERVAL_SEC
            while True:
                kalan = deadline - time.time()
                if kalan <= 0:
                    break
                time.sleep(min(EXIT_INTERVAL_SEC, kalan))
                try:
                    self.fast_exit_tick()
                except Exception:
                    log.exception("v7ht hizli cikis hatasi")

    def tick(self) -> None:
        self._belirsiz_takip()
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
        self._save()

    # ---- R2-alim: belirsiz alim mutabakati -----------------------------------
    def _belirsiz_takip(self) -> None:
        if self._belirsiz_aday is None:
            return
        sorgu = getattr(self._exec, "belirsiz_sonuc", None)
        if sorgu is None:
            self._belirsiz_aday = None
            return
        durum, detay = sorgu("V7")
        if durum == "bekliyor":
            return
        aday = self._belirsiz_aday
        self._belirsiz_aday = None
        if durum == "gerceklesti" and detay and detay.get("fiyat", 0) > 0:
            self._belirsiz_pozisyon_ac(aday, detay)
        elif durum == "yok":
            log.warning("V7HT BELIRSIZ SONUC %s: tx zincirde yok, iptal", aday["pair"])
        else:
            log.critical("V7HT BELIRSIZ SONUC %s: cozulemedi (%s)", aday["pair"], durum)

    def _belirsiz_pozisyon_ac(self, aday: dict, detay: dict) -> None:
        usd = aday["usd"]
        entry = detay["fiyat"]
        gas = GAS_COST_USD.get(aday["chain"], 0.1)
        now = aday["ts"]
        pos = {
            "trade_id": new_trade_id(aday["pool_address"], now),
            "pair": aday["pair"],
            "chain": aday["chain"],
            "token_address": aday["token_address"],
            "pool_address": aday["pool_address"],
            "entry_price": entry,
            "karar_fiyat": aday["karar_fiyat"],
            "amount_token": usd / entry,
            "cost_usd": round(usd, 4),
            "opened_ts": now,
            "opened_at": _now_iso(),
            "chg_m5": aday["chg_m5"],
            "chg_h1": aday["chg_h1"],
            "liq_entry": aday["liq_entry"],
            "sol_chg_h1": aday["sol_chg_h1"],
            "entry_price_source": aday["entry_price_source"],
            "entry_fresh_fark_pct": aday["entry_fresh_fark_pct"],
            "entry_slip_pct": aday["entry_slip_pct"],
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "last_price": entry,
            "tx_al": detay["tx_id"],
            "canli_miktar": detay["miktar_token"],
            "belirsiz_mutabakat": True,
        }
        self.balance -= (usd + gas)
        self.positions.append(pos)
        self._save()
        feed = get_feed()
        if feed is not None:
            feed.add_pool(pos["pool_address"])
        log.warning("V7HT BUY (mutabakat) %s $%.2f @ %.8g", aday["pair"], usd, entry)

    def _sol_chg_h1(self, client: httpx.Client) -> float | None:
        return sol_chg_h1(client)

    # ---- Giris ---------------------------------------------------------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        if self._entries_blocked():
            return
        try:
            pairs = scan_all(self.settings.scan_chains)
        except Exception as e:
            log.warning("V7HT giris tick atlandi, tarama hatasi: %r", e)
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
            # Aday paylastir: baska motor 15dk icinde ayni token'i aldi mi?
            _izin, _red_nedeni = aday_paylastir.iddia_et(pr.token_address, "v7ht", pr.name)
            if not _izin:
                continue
            if pr.liquidity_usd < LIQ_MIN_USD:
                continue
            liq_ok += 1
            h1 = getattr(pr, "chg_h1", 0.0)
            if not (CHG_H1_MIN <= h1 <= CHG_H1_MAX):
                continue
            if H1_MAX > 0 and h1 > H1_MAX:
                safety_reject_kaydet(pr, "V7HT", "h1_tavan_skip",
                                     "h1 %.1f > tavan %.0f" % (h1, H1_MAX))
                continue
            m5t = getattr(pr, "chg_m5", 0) or 0
            if M5_MAX > 0 and m5t > M5_MAX:
                safety_reject_kaydet(pr, "V7HT", "m5_tavan_skip",
                                     "m5 %.1f > tavan %.0f" % (m5t, M5_MAX))
                continue
            cands.append(pr)
        cands.sort(key=lambda pr: pr.chg_h1, reverse=True)
        self._huni.ekle(len(pairs), liq_ok, len(cands), now)
        if not cands:
            return
        # 18 Tem C-plani: rejim gate DEVRE DISI (test). sol_h1 sadece log icin.
        try:
            sol_h1 = self._sol_chg_h1(client)
        except Exception:
            sol_h1 = None
        budget_each = self.balance / empty
        for pair in cands:
            if empty <= 0 or budget_each < 1.0:
                break
            try:
                report = check_token(client, pair.chain, pair.token_address)
            except Exception as e:
                safety_reject_kaydet(pair, "V7HT", "safety_hata", type(e).__name__)
                continue
            time.sleep(0.2 if self._aggressive else 1.5)
            if not report.ok:
                safety_reject_kaydet(
                    pair, "V7HT", report.kapi or "safety_red",
                    "; ".join(report.reasons[:2])
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
        taze = taze_teyit(pair, "V7HT", client)
        if taze.iptal:
            log.warning("V7HT GIRIS IPTAL %s: taze fiyat taramanin %%%.2f ustunde (kaynak %s)",
                        pair.name, taze.fark_pct, taze.kaynak)
            return False
        slip = _mom_slippage(usd, pair.liquidity_usd)
        eff_price = taze.fiyat * (1 + slip)
        karar_fiyat = eff_price
        devam, canli = self._exec_fill("al", pair.token_address,
                                       usd=usd, ref_fiyat=eff_price)
        if not devam:
            if self._son_exec_neden == "islem_belirsiz":
                self._belirsiz_aday = {
                    "pair": pair.name, "chain": pair.chain,
                    "token_address": pair.token_address,
                    "pool_address": pair.pool_address,
                    "usd": usd, "karar_fiyat": karar_fiyat,
                    "chg_m5": round(getattr(pair, "chg_m5", 0.0), 2),
                    "chg_h1": round(pair.chg_h1, 2),
                    "liq_entry": round(pair.liquidity_usd, 2),
            "pool_yas_dk": (round((time.time() - float(pair.pool_created_at)) / 60.0, 1)
                            if getattr(pair, "pool_created_at", None) else None),
                    "sol_chg_h1": sol_h1,
                    "entry_price_source": taze.kaynak,
                    "entry_fresh_fark_pct": taze.fark_pct,
                    "entry_slip_pct": round(slip * 100, 4),
                    "ts": time.time(),
                }
                log.critical("V7HT GIRIS BELIRSIZ %s: zincir mutabakati bekleniyor",
                             pair.name)
                return False
            log.error("V7HT GIRIS IPTAL %s: canli alim gerceklesmedi", pair.name)
            kritik_uyari("GIRIS IPTAL", f"giris:v7ht:{pair.name}", f"V7HT {pair.name}: canli alim gerceklesmedi (broker fail)")
            return False
        if canli is not None and canli.fiyat > 0:
            eff_price = canli.fiyat
        amount_token = usd / eff_price
        now = time.time()
        pos = {
            "trade_id": new_trade_id(pair.pool_address, now),
            "pair": pair.name,
            "chain": pair.chain,
            "token_address": pair.token_address,
            "pool_address": pair.pool_address,
            "entry_price": eff_price,
            "karar_fiyat": karar_fiyat,
            "amount_token": amount_token,
            "cost_usd": round(usd, 4),
            "opened_ts": now,
            "opened_at": _now_iso(),
            "chg_m5": round(getattr(pair, "chg_m5", 0.0), 2),
            "chg_h1": round(pair.chg_h1, 2),
            "liq_entry": round(pair.liquidity_usd, 2),
            "pool_yas_dk": (round((time.time() - float(pair.pool_created_at)) / 60.0, 1)
                            if getattr(pair, "pool_created_at", None) else None),
            "sol_chg_h1": sol_h1,
            "entry_price_source": taze.kaynak,
            "entry_fresh_fark_pct": taze.fark_pct,
            "entry_slip_pct": round(slip * 100, 4),
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "last_price": eff_price,
        }
        if canli is not None:
            if canli.tx_id:
                pos["tx_al"] = canli.tx_id
            if canli.miktar_token > 0:
                pos["canli_miktar"] = canli.miktar_token
        self.balance -= (usd + gas)
        self.positions.append(pos)
        self._save()
        feed = get_feed()
        if feed is not None:
            feed.add_pool(pos["pool_address"])
        aday_paylastir.kaydet(pair.token_address, "v7ht", pair.name)
        log.warning("V7HT BUY %s $%.2f @ %.8g (h1 %.1f%%, liq $%.0f, yas %s)",
                    pair.name, usd, eff_price, pair.chg_h1, pair.liquidity_usd, yas_str(pair.pool_created_at))
        notify("[V7HT] ALIM: %s $%.2f @ %.8g (h1 %%%.1f, liq $%.0f)"
               % (pair.name, usd, eff_price, pair.chg_h1, pair.liquidity_usd))
        return True

    # ---- Cikis: SADECE tp_2 (+%2 uzeri). Stop yok, zaman asimi yok. ----------
    def _eval_position(self, pos: dict, price: float, now: float,
                       liquidity_usd: float | None = None) -> str | None:
        price, ariza = guard_price(pos, price, now, "V7HT", liquidity_usd=liquidity_usd)
        if ariza:
            return None
        pos["last_price"] = price
        entry = pos["entry_price"]
        pnl_pct = (price / entry - 1) * 100 if entry > 0 else 0.0
        if pnl_pct > pos["mfe_pct"]:
            pos["mfe_pct"] = round(pnl_pct, 4)
        if pnl_pct < pos["mae_pct"]:
            pos["mae_pct"] = round(pnl_pct, 4)
        if pnl_pct > TP_PCT:
            return "tp_2"
        if TIMEOUT_MIN > 0 and (now - pos["opened_ts"]) >= TIMEOUT_MIN * 60:
            return "timeout_cuval"
        return None

    def _fiyat_tazelendi(self, pos: dict, now: float) -> None:
        pos["_taze_fiyat_ts"] = now
        if pos.pop("kor_fiyat", None):
            pos.pop("_kor_alarm_ts", None)
            log.warning("V7HT kor fiyat sona erdi %s", pos["pair"])

    def _manage_exits(self, client: httpx.Client) -> None:
        now = time.time()
        feed = get_feed()
        for pos in list(self.positions):
            rec = feed.get_price(pos["pool_address"]) if feed is not None else None
            liq = None
            if rec is not None:
                price, sample_ts = rec
                src = "fast"
            else:
                price, liq = fetch_pool_snapshot(client, pos["chain"], pos["pool_address"])
                sample_ts, src = None, "poll"
            if price is None or price <= 0:
                price = pos["last_price"]
                sample_ts, src = None, "poll"
                taze_yas = now - (pos.get("_taze_fiyat_ts") or pos["opened_ts"])
                if taze_yas >= KOR_FIYAT_SEC:
                    pos["kor_fiyat"] = True
                    if now - pos.get("_kor_alarm_ts", 0.0) >= KOR_ALARM_ARALIK_SEC:
                        pos["_kor_alarm_ts"] = now
                        log.critical(
                            "V7HT KOR FIYAT %s: %.0fs'dir taze fiyat yok "
                            "(TP tetiklenemeyebilir)", pos["pair"], taze_yas)
            else:
                self._fiyat_tazelendi(pos, now)
            reason = self._eval_position(pos, price, now, liquidity_usd=liq)
            if reason:
                pos["_price_src"] = src
                pos["_price_ts"] = sample_ts
                self._close_position(pos, price, reason, now)

    def fast_exit_tick(self) -> None:
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
            self._fiyat_tazelendi(pos, now)
            reason = self._eval_position(pos, price, now)
            if reason:
                pos["_price_src"] = "fast"
                pos["_price_ts"] = sample_ts
                self._close_position(pos, price, reason, now)

    def _close_position(self, pos: dict, price: float, reason: str, now: float) -> None:
        if time.time() < pos.get("_sat_bekle_ts", 0.0):
            return
        cost = pos["cost_usd"]
        slip = _mom_slippage(cost, pos["liq_entry"])
        eff_price = price * (1 - slip)
        karar_cikis = eff_price
        sat_bps = EXIT_SLIPPAGE_BPS.get(reason, 150)
        deneme = STOP_RETRY_ADET if reason in ("stop_gec", "stop_felaket") else 1
        devam, canli = False, None
        for i in range(deneme):
            devam, canli = self._exec_fill("sat", pos["token_address"],
                                           amount_token=pos.get("canli_miktar")
                                           or pos["amount_token"],
                                           ref_fiyat=eff_price,
                                           slippage_bps=sat_bps,
                                           acilis_ts=pos["opened_ts"])
            if devam:
                break
            if i + 1 < deneme:
                log.warning("V7HT SATIS TEKRAR %s (%s): deneme %d/%d basarisiz",
                            pos["pair"], reason, i + 1, deneme)
                time.sleep(STOP_RETRY_SEC)
        if not devam:
            pos["_sat_bekle_ts"] = time.time() + SAT_COOLDOWN_SEC
            log.error("V7HT SATIS ERTELENDI %s: canli satis gerceklesmedi", pos["pair"])
            kritik_uyari("SATIS ERTELENDI", f"sat:v7ht:{pos['pair']}", f"V7HT {pos['pair']}: canli satis fail, retry cooldown")
            return
        if canli is not None and canli.fiyat > 0:
            eff_price = canli.fiyat
        gas = GAS_COST_USD.get(pos["chain"], 0.1)
        proceeds = pos["amount_token"] * eff_price - gas
        pnl = proceeds - cost
        hold_sec = round(now - pos["opened_ts"], 1)
        pnl_pct = (eff_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0.0
        price_src = pos.pop("_price_src", "poll")
        price_ts = pos.pop("_price_ts", None)
        tetik_gecikme = round(now - price_ts, 3) if price_ts else None

        row = {
            "trade_id": pos["trade_id"],
            "pair": pos["pair"],
            "chain": pos["chain"],
            "token_address": pos["token_address"],
            "pool_address": pos["pool_address"],
            "entry_price": pos["entry_price"],
            "exit_price": eff_price,
            "karar_fiyat": pos.get("karar_fiyat"),
            "karar_cikis": karar_cikis,
            "karar_pnl_pct": (round((karar_cikis / pos["karar_fiyat"] - 1) * 100, 3)
                              if pos.get("karar_fiyat") else None),
            "chg_m5": pos["chg_m5"],
            "chg_h1": pos["chg_h1"],
            "liq_entry": pos["liq_entry"],
            "pool_yas_dk": pos.get("pool_yas_dk"),
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
            "price_source": price_src,
            "tetik_gecikme_sec": tetik_gecikme,
            "opened_at": pos["opened_at"],
            "closed_at": _now_iso(),
        }
        if canli is not None and canli.tx_id:
            row["signature"] = canli.tx_id
        if pos.get("tx_al"):
            row["signature_al"] = pos["tx_al"]
        cm = float(pos.get("canli_miktar") or 0.0)
        if cm > 0 and canli is not None and canli.tx_id:
            row["canli_miktar"] = cm
            row["canli_pnl_usd"] = round((eff_price - pos["entry_price"]) * cm, 4)
        self._append_trade(row)
        self.balance += proceeds
        self.realized_pnl += pnl
        self._day_realized_add(pnl, now)
        cd = COOLDOWN_LOSS_SEC if reason in ("stop_gec", "stop_felaket") else COOLDOWN_EXIT_SEC
        if pos.get("token_address"):
            self._cooldown_until[pos["token_address"]] = now + cd
        try:
            self.positions.remove(pos)
        except ValueError:
            pass
        self._save()
        feed = get_feed()
        if feed is not None:
            feed.remove_pool(pos["pool_address"])
        log.warning("V7HT SELL %s pnl $%.2f (%.2f%%) — %s, hold %.0fs (mfe %.1f%% mae %.1f%%)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec, pos["mfe_pct"], pos["mae_pct"])
        notify("[V7HT] SATIM: %s pnl $%.2f (%%%.2f) — %s, hold %.0fdk"
               % (pos["pair"], pnl, pnl_pct, reason, hold_sec / 60))
