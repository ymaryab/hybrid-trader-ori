"""V6 senaryo motoru — ARINDIRILMIŞ GÖLGE, altıncı paralel sanal yarışçı.

Diğer motorlara (v2/v3/v4/v5/gölge) SIFIR dokunuş. Sadece şu dosyalara yazar:
  data/v6_state.json   (sanal bakiye + açık pozisyonlar)
  data/v6_trades.jsonl (her sanal kapanışta kayıt)

V6 = GÖLGE'NİN AYNISI + TEK ek filtre (2026-07-04 arındırma analizi):
  GİRİŞ : liq >= $100k VE 10 <= chg_h1 <= 50. Üst sınırın dayanağı: gölgenin
          11 bozucusunun en kötü 4'ü h1 > 70 ile girmişti (RMG +459658%,
          KITTY +173%, LojakPaul +93%, ALYCIACOW +72%); h1<=50 retrosu 67
          işlemde -$281'i +$125'e çevirdi, 3/3 gün pozitif. Fren YOK, yaş
          filtresi YOK (h1 tavanından sonra ikisi de net negatifti).
  ÇIKIŞ : tp_2 (+%2 kâr al) / stop_gec (ilk 30dk sabır, sonrası girişin -%2
          altı sat) / timeout_60. Gölge ile birebir.
  Ek    : sol_chg_h1 artık trade kayıtlarına yazılır (gölgede eksikti,
          rejim kırılımı analizi yapılamıyordu).

GÜÇLENDİRME (2026-07-09 yeniden aktivasyon):
  REJİM : sol_h1 < 0.5 iken giriş yok (0..0.5 bandı kanıtlı kaybettiren:
          v6/v7/v8 toplamı 41 işlem -$136, bkz POLICY V-serisi final).
  HIZLI GÖZ: çıkış kontrolü fast_price feed'inden 2s kadansla (M1/M2 deseni).
          Pozisyon açılınca havuz feed'e dinamik eklenir, kapanınca çıkar.
          Feed yok/bayat ise 30s polling fallback, motor kör kalmaz.
  ÖLÇÜM : trade kaydında price_source (fast/poll) + tetik_gecikme_sec.
  GİRİŞ TAZE-FİYAT TEYİDİ (09 Tem gece): alım kaydedilmeden hemen önce fiyat
          tazelenir (fast<=3s -> tek fetch -> tarama, fail-open). Taze fiyat
          taramanın +%2'den (MOM_ENTRY_FRESH_MAX_PCT) fazla üstündeyse giriş
          iptal, rejects'e "taze_fiyat_kacti" + 30dk recheck. Kayıt:
          entry_price_source (fast/fetch/scan) + entry_fresh_fark_pct.

Fill'ler sanal: gerçek fiyat + v2'nin likidite-slippage modeli + gas.
Kadans v2 ile aynı; 3/8 interval faz kaydırmalı (v2:0, v5:1/8, v3:1/4,
v6:3/8, gölge:1/2, v4:3/4). V6_ENABLED=0 ile kapatılır.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from hibrit_trader.config import GAS_COST_USD
from hibrit_trader.entry_fresh import HuniSayac, rejim_reject_kaydet, taze_teyit
from hibrit_trader.fast_price import get_feed
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

# ---- V6 eşikleri (gölge ile aynı zemin + h1 bandı) ---------------------------
CHG_H1_MIN = float(os.getenv("V6_CHG_H1_MIN", "10"))
CHG_H1_MAX = float(os.getenv("V6_CHG_H1_MAX", "50"))   # TEK ek: dikey pump tepesi dışarıda
LIQ_MIN_USD = float(os.getenv("V6_LIQ_MIN_USD", "100000"))
MAX_SLOTS = 5
START_BALANCE = float(os.getenv("V6_START_BALANCE", "1000"))
TP_PCT = 2.0            # giriş +%2 görülünce kâr al (gölge ile aynı)
GRACE_SEC = 30 * 60     # ilk 30dk aşağıda stop yok (sabır)
LATE_STOP_PCT = -2.0    # 30dk sonrası: girişin -%2 altı SAT
CEILING_SEC = 60 * 60   # 60dk tavan
# rejim eşiği 0.5: sol_h1 0..0.5 bandı kanıtlı kaybettiren (41 işlem -$136)
SOL_H1_MIN = float(os.getenv("V6_SOL_H1_MIN", "0.5"))
COOLDOWN_LOSS_SEC = float(os.getenv("MOM_COOLDOWN_STOP_MIN", "60")) * 60
COOLDOWN_EXIT_SEC = float(os.getenv("MOM_COOLDOWN_EXIT_MIN", "15")) * 60
# Hizli goz: 30s tam tick arasinda fast feed'ten 2s kadansli cikis kontrolu
EXIT_INTERVAL_SEC = float(os.getenv("M_EXIT_INTERVAL_SEC", "2"))

STATE_FILE = "v6_state.json"
TRADES_FILE = "v6_trades.jsonl"


class V6Engine:
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
        self._huni = HuniSayac("V6")
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
                log.critical("v6 state bozuk, yedeğe taşındı: %s", backup)
            except OSError:
                log.critical("v6 state bozuk ve yedeklenemedi, temiz başlanıyor")

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

        p = self._path("v6_engine.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = p.open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            log.critical("V6: başka bir instance çalışıyor, motor başlatılmıyor")
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
            "V6 senaryo başladı (arındırılmış gölge + rejim %.1f + hızlı göz) — "
            "sanal $%.2f · slot %d · giriş liq>=$%.0f + h1 %.0f..%.0f · "
            "çıkış tp+%.0f%% / %dm sabır sonrası stop%%%.0f / tavan %dm",
            SOL_H1_MIN, self.balance, MAX_SLOTS, LIQ_MIN_USD, CHG_H1_MIN, CHG_H1_MAX,
            TP_PCT, GRACE_SEC // 60, LATE_STOP_PCT, CEILING_SEC // 60,
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
                log.exception("v6 tick hatası")
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
                    log.exception("v6 hızlı çıkış tick hatası")

    def tick(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)
            self._enter(client)
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

    # ---- Giriş (gölge + TEK ek: h1 üst sınırı 50) -------------------------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        if kill_is_active():
            return
        try:
            pairs = scan_all(self.settings.scan_chains)
        except Exception as e:
            log.warning("V6 giris tick atlandi, tarama hatasi: %r", e)
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
                continue  # TEK ek: dikey pump tepesi (RMG/KITTY tipi) dışarıda
            cands.append(pr)
        cands.sort(key=lambda pr: pr.chg_h1, reverse=True)  # en güçlü trend önce
        self._huni.ekle(len(pairs), liq_ok, len(cands), now)
        if not cands:
            return
        # Rejim FAIL-CLOSED (09 Tem): veri yoksa kapi KAPALI; son basarili
        # deger 10dk'ya kadar gecerli, sonrasinda giris yok.
        sol_h1 = None
        try:
            sol_h1 = self._sol_chg_h1(client)
        except Exception:
            log.debug("v6 rejim: sol_chg_h1 alınamadı", exc_info=True)
        if sol_h1 is None:
            ts, cached = self._sol_h1_cache
            if cached is not None and now - ts <= SOL_H1_STALE_MAX_SEC:
                sol_h1 = cached
        if sol_h1 is None:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("V6 REJIM: sol_h1 verisi yok (fail-closed), giriş kapalı")
            rejim_reject_kaydet(cands, "V6", None)
            return
        if sol_h1 < SOL_H1_MIN:
            if not self._regime_logged:
                self._regime_logged = True
                log.warning("V6 REJIM: sol_chg_h1 %.2f%% < %.2f%%, giriş yok", sol_h1, SOL_H1_MIN)
            rejim_reject_kaydet(cands, "V6", sol_h1)
            return
        if self._regime_logged:
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
            if self._open_position(pair, budget_each, sol_h1, client=client):
                empty -= 1
                held.add(pair.pool_address)
                held.add(pair.token_address)

    def _open_position(self, pair, usd: float, sol_h1: float | None = None,
                       client: httpx.Client | None = None) -> bool:
        gas = GAS_COST_USD.get(pair.chain, 0.1)
        if self.balance < usd + gas:
            return False
        taze = taze_teyit(pair, "V6", client)
        if taze.iptal:
            log.warning("V6 GIRIS IPTAL %s: taze fiyat taramanin %%%.2f ustunde (kaynak %s)",
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
            "sol_chg_h1": sol_h1,   # gölgede eksikti: rejim analizi için kaydet
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
        feed = get_feed()
        if feed is not None:  # hizli goz: havuzu 1s feed'ine dinamik ekle
            feed.add_pool(pos["pool_address"])
        log.warning("V6 BUY %s $%.2f @ %.8g (h1 %.1f%%, liq $%.0f)",
                    pair.name, usd, eff_price, pair.chg_h1, pair.liquidity_usd)
        return True

    # ---- Çıkış: tp_2 / stop_gec (30dk sonrası -%2) / timeout_60 (gölge birebir) --
    def _eval_position(self, pos: dict, price: float, now: float,
                       liquidity_usd: float | None = None) -> str | None:
        """Fiyatı işle (last_price/mfe/mae) ve çıkış nedeni döndür (yoksa None)."""
        price, ariza = guard_price(pos, price, now, "V6", liquidity_usd=liquidity_usd)
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
        if age >= GRACE_SEC and pnl_pct <= LATE_STOP_PCT:
            return "stop_gec"
        if age >= CEILING_SEC:
            return "timeout_60"
        return None

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
        # hiz kenari olcumu: fast yolunda tetik gecikmesi = simdi - feed ornek zamani
        price_src = pos.pop("_price_src", "poll")
        price_ts = pos.pop("_price_ts", None)
        tetik_gecikme = round(now - price_ts, 3) if price_ts else None

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
        })
        self.balance += proceeds
        self.realized_pnl += pnl
        cd = COOLDOWN_LOSS_SEC if reason == "stop_gec" else COOLDOWN_EXIT_SEC
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
        log.warning("V6 SELL %s pnl $%.2f (%.2f%%) — %s, hold %.0fs (mfe %.1f%% mae %.1f%%)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec, pos["mfe_pct"], pos["mae_pct"])
