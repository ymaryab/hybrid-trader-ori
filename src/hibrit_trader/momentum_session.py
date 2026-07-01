"""Momentum paper modu — STRATEGY=momentum bayrağıyla açılan AYRI kod yolu (v2).

Bayrak kapalıyken bu modül HİÇ import edilmez; mevcut Engine aynen çalışır.
Bayrak açıkken bu engine çalışır ve SADECE ayrı dosyalara yazar:
  data/momentum_state.json   (bakiye + açık pozisyonlar)
  data/momentum_trades.jsonl (her kapanışta gerçekleşen PnL kaydı)
  data/momentum_exits.jsonl  (her kapanışta gözlem snapshot'ı)

Mevcut paper_state.json / trades.jsonl / exits.jsonl / attribution.jsonl /
shadow_exits.jsonl dosyalarına SIFIR dokunuş. Mevcut Engine kod yolu değişmez.
v1 verisi data/backup_momentum_v1/ altında saklı.

Kurallar (v2):
  GİRİŞ : liq >= $40k (sert taban) VE chg_m5 > 0 VE 5 <= chg_h1 <= 50 (aşırı
          pumplanmış tepeler dışarıda). chg_m5 desc sırala, 5 slot, ~bakiye/5.
          Güvenlik (honeypot/rug/holder) filtresi check_token ile korunur.
  ÇIKIŞ : state machine —
          stop_2     : -%2'ye düşünce anında sat (bekleme penceresi YOK)
          breakeven  : +%3'e ulaşınca stop giriş+%0.75'e çekilir (friction sonrası ~0)
          trail      : +%5'i geçince tepe fiyattan -%3 trailing stop
          timeout_60 : 60dk tavan, koşulsuz kapat (runner'a alan bırakır)
          Sabit TP yok; kazanan koşturulur.
  FRICTION: saf likidite modeli min(usd/liq, %5) — $40k tabanla ~%0.5/yön.
          Paylaşılan PAPER_SLIPPAGE_PCT knob'u BİLEREK kullanılmaz (o ana
          motorun ölçüm-sadakati kalibrasyonu; likidite-filtreli bu stratejide
          fiction'ı yapay şişirir). Gerekirse MOM_SLIPPAGE_PCT ile override.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from hibrit_trader.config import GAS_COST_USD
from hibrit_trader.live_sim import fetch_pool_price
from hibrit_trader.paper import _now_iso, new_trade_id
from hibrit_trader.safety import check_token
from hibrit_trader.scanner import scan_all

log = logging.getLogger(__name__)

# ---- Eşikler (env ile ayarlanabilir, güvenli varsayılanlar) ----------------
CHG_H1_MIN = float(os.getenv("MOM_CHG_H1_MIN", "5"))
CHG_H1_MAX = float(os.getenv("MOM_CHG_H1_MAX", "50"))
CHG_M5_MIN = float(os.getenv("MOM_CHG_M5_MIN", "0"))   # chg_m5 > bu değer (erken ivme)
LIQ_MIN_USD = float(os.getenv("MOM_LIQ_MIN_USD", "40000"))
MAX_SLOTS = int(os.getenv("MOM_MAX_SLOTS", "5"))
START_BALANCE = float(os.getenv("MOM_START_BALANCE", "1000"))
STOP_PCT = -2.0          # başlangıç stop: -%2'de anında sat
BE_ARM_PCT = 3.0         # +%3'e ulaşınca breakeven kilidi devreye girer
BE_STOP_PCT = 0.75       # kilit stop seviyesi: giriş +%0.75 (friction sonrası ~sıfır)
TRAIL_ARM_PCT = 5.0      # +%5'i geçince trailing devreye girer
TRAIL_PCT = 3.0          # tepe fiyattan -%3 trailing stop
CEILING_SEC = 60 * 60    # güvenlik tavanı: 60dk dolunca koşulsuz sat
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "30"))

STATE_FILE = "momentum_state.json"
TRADES_FILE = "momentum_trades.jsonl"
EXITS_FILE = "momentum_exits.jsonl"
REJECTS_FILE = "momentum_rejects.jsonl"   # pasif: filtreye takilanlar + 30dk sonrasi
SHADOW_FILE = "momentum_shadow.jsonl"     # pasif: kapanis sonrasi 20dk fiyat izi

# ---- Pasif gözlem ayarları (motor kararlarına SIFIR etki) -------------------
REJECT_DEDUP_SEC = 30 * 60      # aynı havuzu 30dk içinde tekrar reddedilmiş YAZMA
REJECT_RECHECK_SEC = 30 * 60    # reddedileni 30dk sonra bir kez fiyatla
RECHECK_MAX_PER_TICK = 10       # tick başına en çok 10 recheck GET (yük sınırı)
REJECT_WATCH_CAP = 300          # recheck kuyruğu tavanı (dosya/istek şişmesin)
SHADOW_MARKS = (60, 300, 600, 900, 1200)  # +1/5/10/15/20 dk
# SOL rejim etiketi: SOL/USDC ana havuzu (Raydium), saatte bir cache'lenir
SOL_USDC_POOL = os.getenv(
    "MOM_SOL_USDC_POOL", "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"
)
SOL_H1_CACHE_SEC = 3600


def _data_dir() -> Path:
    # İzolasyon/test için override edilebilir; gerçek çalışmada "data".
    return Path(os.getenv("MOMENTUM_DATA_DIR", "data"))


def _mom_slippage(usd: float, liquidity_usd: float) -> float:
    """Momentum'a özel slippage: saf likidite modeli, PAPER_SLIPPAGE_PCT'den bağımsız.

    Paylaşılan knob (.env PAPER_SLIPPAGE_PCT=5.27) düşük-likidite ölçümünden gelir;
    $40k tabanlı bu stratejide friction'ı yapay şişirir. MOM_SLIPPAGE_PCT set
    edilirse sabit override uygulanır (ölçüm-sadakati deneyi için).
    """
    knob = os.getenv("MOM_SLIPPAGE_PCT", "").strip()
    if knob:
        try:
            return min(max(float(knob) / 100.0, 0.0), 0.5)
        except ValueError:
            pass
    return min(usd / max(liquidity_usd, 1.0), 0.05)


class MomentumEngine:
    """STRATEGY=momentum kod yolu. Kendi state'i + kendi dosyaları, izole."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.balance: float = START_BALANCE
        self.start_balance: float = START_BALANCE
        self.realized_pnl: float = 0.0
        self.positions: list[dict] = []
        self._aggressive = os.getenv("PAPER_AGGRESSIVE", "0") == "1"
        # ---- Pasif gözlem state'i (in-memory, restart'ta kayıp kabul) -------
        self._reject_seen: dict[str, float] = {}    # pool -> son reject yazım ts
        self._reject_watch: dict[str, dict] = {}    # pool -> 30dk recheck bekleyen
        self._shadow_watch: dict[str, dict] = {}    # trade_id -> 20dk fiyat izi
        self._sol_h1_cache: tuple[float, float | None] = (0.0, None)  # (ts, chg_h1)
        self._load()

    # ---- Dosya yolları (yalnız momentum_*) ----------------------------------
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
            self.positions = list(data.get("positions", []))
        except Exception:
            log.exception("momentum state okunamadı, temiz başlanıyor")

    def _save(self) -> None:
        p = self._path(STATE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "balance": round(self.balance, 4),
            "start_balance": round(self.start_balance, 2),
            "realized_pnl": round(self.realized_pnl, 4),
            "positions": self.positions,
            "updated_at": _now_iso(),
        }, ensure_ascii=False, indent=2))

    def _append(self, name: str, row: dict) -> None:
        p = self._path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": round(time.time(), 3), "ts_iso": _now_iso(), **row}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    # ---- Ana döngü ----------------------------------------------------------
    def run_forever(self) -> None:
        log.warning(
            "MOMENTUM v2 başladı — bakiye $%.2f · slot %d · liq>=$%.0f · chg_m5>%.0f · "
            "chg_h1 %.0f..%.0f · stop %.0f%% be+%.0f%%→+%.2f%% trail+%.0f%%/-%.0f%% ceil %dm",
            self.balance, MAX_SLOTS, LIQ_MIN_USD, CHG_M5_MIN, CHG_H1_MIN, CHG_H1_MAX,
            STOP_PCT, BE_ARM_PCT, BE_STOP_PCT, TRAIL_ARM_PCT, TRAIL_PCT, CEILING_SEC // 60,
        )
        self._save()  # state dosyasını garanti et
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("momentum tick hatası")
            time.sleep(SCAN_INTERVAL_SEC)

    def tick(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            self._manage_exits(client)  # önce çıkış: slot/sermaye serbest kalsın
            self._enter(client)
            # Pasif gözlem: hata motoru asla kırmasın
            try:
                self._poll_shadow(client)
            except Exception:
                log.debug("momentum shadow poll hatası", exc_info=True)
            try:
                self._poll_reject_rechecks(client)
            except Exception:
                log.debug("momentum reject recheck hatası", exc_info=True)
        self._save()

    # ---- Giriş --------------------------------------------------------------
    def _enter(self, client: httpx.Client) -> None:
        empty = MAX_SLOTS - len(self.positions)
        if empty <= 0 or self.balance <= 1.0:
            return
        try:
            pairs = scan_all(self.settings.scan_chains)
        except Exception:
            log.exception("momentum scan hatası")
            return
        held = {p["pool_address"] for p in self.positions}
        # Filtre predicate'leri v2 ile BIREBIR aynı; tek fark takılanların pasif logu.
        cands = []
        for pr in pairs:
            if pr.pool_address in held or pr.price_usd <= 0:
                continue  # zaten pozisyondayız / fiyatsız kayıt: reject sayılmaz
            if pr.liquidity_usd < LIQ_MIN_USD:
                self._log_reject(pr, "liq_dusuk")
            elif getattr(pr, "chg_m5", 0.0) <= CHG_M5_MIN:
                self._log_reject(pr, "m5_negatif")
            elif getattr(pr, "chg_h1", 0.0) < CHG_H1_MIN:
                self._log_reject(pr, "h1_dusuk")
            elif getattr(pr, "chg_h1", 0.0) > CHG_H1_MAX:
                self._log_reject(pr, "h1_yuksek")
            else:
                cands.append(pr)
        cands.sort(key=lambda pr: pr.chg_m5, reverse=True)  # en yüksek ivme önce
        if not cands:
            log.info(
                "momentum: aday yok (liq>=%.0f, m5>%.0f, h1 %.0f..%.0f; %d pair tarandı)",
                LIQ_MIN_USD, CHG_M5_MIN, CHG_H1_MIN, CHG_H1_MAX, len(pairs),
            )
            return
        budget_each = self.balance / empty  # boş slotlara eşit dağıt (~bakiye/5)
        for i, pair in enumerate(cands):
            if empty <= 0 or budget_each < 1.0:
                # Filtreyi geçip slot/bütçe kalmadığı için giremeyenler (pasif log)
                for left in cands[i:]:
                    self._log_reject(left, "slot_dolu")
                break
            try:
                report = check_token(client, pair.chain, pair.token_address)
            except Exception:
                log.debug("momentum güvenlik kontrol hatası: %s", pair.name, exc_info=True)
                continue
            time.sleep(0.2 if self._aggressive else 1.5)  # rate limit
            if not report.ok:
                self._log_reject(pair, "safety_red")
                continue
            if self._open_position(pair, budget_each, client):
                empty -= 1
            else:
                # bakiye/bütçe yetmedi (giriş kararı zaten False'tu, sadece kayıt)
                self._log_reject(pair, "slot_dolu")

    def _open_position(self, pair, usd: float, client: httpx.Client | None = None) -> bool:
        gas = GAS_COST_USD.get(pair.chain, 0.1)
        if self.balance < usd + gas:
            return False
        slip = _mom_slippage(usd, pair.liquidity_usd)      # likidite modeli
        eff_price = pair.price_usd * (1 + slip)
        amount = usd / eff_price
        now = time.time()
        # ---- Pasif gözlem alanları (karara etkisiz; hata -> None) ----------
        buys_m5 = sells_m5 = buy_ratio_m5 = None
        sol_chg_h1 = None
        if client is not None:
            try:
                tx = self._fetch_txns_m5(client, pair.chain, pair.pool_address)
                if tx:
                    buys_m5, sells_m5 = tx
                    total = buys_m5 + sells_m5
                    buy_ratio_m5 = round(buys_m5 / total, 3) if total else None
            except Exception:
                log.debug("momentum buys_m5 alınamadı: %s", pair.name, exc_info=True)
            try:
                sol_chg_h1 = self._sol_chg_h1(client)
            except Exception:
                log.debug("momentum sol_chg_h1 alınamadı", exc_info=True)
        pos = {
            "trade_id": new_trade_id(pair.pool_address, now),
            "pair": pair.name,
            "chain": pair.chain,
            "token_address": pair.token_address,
            "pool_address": pair.pool_address,
            "entry_price": eff_price,
            "amount_token": amount,
            "cost_usd": round(usd, 4),
            "opened_ts": now,
            "opened_at": _now_iso(),
            "chg_m5": round(pair.chg_m5, 2),
            "chg_h1": round(pair.chg_h1, 2),
            "liq_entry": round(pair.liquidity_usd, 2),
            "entry_slip_pct": round(slip * 100, 4),
            # pasif gözlem: alım baskısı + rejim etiketi (karara etkisiz)
            "buys_m5": buys_m5,
            "sells_m5": sells_m5,
            "buy_ratio_m5": buy_ratio_m5,
            "sol_chg_h1": sol_chg_h1,
            # --- çıkış state machine ---
            "peak_price": eff_price,   # trailing için tepe takibi
            "be_armed": False,         # +%3 görüldü → stop girişe çekildi
            "trail_armed": False,      # +%5 görüldü → trailing aktif
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "last_price": eff_price,
        }
        self.balance -= (usd + gas)
        self.positions.append(pos)
        log.warning("MOMENTUM BUY %s $%.2f @ %.8g (m5 %.1f%%, h1 %.1f%%, liq $%.0f, slip %.2f%%)",
                    pair.name, usd, eff_price, pair.chg_m5, pair.chg_h1,
                    pair.liquidity_usd, slip * 100)
        return True

    # ---- Çıkış state machine ------------------------------------------------
    def _manage_exits(self, client: httpx.Client) -> None:
        now = time.time()
        for pos in list(self.positions):
            price = fetch_pool_price(client, pos["chain"], pos["pool_address"])
            if price is None or price <= 0:
                price = pos["last_price"]  # tick atlanırsa son bilinen fiyat
            pos["last_price"] = price
            entry = pos["entry_price"]
            pnl_pct = (price / entry - 1) * 100 if entry > 0 else 0.0
            if pnl_pct > pos["mfe_pct"]:
                pos["mfe_pct"] = round(pnl_pct, 4)
            if pnl_pct < pos["mae_pct"]:
                pos["mae_pct"] = round(pnl_pct, 4)
            if price > pos["peak_price"]:
                pos["peak_price"] = price
            # Kilitler tek yönlü: bir kez arm olan geri düşmez
            if pnl_pct >= BE_ARM_PCT:
                pos["be_armed"] = True
            if pnl_pct >= TRAIL_ARM_PCT:
                pos["trail_armed"] = True
            age = now - pos["opened_ts"]

            reason = None
            if pos["trail_armed"] and price <= pos["peak_price"] * (1 - TRAIL_PCT / 100):
                reason = "trail"          # tepeden -%3 düştü, kârı kilitle
            elif pos["be_armed"] and price <= entry * (1 + BE_STOP_PCT / 100):
                reason = "breakeven"      # +%3 sonrası giriş+%0.75'e döndü:
                                          # friction düştükten sonra ~sıfır kapanır
            elif pnl_pct <= STOP_PCT:
                reason = "stop_2"         # -%2: anında kes, bekleme yok
            elif age >= CEILING_SEC:
                reason = "timeout_60"     # 60dk tavan, koşulsuz kapat
            if reason:
                self._close_position(pos, price, reason, now)

    def _close_position(self, pos: dict, price: float, reason: str, now: float) -> None:
        liq = pos["liq_entry"]  # çıkış likiditesi yaklaşımı: giriş likiditesi
        cost = pos["cost_usd"]
        slip = _mom_slippage(cost, liq)                    # likidite modeli
        eff_price = price * (1 - slip)
        gross = pos["amount_token"] * eff_price
        gas = GAS_COST_USD.get(pos["chain"], 0.1)
        proceeds = gross - gas
        pnl = proceeds - cost
        self.balance += proceeds
        self.realized_pnl += pnl
        try:
            self.positions.remove(pos)
        except ValueError:
            pass
        hold_sec = round(now - pos["opened_ts"], 1)
        pnl_pct = (eff_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0.0
        friction_pct = round(pos["entry_slip_pct"] + slip * 100, 4)  # round-trip slippage

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
            "buys_m5": pos.get("buys_m5"),          # giriş anı alım baskısı (pasif)
            "sells_m5": pos.get("sells_m5"),
            "buy_ratio_m5": pos.get("buy_ratio_m5"),
            "sol_chg_h1": pos.get("sol_chg_h1"),    # giriş anı rejim etiketi (pasif)
            "cost_usd": round(cost, 4),
            "proceeds_usd": round(proceeds, 4),
            "pnl_usd": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 3),
            "hold_sec": hold_sec,
            "exit_reason": reason,
            "friction_pct": friction_pct,
            "opened_at": pos["opened_at"],
            "closed_at": _now_iso(),
        })
        self._append(EXITS_FILE, {
            "trade_id": pos["trade_id"],
            "pair": pos["pair"],
            "chain": pos["chain"],
            "token_address": pos["token_address"],
            "entry_price": pos["entry_price"],
            "exit_price": eff_price,
            "peak_price": pos["peak_price"],
            "chg_m5": pos["chg_m5"],
            "chg_h1": pos["chg_h1"],
            "liq_entry": pos["liq_entry"],
            "mfe_pct": pos["mfe_pct"],
            "mae_pct": pos["mae_pct"],
            "be_armed": pos["be_armed"],
            "trail_armed": pos["trail_armed"],
            "hold_sec": hold_sec,
            "exit_reason": reason,
        })
        log.warning("MOMENTUM SELL %s pnl $%.2f (%.2f%%) — %s, hold %.0fs (peak mfe %.1f%%)",
                    pos["pair"], pnl, pnl_pct, reason, hold_sec, pos["mfe_pct"])
        # Pasif: kapanan pozisyonu 20dk fiyat izine al (in-memory, hata kırmaz)
        try:
            self._register_shadow(pos, price, eff_price, reason, now)
        except Exception:
            log.debug("momentum shadow kayıt hatası", exc_info=True)

    # ==== PASİF GÖZLEM (motor kararlarına sıfır etki) =========================

    # ---- 1) Reddedilenler logu + 30dk sonrası recheck ------------------------
    def _log_reject(self, pair, reason: str) -> None:
        """Filtreye takılan adayı yaz (30dk dedup) ve 30dk-sonrası fiyat kuyruğuna al."""
        try:
            now = time.time()
            pool = pair.pool_address
            last = self._reject_seen.get(pool)
            if last is not None and now - last < REJECT_DEDUP_SEC:
                return  # aynı havuz 30dk içinde zaten yazıldı, dosya şişmesin
            self._reject_seen[pool] = now
            if len(self._reject_seen) > 2000:  # süresi geçenleri buda
                self._reject_seen = {
                    k: v for k, v in self._reject_seen.items()
                    if now - v < REJECT_DEDUP_SEC
                }
            self._append(REJECTS_FILE, {
                "type": "reject",
                "pair": pair.name,
                "chain": pair.chain,
                "pool_address": pool,
                "token_address": pair.token_address,
                "reason": reason,
                "liquidity_usd": round(pair.liquidity_usd, 2),
                "chg_m5": round(getattr(pair, "chg_m5", 0.0), 2),
                "chg_h1": round(getattr(pair, "chg_h1", 0.0), 2),
                "price_usd": pair.price_usd,
            })
            if pair.price_usd > 0 and len(self._reject_watch) < REJECT_WATCH_CAP:
                self._reject_watch[pool] = {
                    "pair": pair.name,
                    "chain": pair.chain,
                    "pool_address": pool,
                    "reason": reason,
                    "price_at_reject": pair.price_usd,
                    "reject_ts": now,
                    "due_ts": now + REJECT_RECHECK_SEC,
                }
        except Exception:
            log.debug("momentum reject log hatası", exc_info=True)

    def _poll_reject_rechecks(self, client: httpx.Client) -> None:
        """Süresi gelen reddedilenleri BIR kez fiyatla (tick başına en çok 10 GET)."""
        if not self._reject_watch:
            return
        now = time.time()
        due = sorted(
            (w for w in self._reject_watch.values() if now >= w["due_ts"]),
            key=lambda w: w["due_ts"],
        )[:RECHECK_MAX_PER_TICK]
        for w in due:
            price = fetch_pool_price(client, w["chain"], w["pool_address"])
            chg = (
                round((price / w["price_at_reject"] - 1) * 100, 3)
                if price and w["price_at_reject"] > 0 else None
            )
            self._append(REJECTS_FILE, {
                "type": "recheck_30m",
                "pair": w["pair"],
                "chain": w["chain"],
                "pool_address": w["pool_address"],
                "reason": w["reason"],
                "reject_ts": round(w["reject_ts"], 3),
                "price_at_reject": w["price_at_reject"],
                "price_30m_later": price,
                "chg_30m_pct": chg,
            })
            self._reject_watch.pop(w["pool_address"], None)

    # ---- 2) Alım baskısı: GeckoTerminal tek havuz, m5 buys/sells -------------
    def _fetch_txns_m5(self, client: httpx.Client, chain: str, pool: str) -> tuple[int, int] | None:
        """Giriş anında 1 GET: son-5dk buy/sell işlem sayıları. Yoksa None."""
        from hibrit_trader.config import API

        url = f"{API['geckoterminal']}/networks/{chain}/pools/{pool}"
        resp = client.get(url, headers={"accept": "application/json"}, timeout=10)
        resp.raise_for_status()
        m5 = ((resp.json()["data"]["attributes"].get("transactions") or {}).get("m5") or {})
        if not m5:
            return None
        return int(float(m5.get("buys") or 0)), int(float(m5.get("sells") or 0))

    # ---- 3) Rejim etiketi: SOL/USDC chg_h1, saatlik cache ---------------------
    def _sol_chg_h1(self, client: httpx.Client) -> float | None:
        """SOL'un kendi chg_h1'i (SOL/USDC ana havuzu). Saatte bir GET, cache'li."""
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

    # ---- 4) Shadow tracker: kapanış sonrası 20dk fiyat izi --------------------
    def _register_shadow(self, pos: dict, raw_price: float, eff_price: float,
                         reason: str, now: float) -> None:
        self._shadow_watch[pos["trade_id"]] = {
            "trade_id": pos["trade_id"],
            "pair": pos["pair"],
            "chain": pos["chain"],
            "pool_address": pos["pool_address"],
            "entry_price": pos["entry_price"],
            "exit_price_raw": raw_price,   # slip öncesi piyasa fiyatı (kıyas tabanı)
            "exit_price_eff": eff_price,
            "exit_reason": reason,
            "exit_ts": now,
            "samples": {},                 # saniye işareti -> fiyat
            "wmax": raw_price,
            "wmin": raw_price,
        }

    def _poll_shadow(self, client: httpx.Client) -> None:
        """Her tick: kapananların fiyatını örnekle; 20dk dolunca dosyaya yaz."""
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
