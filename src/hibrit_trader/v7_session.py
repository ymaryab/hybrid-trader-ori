"""V7 senaryo motoru — v6 + -%10 felaket freni, yedinci paralel sanal yarışçı.

Diğer motorlara (v2/v3/v4/v5/v6/gölge) SIFIR dokunuş. Sadece şu dosyalara yazar:
  data/v7_state.json   (sanal bakiye + açık pozisyonlar)
  data/v7_trades.jsonl (her sanal kapanışta kayıt)

V7 = V6'NIN BİREBİR KOPYASI + TEK fark (2026-07-04 v6 arındırma analizi):
  FREN  : pozisyon HERHANGİ bir anda -%10'a ulaşırsa sabır iptal, anında
          sat (stop_felaket, 60dk cooldown). Dayanak: v6'nın bozucuları
          giriş tarafında desensiz, sızıntı sabır penceresinin sınırsız
          derinliğinde (BABYANSEM -35.8, Pauly -21.5); -%10 eşiği gölgenin
          en derin kurtarması olan -8.67'nin altında kalır, kurtarma
          bölgesine basmaz. v6 retrosu: -%10 freni +$28, sıfır ters dönen tp.
  GİRİŞ : liq >= $100k VE 10 <= chg_h1 <= 50; 13 Tem cift ayar: h1 20-40
          bandi atlanir (V6+V7 retrosu n17 -$85), 10-20 ve 40-50 gecerli.
          Atlanan aday h1_bant_skip etiketiyle rejects'e yazilir.
  ÇIKIŞ : tp_2 / stop_felaket (-%10) / stop_gec (30dk sabır sonrası -%2) /
          timeout_60. sol_chg_h1 kaydı v6 ile aynı.
  REJİM : V-serisi final (05 Tem): eşik 0 yerine 0.5; 13 Tem cift ayar:
          esik 0.35'e indi (bos zaman bolusumu: bos vaktin %92-94'u rejim
          kapali kaynakli). Env: V7_SOL_H1_MIN.
  GİRİŞ TAZE-FİYAT TEYİDİ (09 Tem gece): alım kaydedilmeden hemen önce fiyat
          tazelenir (fast<=3s -> tek fetch -> tarama, fail-open). Taze fiyat
          taramanın +%2'den fazla üstündeyse giriş iptal (taze_fiyat_kacti).
          Kayıt: entry_price_source + entry_fresh_fark_pct.
  HIZLI GÖZ (12 Tem, canlı asimetri B2): çıkış kontrolü fast_price feed'inden
          2s kadansla (v6 deseni). Pozisyon açılınca havuz feed'e dinamik
          eklenir, kapanınca çıkar. Fren canlı parada 30s bekleyemez.
          Ölçüm: trade kaydında price_source (fast/poll) + tetik_gecikme_sec.
  KADEMELİ SATIŞ TOLERANSI (12 Tem, canlı asimetri B1): canlı satışta
          slippage nedene göre: normal 150 / stop_gec 300 / stop_felaket
          1000 bps; zarar durdurmada kesinlik dolgu kalitesinden önceliklidir.
          Stop yolunda başarısız satış kadans beklemeden 3x3s tekrar denenir.
          Alım 50 bps'te kalır (başarısız alım güvenli taraftır).

Fill'ler sanal: gerçek fiyat + v2'nin likidite-slippage modeli + gas.
Kadans v2 ile aynı; 3/8 interval faz kaydırmalı (v6:5/8, gölge:1/2,
v7:3/8, v4:3/4; 12 Tem: v7 canlı para taşıdığı için v6 ile faz takası
yapıldı, v7 erken v6 geç). V7_ENABLED=0 ile kapatılır.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from hibrit_trader.broker import ExecOrder, PaperExecBroker, make_exec_broker
from hibrit_trader.config import GAS_COST_USD
from hibrit_trader.entry_fresh import (
    HuniSayac,
    bant_reject_kaydet,
    rejim_reject_kaydet,
    safety_reject_kaydet,
    taze_teyit,
)
from hibrit_trader.fast_price import get_feed
from hibrit_trader.killswitch import is_active as kill_is_active
from hibrit_trader.live_sim import fetch_pool_snapshot
from hibrit_trader.momentum_session import (
    SCAN_INTERVAL_SEC,
    _data_dir,
    _mom_slippage,
    sol_chg_h1,
    sol_h1_son_olcum,
)
from hibrit_trader.paper import _now_iso, new_trade_id
from hibrit_trader.price_sanity import guard_price
from hibrit_trader.safety import check_token
from hibrit_trader.scanner import scan_all_cached as scan_all

log = logging.getLogger(__name__)

# ---- V7 eşikleri (v6 ile aynı zemin + felaket freni) --------------------------
CHG_H1_MIN = float(os.getenv("V7_CHG_H1_MIN", "10"))
CHG_H1_MAX = float(os.getenv("V7_CHG_H1_MAX", "50"))   # v6 ile aynı bant
LIQ_MIN_USD = float(os.getenv("V7_LIQ_MIN_USD", "100000"))
MAX_SLOTS = 5
START_BALANCE = float(os.getenv("V7_START_BALANCE", "1000"))
TP_PCT = 2.0            # giriş +%2 görülünce kâr al (gölge ile aynı)
GRACE_SEC = 30 * 60     # ilk 30dk aşağıda stop yok (sabır)
LATE_STOP_PCT = -2.0    # 30dk sonrası: girişin -%2 altı SAT
DISASTER_PCT = float(os.getenv("V7_DISASTER_PCT", "-10"))  # TEK fark: her an mutlak taban
CEILING_SEC = 60 * 60   # 60dk tavan
SOL_H1_MIN = float(os.getenv("V7_SOL_H1_MIN", "0.35"))  # 13 Tem cift ayar: 0.5 -> 0.35 (bos zaman bolusumu olcumu)
# h1 bant kacinma (13 Tem cift ayar): 20-40 bandi V6+V7 retrosunda negatif
# (n17 -$85); 10-20 ve 40-50 gecerli kalir. LO=HI yapilirsa kacinma kapanir.
H1_SKIP_LO = float(os.getenv("V7_H1_SKIP_LO", "20"))
H1_SKIP_HI = float(os.getenv("V7_H1_SKIP_HI", "40"))
# A2 (14 Tem, A4 bulgusu): bant ici m5 > 0 adaylarin atlanmasi +15..+98 kosulari
# kaciriyordu; m5 <= 0 atlamalari cogunlukla dogru korumaydi. Kosullu skip:
# bant ici aday yalniz m5 <= 0 ise atlanir. "0" ile eski kosulsuz davranis.
H1_SKIP_M5_KOSUL = os.getenv("V7_H1_SKIP_M5_KOSUL", "1").strip() != "0"
BANT_SKIP_DEDUP_SEC = 30 * 60
DAILY_LOSS_LIMIT_USD = float(os.getenv("MOM_DAILY_LOSS_LIMIT_USD", "0"))  # 0 = kapali (M1 ile ayni env)
# 14 Tem karari: kesici sabit USD yerine orana bagli. Gun baslangic canli
# MTM'sinin yuzdesi; gun devrinde o anki MTM'den hesaplanir, gun ici sabit.
# USD env de verilirse kucuk olan gecerli (cifte emniyet). Yalniz live modda
# etkin (paper motorlarda canli MTM anlamsiz). 0 = kapali.
DAILY_LOSS_LIMIT_PCT = float(os.getenv("MOM_DAILY_LOSS_LIMIT_PCT", "25"))
COOLDOWN_LOSS_SEC = float(os.getenv("MOM_COOLDOWN_STOP_MIN", "60")) * 60
COOLDOWN_EXIT_SEC = float(os.getenv("MOM_COOLDOWN_EXIT_MIN", "15")) * 60

# Hizli goz (12 Tem, canli asimetri B2): 30s tam tick arasinda fast feed'ten
# 2s kadansli cikis kontrolu (v6 deseni).
EXIT_INTERVAL_SEC = float(os.getenv("M_EXIT_INTERVAL_SEC", "2"))
# Kademeli satis toleransi (12 Tem, canli asimetri B1). Not: .env'deki
# MAX_SLIPPAGE_BPS eski live.py yolunu besler, bu tabloya BAGLI DEGILDIR.
EXIT_SLIPPAGE_BPS = {"tp_2": 150, "timeout_60": 150,
                     "stop_gec": 300, "stop_felaket": 1000}
STOP_RETRY_ADET = 3     # stop yolunda basarisiz satis: kadans beklemeden tekrar
STOP_RETRY_SEC = 3.0
SAT_COOLDOWN_SEC = 20.0  # ertelenen satis sonrasi soguma; yoksa 1s kadans Jupiter'i 429'a bogar
# Kor fiyat alarmi: feed + poll ikisi de fiyat veremiyorsa degerleme
# last_price'ta donar ve stoplar tetiklenemez; bu sessiz korluk esikten
# sonra CRITICAL alarma baglanir (14 Tem taramasi R1).
KOR_FIYAT_SEC = 120.0
KOR_ALARM_ARALIK_SEC = 60.0

STATE_FILE = "v7_state.json"
TRADES_FILE = "v7_trades.jsonl"


def h1_bant_atla(chg_h1: float, chg_m5: float | None = None) -> bool:
    """h1 kacinma bandinda mi? LO=HI (veya LO>HI) ise kacinma kapali.

    A2: bant ici aday m5 > 0 ise atlanmaz (kosu devam ediyor); m5 kosulu
    V7_H1_SKIP_M5_KOSUL=0 ile kapatilir. m5 bilinmiyorsa (None) eski
    kosulsuz davranis: atla. Bant disi adayda m5 hic degerlendirilmez."""
    if not (H1_SKIP_LO < H1_SKIP_HI and H1_SKIP_LO <= chg_h1 <= H1_SKIP_HI):
        return False
    if H1_SKIP_M5_KOSUL and chg_m5 is not None and chg_m5 > 0:
        return False
    return True


class V7Engine:
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
        self._bant_skip_ts: dict[str, float] = {}   # havuz -> son h1_bant_skip kaydi
        self._regime_logged = False
        self._kill_logged = False
        self._day_key: str = ""                     # UTC gun anahtari (YYYY-MM-DD)
        self._day_realized: float = 0.0             # gun ici realized PnL (limit icin)
        self._limit_logged = False                  # zarar limiti uyarisi tek sefer
        self._day_limit_usd: float | None = None    # gunun sabit kesici esigi (USD)
        self._limit_belirsiz_logged = False         # MTM yok uyarisi tek sefer
        self._yuklenen_gun_limiti: tuple | None = None
        self._huni = HuniSayac("V7")
        self._lock_fh = None
        self._son_exec_neden: str | None = None
        self._belirsiz_aday: dict | None = None    # belirsiz alim: benimseme bekleyen aday
        # Yurutme katmani (BROKER_MODE: paper/dryrun/live). Paper'da davranis
        # birebir ayni; kurulamazsa fail-closed: girisler kapali, cikislar paper.
        try:
            self._exec = make_exec_broker()
            self._exec_arizali = False
        except Exception as e:
            self._exec = PaperExecBroker()
            self._exec_arizali = True
            log.critical("V7: yurutme katmani kurulamadi (%s); girisler KAPALI", e)
        self._load()
        self._restore_day_realized()
        # gun ici restart: kesici esigi ayni gun icin sabit kalir (state'ten)
        if (self._yuklenen_gun_limiti
                and self._yuklenen_gun_limiti[0] == self._day_key
                and self._yuklenen_gun_limiti[1]):
            self._day_limit_usd = float(self._yuklenen_gun_limiti[1])

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
            self._yuklenen_gun_limiti = (data.get("day_limit_key"),
                                         data.get("day_limit_usd"))
        except Exception:
            backup = p.with_name(f"{p.name}.corrupt-{int(time.time())}")
            try:
                p.rename(backup)
                log.critical("v7 state bozuk, yedeğe taşındı: %s", backup)
            except OSError:
                log.critical("v7 state bozuk ve yedeklenemedi, temiz başlanıyor")

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

    # ---- Gun ici realized PnL sayaci (M1 paterni; limit kapaliyken etkisiz) -----
    def _day_realized_add(self, pnl: float, now: float) -> None:
        key = time.strftime("%Y-%m-%d", time.gmtime(now))
        if key != self._day_key:
            self._day_key = key
            self._day_realized = 0.0
            self._limit_logged = False
            self._day_limit_usd = None  # yeni gunun esigi o anki MTM'den
        self._day_realized += pnl

    def _restore_day_realized(self) -> None:
        """Restart'ta bugunun (UTC) realized PnL'ini trades dosyasindan geri yukle."""
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
            log.debug("V7 gun ici pnl geri yuklenemedi", exc_info=True)

    def _entries_blocked(self) -> str | None:
        """Yeni giris engeli var mi? None = serbest. Cikis yonetimi HER ZAMAN surer."""
        if self._exec_arizali:
            return "exec_arizali"  # tek-seferlik CRITICAL __init__'te atildi
        if kill_is_active():
            if not self._kill_logged:
                self._kill_logged = True
                log.critical("V7: kill-switch AKTIF, yeni girisler durdu (cikislar suruyor)")
            return "kill_switch"
        if self._kill_logged:
            self._kill_logged = False
            log.warning("V7: kill-switch kalkti, girisler serbest")
        if DAILY_LOSS_LIMIT_USD > 0 or self._pct_limit_aktif():
            key = time.strftime("%Y-%m-%d", time.gmtime())
            if key != self._day_key:  # gun devri: dunku zarar bugunu bloklamasin
                self._day_key = key
                self._day_realized = 0.0
                self._limit_logged = False
                self._day_limit_usd = None  # yeni gunun esigi o anki MTM'den
            limit, kesin = self._gun_limiti()
            if limit is None and not kesin:
                # PCT acik ama MTM okunamadi, USD yedek de yok: fail-closed
                if not self._limit_belirsiz_logged:
                    self._limit_belirsiz_logged = True
                    log.critical("V7: gun limiti hesaplanamadi (canli MTM yok), "
                                 "yeni giris kapali (fail-closed)")
                return "daily_limit_belirsiz"
            if self._limit_belirsiz_logged:
                self._limit_belirsiz_logged = False
                log.warning("V7: gun limiti hesaplandi, belirsizlik kalkti")
            if limit is not None and self._day_realized <= -limit:
                if not self._limit_logged:
                    self._limit_logged = True
                    log.critical(
                        "V7: gunluk zarar limiti asildi ($%.2f <= -$%.2f), "
                        "bugun (UTC) yeni giris yok", self._day_realized, limit,
                    )
                return "daily_loss_limit"
        return None

    # ---- Gunluk kesici esigi: gun baslangic MTM'sinin yuzdesi (14 Tem) ----------
    def _pct_limit_aktif(self) -> bool:
        return DAILY_LOSS_LIMIT_PCT > 0 and getattr(self._exec, "mode", "paper") == "live"

    def _canli_mtm(self) -> float | None:
        try:
            from hibrit_trader import canli_gosterge

            snap = canli_gosterge.son()
            if snap and float(snap.get("mtm") or 0.0) > 0:
                return float(snap["mtm"])
        except Exception:
            log.debug("V7 canli MTM okunamadi", exc_info=True)
        return None

    def _gun_limiti(self) -> tuple[float | None, bool]:
        """Gunun kesici esigi (pozitif USD). Donus (limit, kesin):
        kesin=True esik sabitlendi/biliniyor; kesin=False gecici durum
        (MTM henuz yok; limit varsa USD yedegi, yoksa fail-closed karari
        cagirana ait). Esik gun icinde SABIT: bir kez hesaplaninca degismez."""
        if self._day_limit_usd is not None:
            return self._day_limit_usd, True
        usd = DAILY_LOSS_LIMIT_USD if DAILY_LOSS_LIMIT_USD > 0 else None
        if not self._pct_limit_aktif():
            self._day_limit_usd = usd  # eski USD-only davranis, sabit zaten
            return usd, True
        mtm = self._canli_mtm()
        if mtm is None:
            return usd, False  # sabitleme yok: MTM gelince hesaplanacak
        limit = mtm * DAILY_LOSS_LIMIT_PCT / 100.0
        if usd is not None:
            limit = min(limit, usd)
        self._day_limit_usd = limit
        self._save()  # gun ici restart ayni esikle devam etsin
        log.warning("V7 gun limiti sabitlendi: MTM $%.2f x %%%g = $%.2f%s",
                    mtm, DAILY_LOSS_LIMIT_PCT, limit,
                    (" (USD yedegi $%.2f ile kucugu)" % usd) if usd is not None else "")
        return limit, True

    def _exec_fill(self, yon: str, token_address: str, *, usd: float = 0.0,
                   amount_token: float = 0.0, ref_fiyat: float = 0.0,
                   slippage_bps: int = 50, acilis_ts: float | None = None):
        """Fill'i yurutme katmanindan gecirir. Donus: (devam, canli_fill).

        paper/dryrun: muhasebe paper kalir, canli_fill None, devam True
        (dryrun quote hatasi yarisi ASLA etkilemez). live: fill basarisizsa
        devam False; basariliysa canli_fill baglayicidir."""
        self._son_exec_neden = None
        try:
            fill = self._exec.execute(ExecOrder(
                engine="V7", yon=yon, token_address=token_address,
                usd=usd, amount_token=amount_token, ref_fiyat=ref_fiyat,
                slippage_bps=slippage_bps, acilis_ts=acilis_ts))
        except Exception as e:
            log.error("V7 yurutme hatasi (%s %s): %s", yon, token_address[:8], e)
            fill = None
        if self._exec.mode != "live":
            return True, None
        if fill is None or not fill.ok:
            self._son_exec_neden = fill.neden if fill is not None else "exec_hata"
            return False, None
        return True, fill

    def _acquire_lock(self) -> bool:
        import fcntl

        p = self._path("v7_engine.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("V7: başka bir instance çalışıyor, motor başlatılmıyor")
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh
        return True

    # ---- Ana döngü (v2 ile aynı kadans, 3/8 interval faz kaydırma) ------------
    def run_forever(self) -> None:
        if not self._acquire_lock():
            return
        log.warning(
            "V7 senaryo başladı (v6 + fren) — sanal $%.2f · slot %d · "
            "giriş liq>=$%.0f + h1 %.0f..%.0f (skip %.0f..%.0f) · rejim>=%.2f · "
            "çıkış tp+%.0f%% / fren %%%.0f / "
            "%dm sabır sonrası stop%%%.0f / tavan %dm",
            self.balance, MAX_SLOTS, LIQ_MIN_USD, CHG_H1_MIN, CHG_H1_MAX,
            H1_SKIP_LO, H1_SKIP_HI, SOL_H1_MIN,
            TP_PCT, DISASTER_PCT, GRACE_SEC // 60, LATE_STOP_PCT, CEILING_SEC // 60,
        )
        self._save()
        feed = get_feed()
        if feed is not None:  # restart sonrasi acik pozisyon havuzlarini feed'e geri tak
            for pos in self.positions:
                feed.add_pool(pos["pool_address"])
        time.sleep(SCAN_INTERVAL_SEC * 3 / 8)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("v7 tick hatası")
            deadline = time.time() + SCAN_INTERVAL_SEC
            while True:
                kalan = deadline - time.time()
                if kalan <= 0:
                    break
                time.sleep(min(EXIT_INTERVAL_SEC, kalan))
                try:
                    self.fast_exit_tick()
                except Exception:
                    log.exception("v7 hizli cikis hatası")

    def tick(self) -> None:
        self._belirsiz_takip()
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
        self._save()

    # ---- R2-alim: belirsiz alim mutabakati (broker uzlastiricisinin sonucu) -----
    def _belirsiz_takip(self) -> None:
        if self._belirsiz_aday is None:
            return
        sorgu = getattr(self._exec, "belirsiz_sonuc", None)
        if sorgu is None:  # paper/dryrun: belirsiz aday olusamaz, temizle
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
            log.warning("V7 BELIRSIZ SONUC %s: tx zincirde yok, para cikmadi, "
                        "giris kayitsiz iptal", aday["pair"])
        else:  # cozulemedi / kayit_yok / bozuk detay
            log.critical("V7 BELIRSIZ SONUC %s: cozulemedi (%s); kilit kapali "
                         "kaliyor, manuel kontrol gerekir", aday["pair"], durum)

    def _belirsiz_pozisyon_ac(self, aday: dict, detay: dict) -> None:
        """Zincirde gerceklesen belirsiz alimi pozisyon olarak benimse.
        Muhasebe paper boyutta (usd), canli gercek miktar canli_miktar'da."""
        usd = aday["usd"]
        entry = detay["fiyat"]
        gas = GAS_COST_USD.get(aday["chain"], 0.1)
        now = aday["ts"]  # yas bazli cikislar gercek giris anindan saysin
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
        log.warning("V7 BUY (mutabakat) %s $%.2f @ %.8g: belirsiz tx zincirde "
                    "gerceklesti, pozisyon benimsendi (tx %s)",
                    aday["pair"], usd, entry, detay["tx_id"])

    # ---- Rejim: SOL chg_h1 motorlar arasi paylasimli cache'ten ------------------
    def _sol_chg_h1(self, client: httpx.Client) -> float | None:
        return sol_chg_h1(client)

    # ---- Giriş (v6 ile birebir: liq >= $100k + h1 bandı 10..50) -----------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        if self._entries_blocked():  # kill-switch / gunluk zarar limiti (varsayilan kapali)
            return
        try:
            pairs = scan_all(self.settings.scan_chains)
        except Exception as e:
            log.warning("V7 giris tick atlandi, tarama hatasi: %r", e)
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
            h1 = getattr(pr, "chg_h1", 0.0)
            if not (CHG_H1_MIN <= h1 <= CHG_H1_MAX):
                continue  # v6 bandı: dikey pump tepesi dışarıda
            if h1_bant_atla(h1, getattr(pr, "chg_m5", None)):
                self._bant_skip_kaydet(pr, now)
                continue
            cands.append(pr)
        cands.sort(key=lambda pr: pr.chg_h1, reverse=True)  # en güçlü trend önce
        self._huni.ekle(len(pairs), liq_ok, len(cands), now)
        if not cands:
            return
        # Rejim FAIL-CLOSED (09 Tem): veri yoksa kapi KAPALI; son basarili
        # deger 10dk'ya kadar gecerli, sonrasinda giris yok.
        sol_h1 = self._sol_chg_h1(client)
        if sol_h1 is None:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("V7 REJIM: sol_h1 verisi yok (fail-closed), giriş kapalı")
            rejim_reject_kaydet(cands, "V7", None)
            return
        if sol_h1 < SOL_H1_MIN:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("V7 REJIM: sol_chg_h1 %.2f%% < %.2f%%, giriş yok", sol_h1, SOL_H1_MIN)
            rejim_reject_kaydet(cands, "V7", sol_h1)
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
                safety_reject_kaydet(pair, "V7", "safety_hata", type(e).__name__)
                continue
            time.sleep(0.2 if self._aggressive else 1.5)
            if not report.ok:
                safety_reject_kaydet(
                    pair, "V7", report.kapi or "safety_red", "; ".join(report.reasons[:2])
                )
                continue
            if self._open_position(pair, budget_each, sol_h1, client=client):
                empty -= 1
                held.add(pair.pool_address)
                held.add(pair.token_address)

    def _bant_skip_kaydet(self, pair, now: float) -> None:
        """h1 kacinma bandi elemesini olcum satiri olarak yaz (30dk havuz dedup)."""
        son = self._bant_skip_ts.get(pair.pool_address, 0.0)
        if now - son < BANT_SKIP_DEDUP_SEC:
            return
        self._bant_skip_ts[pair.pool_address] = now
        if len(self._bant_skip_ts) > 200:  # sinirsiz buyume freni
            esik = now - BANT_SKIP_DEDUP_SEC
            self._bant_skip_ts = {
                p: t for p, t in self._bant_skip_ts.items() if t >= esik
            }
        sol_h1, _ = sol_h1_son_olcum()  # fetch yok, son paylasimli olcum
        bant_reject_kaydet(pair, "V7", sol_h1)

    def _open_position(self, pair, usd: float, sol_h1: float | None = None,
                       client: httpx.Client | None = None) -> bool:
        gas = GAS_COST_USD.get(pair.chain, 0.1)
        if self.balance < usd + gas:
            return False
        taze = taze_teyit(pair, "V7", client)
        if taze.iptal:
            log.warning("V7 GIRIS IPTAL %s: taze fiyat taramanin %%%.2f ustunde (kaynak %s)",
                        pair.name, taze.fark_pct, taze.kaynak)
            return False
        slip = _mom_slippage(usd, pair.liquidity_usd)
        eff_price = taze.fiyat * (1 + slip)
        karar_fiyat = eff_price  # canli fill ezmeden onceki karar fiyati (prim analizi)
        devam, canli = self._exec_fill("al", pair.token_address,
                                       usd=usd, ref_fiyat=eff_price)
        if not devam:
            if self._son_exec_neden == "islem_belirsiz":
                # R2-alim: tx zincirde olabilir; aday baglami saklanir, broker
                # uzlastiricisi karar verene kadar pozisyon YAZILMAZ.
                self._belirsiz_aday = {
                    "pair": pair.name, "chain": pair.chain,
                    "token_address": pair.token_address,
                    "pool_address": pair.pool_address,
                    "usd": usd, "karar_fiyat": karar_fiyat,
                    "chg_m5": round(getattr(pair, "chg_m5", 0.0), 2),
                    "chg_h1": round(pair.chg_h1, 2),
                    "liq_entry": round(pair.liquidity_usd, 2),
                    "sol_chg_h1": sol_h1,
                    "entry_price_source": taze.kaynak,
                    "entry_fresh_fark_pct": taze.fark_pct,
                    "entry_slip_pct": round(slip * 100, 4),
                    "ts": time.time(),
                }
                log.critical("V7 GIRIS BELIRSIZ %s: zincir mutabakati "
                             "bekleniyor, sonuca kadar canli islemler kilitli",
                             pair.name)
                return False
            log.error("V7 GIRIS IPTAL %s: canli alim gerceklesmedi", pair.name)
            return False
        if canli is not None and canli.fiyat > 0:
            eff_price = canli.fiyat  # sadece live: gercek fill fiyati baglayici
        # Muhasebe her modda paper boyutta surer; canli bilet (MTM x LIVE_TICKET_PCT) yarisi etkilemez.
        # Cuzdandaki gercek miktar ayrica canli_miktar'da tutulur (satis onu kullanir).
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
            "sol_chg_h1": sol_h1,   # gölgede eksikti: rejim analizi için kaydet
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
        if feed is not None:  # hizli goz: havuzu 1s feed'ine dinamik ekle
            feed.add_pool(pos["pool_address"])
        log.warning("V7 BUY %s $%.2f @ %.8g (h1 %.1f%%, liq $%.0f)",
                    pair.name, usd, eff_price, pair.chg_h1, pair.liquidity_usd)
        return True

    # ---- Çıkış: tp_2 / stop_felaket (-%10) / stop_gec / timeout_60 ---------------
    def _eval_position(self, pos: dict, price: float, now: float,
                       liquidity_usd: float | None = None) -> str | None:
        """Fiyatı işle (last_price/mfe/mae) ve çıkış nedeni döndür (yoksa None)."""
        price, ariza = guard_price(pos, price, now, "V7", liquidity_usd=liquidity_usd)
        if ariza:
            return None  # veri arizasi: islem tetikleme, degerleme son gecerli fiyatta
        pos["last_price"] = price
        entry = pos["entry_price"]
        pnl_pct = (price / entry - 1) * 100 if entry > 0 else 0.0
        if pnl_pct > pos["mfe_pct"]:
            pos["mfe_pct"] = round(pnl_pct, 4)
        if pnl_pct < pos["mae_pct"]:
            pos["mae_pct"] = round(pnl_pct, 4)
        age = now - pos["opened_ts"]
        if pnl_pct >= TP_PCT:
            return "tp_2"
        if pnl_pct <= DISASTER_PCT:
            return "stop_felaket"  # TEK fark: her an geçerli mutlak taban
        if age >= GRACE_SEC and pnl_pct <= LATE_STOP_PCT:
            return "stop_gec"
        if age >= CEILING_SEC:
            return "timeout_60"
        return None

    def _fiyat_tazelendi(self, pos: dict, now: float) -> None:
        pos["_taze_fiyat_ts"] = now
        if pos.pop("kor_fiyat", None):
            pos.pop("_kor_alarm_ts", None)
            log.warning("V7 kor fiyat sona erdi %s: taze fiyat geri geldi",
                        pos["pair"])

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
                            "V7 KOR FIYAT %s: %.0fs'dir taze fiyat yok, "
                            "degerleme donuk (last %.8g); stoplar "
                            "tetiklenemiyor olabilir", pos["pair"],
                            taze_yas, price)
            else:
                self._fiyat_tazelendi(pos, now)
            reason = self._eval_position(pos, price, now, liquidity_usd=liq)
            if reason:
                pos["_price_src"] = src
                pos["_price_ts"] = sample_ts
                self._close_position(pos, price, reason, now)

    def fast_exit_tick(self) -> None:
        """2s kadanslı çıkış kontrolü. SADECE fast feed'te taze fiyatı olan
        pozisyonlara bakar; taze fiyat yoksa dokunmaz, 30s tick kapsar
        (motor hiçbir koşulda kör kalmaz). Kapanış olmadıkça disk yazılmaz."""
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
        karar_cikis = eff_price  # canli fill ezmeden onceki karar cikisi
        sat_bps = EXIT_SLIPPAGE_BPS.get(reason, 150)
        deneme = STOP_RETRY_ADET if reason in ("stop_felaket", "stop_gec") else 1
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
                log.warning("V7 SATIS TEKRAR %s (%s): deneme %d/%d basarisiz, "
                            "%.0fs sonra yeniden", pos["pair"], reason,
                            i + 1, deneme, STOP_RETRY_SEC)
                time.sleep(STOP_RETRY_SEC)
        if not devam:
            pos["_sat_bekle_ts"] = time.time() + SAT_COOLDOWN_SEC
            log.error("V7 SATIS ERTELENDI %s: canli satis gerceklesmedi, "
                      "%.0fs soguma sonrasi tekrar denenecek",
                      pos["pair"], SAT_COOLDOWN_SEC)
            return
        if canli is not None and canli.fiyat > 0:  # sadece live baglayici
            eff_price = canli.fiyat
        gas = GAS_COST_USD.get(pos["chain"], 0.1)
        proceeds = pos["amount_token"] * eff_price - gas
        pnl = proceeds - cost
        hold_sec = round(now - pos["opened_ts"], 1)
        pnl_pct = (eff_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0.0
        # hiz kenari olcumu: fast yolunda tetik gecikmesi = simdi - feed ornek zamani
        price_src = pos.pop("_price_src", "poll")
        price_ts = pos.pop("_price_ts", None)
        tetik_gecikme = round(now - price_ts, 3) if price_ts else None

        # v2 hardening deseni: önce kayıt, sonra mutasyon, anında save
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
            row["signature"] = canli.tx_id  # denetim defteri tx imzasi kolonu
        if pos.get("tx_al"):
            row["signature_al"] = pos["tx_al"]
        cm = float(pos.get("canli_miktar") or 0.0)
        if cm > 0 and canli is not None and canli.tx_id:
            # gercek cuzdan pnl: canli fill fiyatlari x zincirdeki miktar
            # (paper boyut degil); panel SON ISLEMLER canli satiri bunu basar
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
        if feed is not None:  # hizli goz: kapanan havuzu feed'ten cikar
            feed.remove_pool(pos["pool_address"])
        log.warning("V7 SELL %s pnl $%.2f (%.2f%%) — %s, hold %.0fs (mfe %.1f%% mae %.1f%%)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec, pos["mfe_pct"], pos["mae_pct"])
