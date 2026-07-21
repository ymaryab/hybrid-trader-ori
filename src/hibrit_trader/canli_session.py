"""CANLI motor — 10. slot, gercek cuzdanla emir keser (19 Tem tasarim).

MIMARI (kullanici karari, 19 Tem):
  - 9 paper motor bolunmeden kendi defterlerinde yarismaya devam eder.
  - Bu 10. motor AYRI defter tutar, ayni kaynak motor kural setiyle
    KENDI taramasindan karar verir (drift kabul: bilgi kazanci).
  - Kural degisirse defter aynen kalir, trades.jsonl'a "kural_degisim"
    satiri dusulur (kullanici karari: birikimli tek defter).

Env:
  CANLI_ENABLED=1     — panel startup'ta bu motoru baslatir
  CANLI_KAYNAK_MOTOR  — kural seti hangi paper motordan (default r1)
  CANLI_MOTOR=canli   — broker.init_motor_exec kilidini bu motora verir

Dosyalar:
  data/canli_state.json    (sanal bakiye + acik pozisyonlar)
  data/canli_trades.jsonl  (kapanislar + kural_degisim notlari)
  data/canli_equity.jsonl  (MTM izleme)
  data/canli_engine.lock   (tek instance)

Ilk surum (19 Tem): sadece R1 kural seti desteklenir. Gelecekte
CANLI_KAYNAK_MOTOR degistirilecekse burada kaynak-secim mantigi eklenir.
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
    btc_macro_gate,
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

# ---- Kaynak motor secimi (kural seti) -----------------------------------
KAYNAK_MOTOR = os.getenv("CANLI_KAYNAK_MOTOR", "r1").strip().lower()
# 21 Tem genellestirme: tum aktif paper motorlar canliya alinabilir.
# Giris (_enter) ve cikis (_eval_position) kaynak motorun Engine sinifina
# DELEGE edilir; boylece kural seti kopya degil, birebir ayni kod calisir.
_KAYNAK_KAYITLARI = {
    "r1": ("r1_session", "R1Engine"),
    "r2": ("r2_session", "R2Engine"),
    "v7": ("v7_session", "V7Engine"),
    "v7c": ("v7c_session", "V7CEngine"),
    "v7d": ("v7d_session", "V7DEngine"),
    "v7t": ("v7t_session", "V7TEngine"),
    "v7hizli": ("v7hizli_session", "V7HizliEngine"),
    "v7ht": ("v7ht_session", "V7HTEngine"),
}
DESTEKLENEN_KAYNAKLAR = set(_KAYNAK_KAYITLARI)
if KAYNAK_MOTOR not in DESTEKLENEN_KAYNAKLAR:
    raise RuntimeError(
        f"CANLI_KAYNAK_MOTOR={KAYNAK_MOTOR} destekli degil "
        f"(mevcut: {sorted(DESTEKLENEN_KAYNAKLAR)}). "
        "Yeni kaynak icin canli_session.py'ye ithal ekle."
    )

import importlib as _importlib

_mod_adi, _sinif_adi = _KAYNAK_KAYITLARI[KAYNAK_MOTOR]
_kaynak = _importlib.import_module(f"hibrit_trader.{_mod_adi}")
_KaynakEngine = getattr(_kaynak, _sinif_adi)

CHG_H1_MIN = _kaynak.CHG_H1_MIN
CHG_H1_MAX = _kaynak.CHG_H1_MAX
LIQ_MIN_USD = _kaynak.LIQ_MIN_USD
MAX_SLOTS = _kaynak.MAX_SLOTS
TP_PCT = _kaynak.TP_PCT
SOL_H1_MIN = _kaynak.SOL_H1_MIN
DAILY_LOSS_LIMIT_USD = _kaynak.DAILY_LOSS_LIMIT_USD
DAILY_LOSS_LIMIT_PCT = _kaynak.DAILY_LOSS_LIMIT_PCT
COOLDOWN_LOSS_SEC = _kaynak.COOLDOWN_LOSS_SEC
COOLDOWN_EXIT_SEC = _kaynak.COOLDOWN_EXIT_SEC
EXIT_INTERVAL_SEC = _kaynak.EXIT_INTERVAL_SEC
EXIT_SLIPPAGE_BPS = _kaynak.EXIT_SLIPPAGE_BPS
STOP_RETRY_ADET = _kaynak.STOP_RETRY_ADET
STOP_RETRY_SEC = _kaynak.STOP_RETRY_SEC
SAT_COOLDOWN_SEC = _kaynak.SAT_COOLDOWN_SEC
KOR_FIYAT_SEC = _kaynak.KOR_FIYAT_SEC
KOR_ALARM_ARALIK_SEC = _kaynak.KOR_ALARM_ARALIK_SEC
# R1'e ozgu sabitler: v7hizli kaynakta tanimsiz (None), _eval_position'da
# kaynak dali bunlari kullanmaz.
DISASTER_PCT = getattr(_kaynak, "DISASTER_PCT", None)
GRACE_SEC = getattr(_kaynak, "GRACE_SEC", None)
LATE_STOP_PCT = getattr(_kaynak, "LATE_STOP_PCT", None)
CEILING_SEC = getattr(_kaynak, "CEILING_SEC", None)
M5_MIN = getattr(_kaynak, "M5_MIN", None)
KISMI_ORAN = getattr(_kaynak, "KISMI_ORAN", None)
KISMI_ORAN1 = getattr(_kaynak, "KISMI_ORAN1", None)
KISMI_ORAN2 = getattr(_kaynak, "KISMI_ORAN2", None)
TP2_PCT = getattr(_kaynak, "TP2_PCT", None)
TRAIL_PCT = getattr(_kaynak, "TRAIL_PCT", None)

# CANLI kendi sanal bakiye baslangicini env'den alir (bilet zaten broker tarafinda
# cuzdan MTM'inden hesaplaniyor; buradaki bakiye paper muhasebesi icin).
START_BALANCE = float(os.getenv("CANLI_START_BALANCE", "1000"))

STATE_FILE = "canli_state.json"
TRADES_FILE = "canli_trades.jsonl"
LOCK_FILE = "canli_engine.lock"
TAG = "CANLI"
PAUSE_FILE = "CANLI_DUR"


def canli_pause_aktif() -> bool:
    """Ana salter: data/CANLI_DUR varsa yeni canli giris yok, cikislar surer.
    LIVE_ONAY'dan farki: broker'a dokunmaz, satislar kesintisiz calisir."""
    return (_data_dir() / PAUSE_FILE).exists()


class CanliEngine:
    """10. motor: gercek cuzdanla emir keser, ayri defter tutar. Kural seti
    KAYNAK_MOTOR'dan (default r1) import edilir."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.balance: float = START_BALANCE
        self.start_balance: float = START_BALANCE
        self.realized_pnl: float = 0.0
        self.positions: list[dict] = []
        self.created_ts: float = time.time()
        self.kaynak_motor: str = KAYNAK_MOTOR
        self._aggressive = os.getenv("PAPER_AGGRESSIVE", "0") == "1"
        self._cooldown_until: dict[str, float] = {}
        self._regime_logged = False
        self._kill_logged = False
        self._pause_logged = False
        self._day_key: str = ""
        self._day_realized: float = 0.0
        self._limit_logged = False
        self._day_limit_usd: float | None = None
        self._limit_belirsiz_logged = False
        self._yuklenen_gun_limiti: tuple | None = None
        self._huni = HuniSayac(TAG)
        self._lock_fh = None
        self._son_exec_neden: str | None = None
        self._belirsiz_aday: dict | None = None
        # broker.init_motor_exec("canli") → CANLI_MOTOR=canli ise live broker,
        # aksi halde PaperExec. Diger motorlar bu kilidi almaz.
        self._exec, self._exec_arizali = init_motor_exec("canli")
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
                log.critical("canli state bozuk, yedege tasindi: %s", backup)
            except OSError:
                log.critical("canli state bozuk ve yedeklenemedi, temiz baslaniyor")

    def _save(self) -> None:
        p = self._path(STATE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "balance": round(self.balance, 4),
            "start_balance": round(self.start_balance, 2),
            "realized_pnl": round(self.realized_pnl, 4),
            "created_ts": round(self.created_ts, 3),
            "kaynak_motor": self.kaynak_motor,
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

    # ---- Kural degisim protokolu ---------------------------------------------
    def _son_kural_kaynagi(self) -> str | None:
        """trades.jsonl'daki son kural_degisim satirinin 'yeni' alanini dondurur."""
        p = self._path(TRADES_FILE)
        if not p.exists():
            return None
        try:
            for ln in reversed(p.read_text().splitlines()):
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                    if r.get("type") == "kural_degisim":
                        return r.get("yeni")
                except Exception:
                    continue
        except Exception:
            log.debug("canli kural kaynak okumasi hata", exc_info=True)
        return None

    def _kural_kontrol(self) -> None:
        """Startup'ta env'deki kaynak motor son kayitli ile ayni mi kiyasla.
        Farkliysa trades.jsonl'a kural_degisim satiri yaz (birikimli defter)."""
        son = self._son_kural_kaynagi()
        if son == self.kaynak_motor:
            return
        self._append_trade({
            "type": "kural_degisim",
            "eski": son,
            "yeni": self.kaynak_motor,
            "note": f"Kaynak motor {son or '(ilk_baslangic)'} -> {self.kaynak_motor}",
        })
        log.warning("CANLI kural degisimi: %s -> %s (defter aynen surer)",
                    son or "(ilk_baslangic)", self.kaynak_motor)

    def _hizala_baslangic_bakiyesi(self) -> None:
        """Ilk baslangicta (bos defter) sanal bakiyeyi cuzdan MTM'iyle hizala.
        canli_gosterge snapshot bekler (max ~30s), alinirsa balance=start=mtm.
        Boylece kart cuzdan degeriyle ayni görünür, kar%% cuzdan hareketini yansitir.
        Zaten defter kirliyse (poz/realized/farkli balance) dokunulmaz."""
        if self.positions or self.realized_pnl != 0.0 or self.balance != START_BALANCE:
            return
        for _ in range(10):
            mtm = self._canli_mtm()
            if mtm is not None and mtm > 1.0:
                self.balance = mtm
                self.start_balance = mtm
                self.created_ts = time.time()
                self._save()
                log.warning(
                    "CANLI ilk baslangic: sanal bakiye cuzdan MTM'iyle hizalandi ($%.2f)",
                    mtm)
                return
            time.sleep(3.0)
        log.warning(
            "CANLI ilk baslangic: cuzdan MTM 30s icinde alinamadi, sanal $%.2f kaldi",
            self.balance)

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
                    if t.get("type") == "kural_degisim":
                        continue
                    ts = float(t.get("ts") or 0.0)
                    if time.strftime("%Y-%m-%d", time.gmtime(ts)) == self._day_key:
                        total += float(t.get("pnl_usd") or 0.0)
                except Exception:
                    continue
            self._day_realized = total
        except Exception:
            log.debug("CANLI gun ici pnl geri yuklenemedi", exc_info=True)

    def _entries_blocked(self) -> str | None:
        if self._exec_arizali:
            return "exec_arizali"
        if kill_is_active():
            if not self._kill_logged:
                self._kill_logged = True
                log.critical("CANLI: kill-switch AKTIF, yeni girisler durdu (cikislar suruyor)")
            return "kill_switch"
        if self._kill_logged:
            self._kill_logged = False
            log.warning("CANLI: kill-switch kalkti, girisler serbest")
        if canli_pause_aktif():
            if not self._pause_logged:
                self._pause_logged = True
                log.critical("CANLI: ana salter KAPALI (CANLI_DUR), yeni girisler "
                             "durdu (cikislar suruyor)")
            return "canli_pause"
        if self._pause_logged:
            self._pause_logged = False
            log.warning("CANLI: ana salter ACIK, girisler serbest")
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
                    log.critical("CANLI: gun limiti hesaplanamadi, yeni giris kapali (fail-closed)")
                return "daily_limit_belirsiz"
            if self._limit_belirsiz_logged:
                self._limit_belirsiz_logged = False
                log.warning("CANLI: gun limiti hesaplandi, belirsizlik kalkti")
            if limit is not None and self._day_realized <= -limit:
                if not self._limit_logged:
                    self._limit_logged = True
                    log.critical(
                        "CANLI: gunluk zarar limiti asildi ($%.2f <= -$%.2f), "
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
            log.debug("CANLI MTM okunamadi", exc_info=True)
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
        log.warning("CANLI gun limiti sabitlendi: MTM $%.2f x %%%g = $%.2f",
                    mtm, DAILY_LOSS_LIMIT_PCT, limit)
        return limit, True

    def _exec_fill(self, yon: str, token_address: str, *, usd: float = 0.0,
                   amount_token: float = 0.0, ref_fiyat: float = 0.0,
                   slippage_bps: int = 50, acilis_ts: float | None = None):
        self._son_exec_neden = None
        try:
            fill = self._exec.execute(ExecOrder(
                engine="CANLI", yon=yon, token_address=token_address,
                usd=usd, amount_token=amount_token, ref_fiyat=ref_fiyat,
                slippage_bps=slippage_bps, acilis_ts=acilis_ts))
        except Exception as e:
            log.error("CANLI yurutme hatasi (%s %s): %s", yon, token_address[:8], e)
            fill = None
        if self._exec.mode != "live":
            return True, None
        if fill is None or not fill.ok:
            self._son_exec_neden = fill.neden if fill is not None else "exec_hata"
            return False, None
        return True, fill

    def _acquire_lock(self) -> bool:
        import fcntl
        p = self._path(LOCK_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("CANLI: baska bir instance calisiyor, motor baslatilmiyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    # ---- Ana dongu -----------------------------------------------------------
    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        self._kural_kontrol()
        self._hizala_baslangic_bakiyesi()
        mod = getattr(self._exec, "mode", "paper")
        log.warning(
            "CANLI motor basladi (kaynak=%s, broker=%s) - sanal $%.2f · "
            "slot %d · giris liq>=$%.0f + h1 %.0f..%.0f · rejim>=%.2f",
            self.kaynak_motor, mod, self.balance, MAX_SLOTS,
            LIQ_MIN_USD, CHG_H1_MIN, CHG_H1_MAX, SOL_H1_MIN,
        )
        self._save()
        feed = get_feed()
        if feed is not None:
            for pos in self.positions:
                feed.add_pool(pos["pool_address"])
        # Faz: 8/8 (en son; diger 8 paper motor once tarasin)
        time.sleep(SCAN_INTERVAL_SEC * 7 / 8)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("CANLI tick hatasi")
            deadline = time.time() + SCAN_INTERVAL_SEC
            while True:
                kalan = deadline - time.time()
                if kalan <= 0:
                    break
                time.sleep(min(EXIT_INTERVAL_SEC, kalan))
                try:
                    self.fast_exit_tick()
                except Exception:
                    log.exception("CANLI hizli cikis hatasi")

    def tick(self) -> None:
        self._belirsiz_takip()
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
        self._save()

    # ---- Belirsiz alim mutabakati --------------------------------------------
    def _belirsiz_takip(self) -> None:
        if self._belirsiz_aday is None:
            return
        sorgu = getattr(self._exec, "belirsiz_sonuc", None)
        if sorgu is None:
            self._belirsiz_aday = None
            return
        durum, detay = sorgu("CANLI")
        if durum == "bekliyor":
            return
        aday = self._belirsiz_aday
        self._belirsiz_aday = None
        if durum == "gerceklesti" and detay and detay.get("fiyat", 0) > 0:
            self._belirsiz_pozisyon_ac(aday, detay)
        elif durum == "yok":
            log.warning("CANLI BELIRSIZ SONUC %s: tx zincirde yok, iptal", aday["pair"])
        else:
            log.critical("CANLI BELIRSIZ SONUC %s: cozulemedi (%s)", aday["pair"], durum)

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
        log.warning("CANLI BUY (mutabakat) %s $%.2f @ %.8g", aday["pair"], usd, entry)

    def _sol_chg_h1(self, client: httpx.Client) -> float | None:
        return sol_chg_h1(client)

    # ---- Giris: kaynak motorun _enter'ina DELEGE (21 Tem) --------------------
    # Kaynak kendi giris filtrelerini (band, tavanlar, yas, m5...) kendi modul
    # sabitleriyle uygular; pozisyon acilisi self._open_position uzerinden
    # CANLI'nin canli-emirli yoluna gelir. aday_paylastir iddia etiketi kaynak
    # motor id'siyle gider (PAYLASTIR kapali, kozmetik).
    def _enter(self, client: httpx.Client) -> None:
        return _KaynakEngine._enter(self, client)

    def _open_position(self, pair, usd: float, sol_h1: float | None = None,
                       client: httpx.Client | None = None) -> bool:
        gas = GAS_COST_USD.get(pair.chain, 0.1)
        if self.balance < usd + gas:
            return False
        taze = taze_teyit(pair, TAG, client)
        if taze.iptal:
            log.warning("CANLI GIRIS IPTAL %s: taze fiyat taramanin %%%.2f ustunde (kaynak %s)",
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
                log.critical("CANLI GIRIS BELIRSIZ %s: zincir mutabakati bekleniyor",
                             pair.name)
                return False
            log.error("CANLI GIRIS IPTAL %s: canli alim gerceklesmedi", pair.name)
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
            "kaynak_motor": self.kaynak_motor,
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
        aday_paylastir.kaydet(pair.token_address, "canli", pair.name)
        log.warning("CANLI BUY %s $%.2f @ %.8g (h1 %.1f%%, liq $%.0f, yas %s)",
                    pair.name, usd, eff_price, pair.chg_h1, pair.liquidity_usd,
                    yas_str(pair.pool_created_at))
        notify("[CANLI] ALIM: %s $%.2f @ %.8g (h1 %%%.1f, liq $%.0f)"
               % (pair.name, usd, eff_price, pair.chg_h1, pair.liquidity_usd))
        return True

    # ---- Cikis karari: kaynak motorun _eval_position'ina DELEGE (21 Tem) ----
    def _eval_position(self, pos: dict, price: float, now: float,
                       liquidity_usd: float | None = None) -> str | None:
        return _KaynakEngine._eval_position(self, pos, price, now,
                                            liquidity_usd=liquidity_usd)

    def _fiyat_tazelendi(self, pos: dict, now: float) -> None:
        pos["_taze_fiyat_ts"] = now
        if pos.pop("kor_fiyat", None):
            pos.pop("_kor_alarm_ts", None)
            log.warning("CANLI kor fiyat sona erdi %s", pos["pair"])

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
                            "CANLI KOR FIYAT %s: %.0fs'dir taze fiyat yok "
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
        if (float(pos.get("canli_miktar") or 0.0) > 0
                and getattr(self._exec, "mode", "paper") != "live"):
            # LIVE_ONAY dusmus / broker fallback: gercek token satilamaz.
            # Sanal kapanis defteri zincirden koparir (20 Tem HBULL vakasi);
            # pozisyon bekletilir, kilit acilip restart edilince gercek satis.
            pos["_sat_bekle_ts"] = time.time() + SAT_COOLDOWN_SEC
            log.critical("CANLI SATIS ERTELENDI %s (%s): canli pozisyon ama "
                         "broker live degil (%s), sanal kapanis YAPILMADI; "
                         "LIVE_ONAY + restart gerekir", pos["pair"], reason,
                         getattr(self._exec, "mode", "?"))
            kritik_uyari("SATIS ERTELENDI", f"sat:canli:{pos['pair']}",
                         "CANLI %s: broker live degil, gercek satis yapilamiyor"
                         % pos["pair"])
            return
        # Kismi satis haritasi kaynak motorun sabitlerinden (21 Tem genel):
        # r1 uc-asama (1/3+1/3+runner), r2 kar kilidi (1/4); digerleri tam satis.
        _oran_haritasi = {
            "tp_partial_1": KISMI_ORAN1,
            "tp_partial_2": KISMI_ORAN2,
            "tp_runner_partial": KISMI_ORAN,
        }
        _oran_haritasi.update(getattr(_kaynak, "KISMI_ORAN_HARITASI", {}))
        satilan_oran = _oran_haritasi.get(reason) or 1.0
        kismi = reason in _oran_haritasi and _oran_haritasi[reason] is not None
        cost = pos["cost_usd"]
        satilan_amount = pos["amount_token"] * satilan_oran
        satilan_cost = cost * satilan_oran
        slip = _mom_slippage(satilan_cost, pos["liq_entry"])
        eff_price = price * (1 - slip)
        karar_cikis = eff_price
        sat_bps = EXIT_SLIPPAGE_BPS.get(reason, 150)
        deneme = STOP_RETRY_ADET if reason in ("stop_gec", "stop_felaket") else 1
        canli_miktar_satilacak = (float(pos.get("canli_miktar") or 0.0) * satilan_oran
                                  if pos.get("canli_miktar") else satilan_amount)
        devam, canli = False, None
        for i in range(deneme):
            devam, canli = self._exec_fill("sat", pos["token_address"],
                                           amount_token=canli_miktar_satilacak,
                                           ref_fiyat=eff_price,
                                           slippage_bps=sat_bps,
                                           acilis_ts=pos["opened_ts"])
            if devam:
                break
            if i + 1 < deneme:
                log.warning("CANLI SATIS TEKRAR %s (%s): deneme %d/%d basarisiz",
                            pos["pair"], reason, i + 1, deneme)
                time.sleep(STOP_RETRY_SEC)
        if not devam:
            pos["_sat_bekle_ts"] = time.time() + SAT_COOLDOWN_SEC
            log.error("CANLI SATIS ERTELENDI %s: canli satis gerceklesmedi", pos["pair"])
            return
        if canli is not None and canli.fiyat > 0:
            eff_price = canli.fiyat
        gas = GAS_COST_USD.get(pos["chain"], 0.1)
        proceeds = satilan_amount * eff_price - gas
        pnl = proceeds - satilan_cost
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
            "kaynak_motor": pos.get("kaynak_motor") or self.kaynak_motor,
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
        if kismi:
            pos["amount_token"] -= satilan_amount
            pos["cost_usd"] = round(cost - satilan_cost, 4)
            if pos.get("canli_miktar"):
                pos["canli_miktar"] -= canli_miktar_satilacak
            if reason == "tp_partial_1":
                pos["kismi_asama"] = 1
                self._save()
                log.warning("CANLI PARTIAL-1/3 %s: TP+%%%.0f, 1/3 satildi pnl $%.2f (%.2f%%), "
                            "%.0f%% i +%.0f%% bekliyor, kalan 1/3 runner",
                            pos["pair"], TP_PCT, pnl, pnl_pct, 100/3, TP2_PCT)
                notify("[CANLI] PARTIAL-1/3: %s +$%.2f (%%%.2f) — 1/3 sirada +%%%.0f"
                       % (pos["pair"], pnl, pnl_pct, TP2_PCT))
            elif reason in ("tp_partial_2", "tp_runner_partial"):
                pos["kismi_asama"] = 2
                pos["runner_mode"] = True
                pos["runner_peak"] = float(pos.get("last_price") or eff_price)
                self._save()
                log.warning("CANLI PARTIAL-2/3 %s: 1/3 daha satildi pnl $%.2f (%.2f%%), "
                            "kalan 1/3 runner (peak=%.8g, trail=%.1f%%)",
                            pos["pair"], pnl, pnl_pct, pos["runner_peak"], TRAIL_PCT)
                notify("[CANLI] PARTIAL-2/3: %s +$%.2f (%%%.2f) — kalan 1/3 runner (peak %.8g)"
                       % (pos["pair"], pnl, pnl_pct, pos["runner_peak"]))
            elif reason in ("tp_kilit_25", "tp_kilit_40"):
                # R2 kaynak: iki asamali kar kilidi, kalan runner trail ile devam
                pos["kilit_asama"] = 1 if reason == "tp_kilit_25" else 2
                pos["kilit_alindi"] = True
                self._save()
                log.warning("CANLI KAR KILIDI %s %s: 1/4 satildi pnl $%.2f (%.2f%%)",
                            reason[-2:], pos["pair"], pnl, pnl_pct)
                notify("[CANLI] KAR KILIDI +%s: %s +$%.2f (%%%.2f)"
                       % (reason[-2:], pos["pair"], pnl, pnl_pct))
            return
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
        log.warning("CANLI SELL %s pnl $%.2f (%.2f%%) — %s, hold %.0fs (mfe %.1f%% mae %.1f%%)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec, pos["mfe_pct"], pos["mae_pct"])
        notify("[CANLI] SATIM: %s pnl $%.2f (%%%.2f) — %s, hold %.0fdk"
               % (pos["pair"], pnl, pnl_pct, reason, hold_sec / 60))
