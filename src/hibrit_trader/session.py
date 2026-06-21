"""Oturum motoru — tarama → karar → gir/çık döngüsü."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date

import httpx

from hibrit_trader.cex_confluence import cex_hold_score, cex_symbol_scores
from hibrit_trader.config import CHAIN_ENTRY_PRIORITY, Settings
from hibrit_trader.decision import Decision, DecisionPolicy, evaluate_entry, evaluate_exit
from hibrit_trader.entry_diagnostics import build_entry_diagnostics
from hibrit_trader.exit_policy import ExitPolicy, init_position_exit_state
from hibrit_trader.killswitch import is_active
from hibrit_trader.paper import Position
from hibrit_trader.position_sizer import compute_position_usd
from hibrit_trader.safety import SafetyReport, check_token, entry_safety_ok
from hibrit_trader.scanner import Pair, scan_all
from hibrit_trader.score import rank
from hibrit_trader.dex_trending_strategy import pool_age_hours
from hibrit_trader import telemetry
from hibrit_trader.slippage_edge import estimate_entry_slippage_pct
from hibrit_trader.peak_intelligence import ExitContext, whale_for_pair
from hibrit_trader.phantom_trade import PhantomPendingTrade
from hibrit_trader.smart_money import (
    estimate_wallet_buyers,
    scan_whale_accumulation,
    smart_money_entry_ok,
    wallet_buyer_info,
)
from hibrit_trader.trade_confluence import build_confluence_snapshot, compute_trade_confluence
from hibrit_trader.dex_trending_strategy import evaluate_trending, trending_fast_enabled
from hibrit_trader.dex_boost import watchlist_sort_key
from hibrit_trader.pump_research import analyze_pump_pair, founder_fast_entry_ok, moonshot_min_score
from hibrit_trader.early_launch import genesis_entry_ok, is_trending_late_pump, pump_entry_ok
from hibrit_trader.growth_potential import build_growth_watchlist
from hibrit_trader.pair_cooldown import PairCooldownStore
from hibrit_trader.slot_rotation import (
    pick_weakest_hold,
    should_rotate,
    slot_rotation_enabled,
)

log = logging.getLogger(__name__)

SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "30"))
SAFETY_CACHE_TTL = 3600
PAIR_COOLDOWN_LOSS_SEC = int(os.getenv("PAIR_COOLDOWN_SEC", "5400"))  # kayıp sonrası
PAIR_COOLDOWN_ANY_SEC = int(os.getenv("PAIR_COOLDOWN_ANY_SEC", "1800"))  # her kapanış (30dk)
MACRO_REFRESH_TICKS = 15
BRAIN_REFRESH_TICKS = 30
EARLY_TICKS_MAX = int(os.getenv("EARLY_TICKS_MAX", "5"))  # ilk ~5 tick (~0/30/60/90/120s) profili
BRAIN_COOLDOWN_SEC = 300
HOLD_REFRESH_TICKS = 20


def _brain_enabled() -> bool:
    return os.getenv("HIBRIT_BRAIN_ENABLED", "1") != "0"


def _smart_money_required() -> bool:
    from hibrit_trader.smart_money import alpha_wallet_addresses

    if os.getenv("PAPER_AGGRESSIVE", "1") != "0" and not alpha_wallet_addresses():
        return os.getenv("SMART_MONEY_REQUIRED", "0") != "0"
    return os.getenv("SMART_MONEY_REQUIRED", "1") != "0"


def _aggressive_trading(settings: Settings | None = None) -> bool:
    if settings is not None and settings.paper_aggressive and settings.mode == "paper":
        return True
    return os.getenv("AGGRESSIVE_TRADING", "0") == "1"


class Engine:
    def __init__(self, settings: Settings, broker, policy: DecisionPolicy | None = None) -> None:
        self.settings = settings
        self.broker = broker
        self.policy = policy or DecisionPolicy(
            entry_score_min=settings.entry_score_min,
            min_edge_after_cost_pct=settings.min_edge_after_cost_pct,
            require_smart_money=_smart_money_required(),
            max_open_positions=settings.max_open_positions,
        )
        self._broker_lock = threading.Lock()
        self.watchlist: list[tuple[float, Pair]] = []
        self._safety_cache: dict[str, tuple[float, SafetyReport]] = {}
        self._last_prices: dict[str, float] = {}
        self._missing_ticks: dict[str, int] = {}
        self._daily_pnl: float = 0.0
        self._daily_date = date.today()
        self._macro_avg: float | None = None
        self._macro_updated_at: float = 0.0
        self._tick_count = 0
        self.last_decision: Decision | None = None
        self._brain_verdict = None
        self._brain_updated_at: float = 0.0
        self._brain_lock = threading.Lock()
        self._brain_running = False
        self._brain_last_error: str | None = None
        self._brain_degraded = False
        self._whale_signals: list[dict] = []
        self._binance_holds: list[dict] = []
        self._okx_holds: list[dict] = []
        self._holds_updated_at: float = 0.0
        self._entry_diagnostics: list[dict] = []
        self._growth_watchlist: list[dict] = []
        self._pair_cooldown = PairCooldownStore()
        self._tick_started_ts: float = 0.0
        # Telemetri reddetme dedup: (token, reject_type) -> son log zamanı
        self._decision_logged: dict[tuple[str, str], float] = {}
        self._daily_halt_logged: bool = False  # gunluk-limit olayini gunde bir kez logla

    def _reset_daily_if_needed(self) -> None:
        today = date.today()
        if today != self._daily_date:
            self._daily_pnl = 0.0
            self._daily_date = today
            self._daily_halt_logged = False

    def _pair_by_pool(self, pairs: list[Pair], pool_address: str) -> Pair | None:
        for p in pairs:
            if p.pool_address == pool_address:
                return p
        return None

    def _check_safety(self, client: httpx.Client, chain: str, token: str) -> SafetyReport:
        key = f"{chain}:{token}"
        now = time.time()
        cached = self._safety_cache.get(key)
        if cached and now - cached[0] < SAFETY_CACHE_TTL:
            return cached[1]
        report = check_token(client, chain, token)
        self._safety_cache[key] = (now, report)
        if os.getenv("PAPER_AGGRESSIVE", "0") == "1":
            time.sleep(0.2)
        else:
            time.sleep(1.5)
        return report

    def _refresh_macro_if_needed(self) -> None:
        self._tick_count += 1
        if self._tick_count % MACRO_REFRESH_TICKS != 0:
            return
        try:
            from hibrit_trader.advanced_scan.cex_scan import scan_cex

            rows = scan_cex(limit=5)
            majors = [r["score"] for r in rows if r["symbol"] in ("BTC", "ETH", "SOL")]
            if majors:
                self._macro_avg = round(sum(majors) / len(majors), 1)
                self._macro_updated_at = time.time()
                log.info("Makro rejim skoru: %.1f", self._macro_avg)
        except Exception:
            log.debug("Makro skor güncellenemedi", exc_info=True)

    def _refresh_holds_if_needed(self, pairs: list[Pair], *, client: httpx.Client | None = None) -> None:
        if self._tick_count % HOLD_REFRESH_TICKS != 0 and self._holds_updated_at > 0:
            return
        self._sync_market_intel(pairs, client=client)

    def _sync_market_intel(self, pairs: list[Pair], *, client: httpx.Client | None = None) -> None:
        """CEX tut + balina — giriş konfluansı için senkron güncelle."""
        try:
            from hibrit_trader.hold_ranking import scan_binance_holds, scan_okx_holds

            self._binance_holds = scan_binance_holds(limit=12)
            self._okx_holds = scan_okx_holds(limit=12)
            self._whale_signals = scan_whale_accumulation(pairs, limit=15, client=client)
            self._holds_updated_at = time.time()
        except Exception:
            log.debug("Hold/whale tarama güncellenemedi", exc_info=True)

    def _confluence_snapshot(self):
        return build_confluence_snapshot(
            binance_holds=self._binance_holds,
            okx_holds=self._okx_holds,
            whale_signals=self._whale_signals,
            brain_verdict=self._brain_verdict,
            macro_avg=self._macro_avg,
            brain_penalty=self._brain_entry_penalty(),
            confluence_min=self.settings.confluence_min,
            min_layers=self.settings.confluence_min_layers,
            aggressive=_aggressive_trading(self.settings),
        )

    def _cex_scores(self) -> dict[str, float]:
        return cex_symbol_scores(self._binance_holds, self._okx_holds)

    def _rank_pairs(self, pairs: list[Pair], *, client: httpx.Client | None = None) -> list[tuple[float, Pair]]:
        return rank(pairs, self.settings.max_position_usd, cex_scores=self._cex_scores(), client=client)

    def _prefetch_safety(self, client: httpx.Client, ranked: list[tuple[float, Pair]], limit: int = 12) -> None:
        """Teşhis tablosunda 'güvenlik bekleniyor' azaltmak için top-N güvenlik ön-ısıtma."""
        if _aggressive_trading(self.settings):
            limit = int(os.getenv("SAFETY_PREFETCH_LIMIT", "4"))
            genesis: list[tuple[float, Pair]] = []
            rest: list[tuple[float, Pair]] = []
            for item in ranked:
                if pump_entry_ok(item[1])[0]:
                    genesis.append(item)
                else:
                    rest.append(item)
            ranked = genesis + rest
        for _, pair in ranked[:limit]:
            self._check_safety(client, pair.chain, pair.token_address)

    def _refresh_entry_diagnostics(
        self,
        ranked: list[tuple[float, Pair]],
        *,
        live_allowed: bool,
        client: httpx.Client | None = None,
    ) -> None:
        try:
            self._entry_diagnostics = build_entry_diagnostics(
                ranked,
                policy=self.policy,
                settings=self.settings,
                broker=self.broker,
                macro_avg=self._macro_avg,
                brain_penalty=self._brain_entry_penalty(),
                binance_holds=self._binance_holds,
                okx_holds=self._okx_holds,
                safety_cache=self._safety_cache,
                kill_switch=is_active(),
                held_tokens={p.token_address for p in self.broker.positions},
                live_allowed=live_allowed,
                daily_pnl=self._daily_pnl,
                client=client,
                whale_signals=self._whale_signals,
                brain_verdict=self._brain_verdict,
                confluence_required=self.settings.confluence_required,
            )
        except Exception:
            log.exception("entry_diagnostics hatası")

    def _fallback_brain(self, reason: str):
        from hibrit_trader.brain.orchestrator import BrainVerdict

        return BrainVerdict(
            regime="neutral",
            action_bias="neutral",
            entry_penalty=0.0,
            exit_bias="neutral",
            counterparty_thesis=f"Yedek mod — ağ/API bekleniyor ({reason[:120]})",
            predicted_moves=[],
            confidence=25.0,
            macro_avg=self._macro_avg,
            fear_greed=None,
            fear_greed_label=None,
            scan_count=0,
            tam_isabet_symbols=[],
            top_picks=[],
            sources=["fallback"],
        )

    def _run_brain_locked(self) -> None:
        from hibrit_trader.brain import run_brain

        self._brain_verdict = run_brain()
        self._brain_degraded = False
        self._brain_last_error = None
        self._brain_updated_at = time.time()
        if self._brain_verdict.macro_avg is not None:
            self._macro_avg = self._brain_verdict.macro_avg
            self._macro_updated_at = self._brain_updated_at
        log.info(
            "Saito beyin: %s / %s — %s",
            self._brain_verdict.regime,
            self._brain_verdict.action_bias,
            self._brain_verdict.counterparty_thesis[:80],
        )

    def _run_brain_job(self) -> None:
        try:
            self._run_brain_locked()
        except Exception as exc:
            log.exception("Saito beyin güncellenemedi")
            self._brain_last_error = str(exc)[:200]
            if self._brain_verdict is None:
                self._brain_verdict = self._fallback_brain(self._brain_last_error)
                self._brain_degraded = True
                self._brain_updated_at = time.time()
        finally:
            with self._brain_lock:
                self._brain_running = False

    def _refresh_brain_if_needed(self) -> None:
        if not _brain_enabled() or self._brain_running:
            return
        due = self._tick_count == 1 or self._tick_count % BRAIN_REFRESH_TICKS == 0
        if not due:
            return
        if self._brain_verdict is not None and time.time() - self._brain_updated_at < BRAIN_COOLDOWN_SEC:
            if self._tick_count != 1:
                return
        self._start_brain_async(background=True)

    def _start_brain_async(self, background: bool = True) -> bool:
        with self._brain_lock:
            if self._brain_running:
                return False
            self._brain_running = True

        def _job() -> None:
            self._run_brain_job()

        if background:
            threading.Thread(target=_job, daemon=True, name="hibrit-brain").start()
            return True
        _job()
        return True

    def schedule_brain_startup(self) -> None:
        if not _brain_enabled():
            return

        def _delayed() -> None:
            time.sleep(2)
            if self._brain_verdict is None and not self._brain_running:
                self._start_brain_async(background=True)

        threading.Thread(target=_delayed, daemon=True, name="hibrit-brain-boot").start()

    def _brain_entry_penalty(self) -> float:
        if self._brain_verdict is None:
            return 0.0
        raw = float(self._brain_verdict.entry_penalty)
        if _aggressive_trading(self.settings):
            # Paper agresif: risk_off girişi kilitlemesin (wiki idle-capital triage)
            cap = float(os.getenv("PAPER_BRAIN_PENALTY_MAX", "0"))
            if cap <= 0:
                return 0.0
            return round(min(raw * 0.35, cap), 1)
        return raw

    def _exit_policy(self) -> ExitPolicy:
        base = self.policy.base_exit_policy()
        bias = "neutral"
        if _aggressive_trading(self.settings):
            bias = "aggressive"
        elif self._brain_verdict is not None:
            bias = getattr(self._brain_verdict, "exit_bias", None) or self._brain_verdict.action_bias
        ep = ExitPolicy.for_regime(base, bias)
        if _aggressive_trading(self.settings) and os.getenv("DEX_TRENDING_FAST", "1") != "0":
            ep = ExitPolicy.for_dex_trending(ep)
        return ep

    def brain_state(self) -> dict:
        if self._brain_verdict is None:
            msg = "Saito analiz başlatılıyor…"
            if self._brain_running:
                msg = "Saito analiz ediyor… (20–90 sn)"
            elif self._brain_last_error:
                msg = f"Analiz yeniden denenecek — {self._brain_last_error[:100]}"
            return {
                "ready": False,
                "message": msg,
                "running": self._brain_running,
                "last_error": self._brain_last_error,
            }
        out = self._brain_verdict.to_dict()
        out["ready"] = True
        out["running"] = self._brain_running
        out["degraded"] = self._brain_degraded
        out["last_error"] = self._brain_last_error
        out["exit_policy"] = self._exit_policy().summary()
        return out

    def request_brain_run(self) -> dict:
        """Panel — arka planda başlat, hemen dön (bloklama yok)."""
        if not _brain_enabled():
            return {"ready": False, "message": "Saito beyin devre dışı (HIBRIT_BRAIN_ENABLED=0)"}
        if not self._start_brain_async(background=True):
            return {**self.brain_state(), "message": "Analiz zaten çalışıyor…"}
        return self.brain_state()

    def run_brain_now(self) -> dict:
        """Senkron çalıştır — test/CLI."""
        if not _brain_enabled():
            return {"ready": False, "message": "Saito beyin devre dışı (HIBRIT_BRAIN_ENABLED=0)"}
        with self._brain_lock:
            if self._brain_running:
                return {**self.brain_state(), "message": "Analiz zaten çalışıyor…"}
            self._brain_running = True
        self._run_brain_job()
        return self.brain_state()

    def market_intel_state(self) -> dict:
        return {
            "whale_accumulation": self._whale_signals,
            "binance_holds": self._binance_holds,
            "okx_holds": self._okx_holds,
            "updated_at": self._holds_updated_at or None,
            "alpha_tracking": self._alpha_tracking_state(),
        }

    def _alpha_tracking_state(self) -> dict:
        from hibrit_trader.helius_alpha import alpha_tracking_status

        return alpha_tracking_status()

    def _record_decision(self, decision: Decision) -> None:
        self.last_decision = decision
        if decision.action == "enter":
            log.info("KARAR AL: %s — %s", decision.pair, decision.reason)
        elif decision.action in ("exit", "exit_partial"):
            log.info(
                "KARAR SAT: %s — %s (PnL %% %.1f%s)",
                decision.pair,
                decision.reason,
                decision.pnl_pct or 0,
                f" frac={decision.sell_fraction:.0%}" if decision.action == "exit_partial" else "",
            )
        elif decision.action == "skip" and decision.score and decision.score >= self.policy.entry_score_min - 5:
            log.debug("KARAR BEKLE: %s — %s", decision.pair, decision.reason)

    def _refresh_open_position_prices(self) -> None:
        if not self.broker.positions:
            return
        from hibrit_trader.live_sim import fetch_pool_price

        with httpx.Client() as client:
            for pos in self.broker.positions:
                live = fetch_pool_price(client, pos.chain, pos.pool_address)
                if live is not None:
                    self._last_prices[pos.pool_address] = live

    def _exit_context(self, pair: Pair | None, score: float) -> ExitContext:
        bv = self._brain_verdict
        exit_bias = "neutral"
        fg: int | None = None
        if bv is not None:
            exit_bias = getattr(bv, "exit_bias", None) or bv.action_bias or "neutral"
            fg = bv.fear_greed
        return ExitContext(
            dex_score=score,
            macro_avg=self._macro_avg,
            fear_greed=fg,
            exit_bias=exit_bias,
            whale=whale_for_pair(pair, self._whale_signals),
        )

    def _exit_positions(self, pairs: list[Pair], ranked: list[tuple[float, Pair]]) -> None:
        score_map = {p.pool_address: s for s, p in ranked}
        exit_policy = self._exit_policy()
        pending: list[tuple[Position, Decision]] = []

        for pos in list(self.broker.positions):
            pair = self._pair_by_pool(pairs, pos.pool_address)
            if pair:
                price = pair.price_usd
                self._last_prices[pos.pool_address] = price
                self._missing_ticks[pos.pool_address] = 0
            else:
                price = self._last_prices.get(pos.pool_address, pos.entry_price)
                self._missing_ticks[pos.pool_address] = self._missing_ticks.get(pos.pool_address, 0) + 1

            init_position_exit_state(pos, pair)
            self._update_excursion(pos, price, pair)
            score = score_map.get(pos.pool_address, 0)
            missing = self._missing_ticks.get(pos.pool_address, 0)
            if pair and is_trending_late_pump(pair):
                pending.append(
                    (
                        pos,
                        Decision(
                            "exit",
                            "trending geç pump — sermaye serbest",
                            pos.pair_name,
                            score,
                        ),
                    )
                )
                continue
            exit_dec = evaluate_exit(
                pos,
                price,
                score,
                missing,
                self.policy,
                exit_policy=exit_policy,
                pair=pair,
                exit_ctx=self._exit_context(pair, score),
            )
            if exit_dec:
                pending.append((pos, exit_dec))

        for pos, decision in pending:
            price = self._last_prices.get(pos.pool_address, pos.entry_price)
            pair = self._pair_by_pool(pairs, pos.pool_address)
            liq = pair.liquidity_usd if pair else 100_000
            try:
                if decision.action == "exit_partial" and decision.sell_fraction:
                    trade = self.broker.sell_partial(
                        pos, decision.sell_fraction, price, liq, decision.reason
                    )
                else:
                    trade = self.broker.sell(pos, price, liq, decision.reason)
            except PhantomPendingTrade:
                log.info("Phantom satış imzası bekleniyor: %s", pos.pair_name)
                continue
            self._daily_pnl += trade.pnl_usd
            self._apply_pair_cooldown(pos, trade.pnl_usd)
            self._record_decision(decision)
            telemetry.log_event(
                "MONEY", f"SELL {pos.pair_name} pnl ${trade.pnl_usd:+.2f} — {decision.reason}",
                trade_id=pos.trade_id, pair=pos.pair_name, pnl_usd=round(trade.pnl_usd, 2),
                reason=decision.reason, partial=(decision.action == "exit_partial"),
            )
            if pos.pool_address not in {p.pool_address for p in self.broker.positions}:
                self._log_exit_profile(pos, trade)  # tam kapanış (kısmi satışta yazma)

    def _apply_pair_cooldown(self, pos: Position, pnl_usd: float) -> None:
        cd = PAIR_COOLDOWN_LOSS_SEC if pnl_usd <= 0 else PAIR_COOLDOWN_ANY_SEC
        self._pair_cooldown.set_cooldown(pos.token_address, pos.pair_name, cd)

    def _pair_on_cooldown(self, token_address: str, pair_name: str = "") -> bool:
        return self._pair_cooldown.on_cooldown(token_address, pair_name)

    # ---- Telemetri ----------------------------------------------------------
    def _regime_stamp(self) -> tuple[str, int | None, float | None]:
        bv = self._brain_verdict
        regime = getattr(bv, "regime", "") if bv is not None else ""
        fg = getattr(bv, "fear_greed", None) if bv is not None else None
        return regime, fg, self._macro_avg

    def _update_excursion(self, pos: Position, price: float, pair: Pair | None) -> None:
        """Her tick: MFE/MAE + min likidite + runner zaman-profili (saf gözlem)."""
        if pos.entry_price > 0 and price > 0:
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
            held = max(0.0, time.time() - pos.opened_ts) if pos.opened_ts else 0.0
            if pnl_pct > pos.mfe_pct:
                pos.mfe_pct = pnl_pct
                pos.mfe_at_sec = held
            if pnl_pct < pos.mae_pct:
                pos.mae_pct = pnl_pct
                pos.mae_at_sec = held
            # Tepe/dip fiyat + epoch ms zaman damgası (entry baseline ile başlat)
            now_ms = int(time.time() * 1000)
            if pos.obs_peak_price <= 0:
                entry_ms = int(pos.opened_ts * 1000) if pos.opened_ts else now_ms
                pos.obs_peak_price = pos.entry_price
                pos.obs_peak_ts_ms = entry_ms
                pos.obs_trough_price = pos.entry_price
                pos.obs_trough_ts_ms = entry_ms
            if price > pos.obs_peak_price:
                pos.obs_peak_price = price
                pos.obs_peak_ts_ms = now_ms
            if price < pos.obs_trough_price:
                pos.obs_trough_price = price
                pos.obs_trough_ts_ms = now_ms
            # İlk ~5 tick fiyat profili: [[saniye, fiyat], ...] (onay penceresi)
            if len(pos.early_ticks) < EARLY_TICKS_MAX:
                if not pos.early_ticks:
                    pos.early_ticks.append([0, pos.entry_price])
                if len(pos.early_ticks) < EARLY_TICKS_MAX:
                    sec = int(round(time.time() - pos.opened_ts)) if pos.opened_ts else 0
                    pos.early_ticks.append([sec, price])
        if pair is not None and pair.liquidity_usd > 0:
            base = pos.liq_min if pos.liq_min > 0 else (pos.liq_entry or pair.liquidity_usd)
            pos.liq_min = min(base, pair.liquidity_usd)

    def _log_exit_profile(self, pos: Position, trade) -> None:
        """Tam kapanışta runner zaman-profili → exits.jsonl (trades.jsonl'den ayrı, saf gözlem)."""
        try:
            entry_ts_ms = int(pos.opened_ts * 1000) if pos.opened_ts else None
            ttp = (
                pos.obs_peak_ts_ms - entry_ts_ms
                if entry_ts_ms and pos.obs_peak_ts_ms
                else None
            )
            telemetry.log_exit({
                "trade_id": pos.trade_id,
                "pair": pos.pair_name,
                "chain": pos.chain,
                "token_address": pos.token_address,
                "source": pos.discovery_source,
                "regime": pos.entry_regime,
                "entry_price": pos.entry_price,
                "entry_ts_ms": entry_ts_ms,
                "exit_price": getattr(trade, "exit_price", None),
                "exit_reason": getattr(trade, "exit_reason", None),
                "hold_sec": getattr(trade, "hold_sec", None),
                "pnl_pct": getattr(trade, "pnl_pct", None),
                "mfe_pct": round(pos.mfe_pct, 3),
                "mae_pct": round(pos.mae_pct, 3),
                "peak_price": pos.obs_peak_price,
                "peak_ts": pos.obs_peak_ts_ms,
                "trough_price": pos.obs_trough_price,
                "trough_ts": pos.obs_trough_ts_ms,
                "time_to_peak_ms": ttp,
                "early_ticks": pos.early_ticks,
            })
        except Exception:
            log.debug("exit profile log hatası", exc_info=True)

    def _pair_features(self, pair: Pair) -> dict:
        """Aday/giriş için ham sayısal özellikler — eşik kalibrasyonu için."""
        return {
            "liquidity": round(pair.liquidity_usd, 2),
            "market_cap": round(pair.market_cap_usd, 2),
            "price": pair.price_usd,
            "chg_m5": pair.chg_m5,
            "chg_h1": pair.chg_h1,
            "chg_h24": pair.chg_h24,
            "vol_m5": round(pair.vol_m5, 2),
            "vol_h1": round(pair.vol_h1, 2),
            "vol_h24": round(pair.vol_h24, 2),
            "txns_h1": pair.txns_h1,
            "txns_m5": pair.txns_m5,
            "boost_score": int(getattr(pair, "boost_score", 0) or 0),
            "token_age_h": pool_age_hours(pair),
            "source": pair.discovery_source or "geckoterminal",
        }

    def _deploy_pct(self) -> float:
        locked = sum(p.cost_usd for p in self.broker.positions)
        equity = getattr(self.broker, "balance", 0.0) + locked
        return round(100 * locked / equity, 1) if equity > 0 else 0.0

    def _log_reject(
        self,
        pair: Pair,
        skor: float,
        reason: str,
        reject_type: str,
        *,
        cooldown_sec: float = 600.0,
        extra: dict | None = None,
    ) -> None:
        """Reddedilen/kaçırılan aday — token+tür başına dedup ile decisions.jsonl."""
        key = (pair.token_address, reject_type)
        now = time.time()
        last = self._decision_logged.get(key, 0.0)
        if now - last < cooldown_sec:
            return
        self._decision_logged[key] = now
        regime, fg, macro = self._regime_stamp()
        row = {
            "pair": pair.name,
            "chain": pair.chain,
            "token_address": pair.token_address,
            "pool_address": pair.pool_address,
            "score": round(skor, 1),
            "reason": reason,
            "reject_type": reject_type,
            "open_pos_count": self._entry_open_count(),
            "deploy_pct": self._deploy_pct(),
            "regime": regime,
            "fear_greed": fg,
            "macro_avg": macro,
            "features": self._pair_features(pair),
        }
        if extra:
            row.update(extra)
        telemetry.log_decision(row)

    def _liquidity_for_pool(self, pool_address: str) -> float:
        for _s, p in self.watchlist:
            if p.pool_address == pool_address:
                return p.liquidity_usd
        return 100_000.0

    def _prioritized_watchlist(
        self, ranked: list[tuple[float, Pair]], limit: int = 15
    ) -> list[tuple[float, Pair]]:
        """Solana önce; yalnız entry_chains içindeki çiftler."""
        allowed = set(self.settings.entry_chains)
        ranked = [x for x in ranked if x[1].chain in allowed]
        sol = sorted(
            [x for x in ranked if x[1].chain == "solana"],
            key=lambda x: watchlist_sort_key(x[0], x[1]),
        )
        rest = sorted(
            [x for x in ranked if x[1].chain != "solana"],
            key=lambda x: (
                CHAIN_ENTRY_PRIORITY.get(x[1].chain, 99),
                *watchlist_sort_key(x[0], x[1]),
            ),
        )
        return (sol + rest)[:limit]

    def manual_close_position(self, pool_address: str, fraction: float = 1.0) -> dict:
        """Panel — kullanıcı manuel kapat / kısmi sat."""
        with self._broker_lock:
            pos = next(
                (p for p in self.broker.positions if p.pool_address == pool_address),
                None,
            )
            if pos is None:
                raise ValueError("Açık pozisyon bulunamadı")
            price = self._last_prices.get(pool_address, pos.entry_price)
            liq = self._liquidity_for_pool(pool_address)
            reason = "manual: kullanıcı kapattı" if fraction >= 0.999 else f"manual: kullanıcı %{int(fraction * 100)} sat"
            try:
                if fraction >= 0.999:
                    trade = self.broker.sell(pos, price, liq, reason)
                    action = "exit"
                else:
                    trade = self.broker.sell_partial(pos, fraction, price, liq, reason)
                    action = "exit_partial"
            except PhantomPendingTrade:
                return {"ok": True, "pending": True, "pair": pos.pair_name, "message": "Phantom imza bekleniyor"}
            self._daily_pnl += trade.pnl_usd
            if fraction >= 0.999:
                self._apply_pair_cooldown(pos, trade.pnl_usd)
                self._log_exit_profile(pos, trade)
            telemetry.log_event(
                "MONEY", f"SELL {pos.pair_name} pnl ${trade.pnl_usd:+.2f} — {reason}",
                trade_id=pos.trade_id, pair=pos.pair_name, pnl_usd=round(trade.pnl_usd, 2),
                reason=reason, partial=(fraction < 0.999), manual=True,
            )
            pnl_pct = 100 * trade.pnl_usd / max(trade.cost_usd, 0.01)
            self._record_decision(
                Decision(action, reason, pos.pair_name, pos.entry_score, pnl_pct, fraction if action == "exit_partial" else None)
            )
            return {
                "ok": True,
                "pair": pos.pair_name,
                "pnl_usd": trade.pnl_usd,
                "fraction": fraction,
            }

    def _active_live_chains(self) -> set[str] | None:
        if self.settings.mode != "live":
            return None
        chains = set(self.settings.live_chains())
        if getattr(self.broker, "phantom_pubkey", None):
            chains.add("solana")
        return chains

    def _entry_open_count(self) -> int:
        return sum(1 for p in self.broker.positions if self.settings.entry_allowed(p.chain))

    def _collect_entry_candidates(
        self, ranked: list[tuple[float, Pair]], client: httpx.Client
    ) -> list[tuple]:
        held = {p.token_address for p in self.broker.positions}
        live_chains = self._active_live_chains()
        entry_min = self.policy.effective_entry_min(self._macro_avg, self._brain_entry_penalty())
        conf_snap = self._confluence_snapshot()
        candidates: list[tuple] = []
        scan_limit = int(os.getenv("ENTRY_CANDIDATE_LIMIT", "25"))

        for skor, pair in ranked[:scan_limit]:
            if not self.settings.entry_allowed(pair.chain):
                continue
            if pair.token_address in held:
                continue
            if self._pair_cooldown.on_cooldown_pair(pair):
                continue
            if live_chains is not None and pair.chain not in live_chains:
                continue

            genesis_ok, _gen_note = pump_entry_ok(pair)
            if is_trending_late_pump(pair):
                continue

            pair_min = entry_min
            if genesis_ok:
                pair_min = min(entry_min, float(os.getenv("GENESIS_ENTRY_MIN", "52")))
            if skor < pair_min:
                continue

            # Genesis/paper: aday taramada RPC yapma — tick dakikalarca kilitlenmesin
            sm_count, sm_src = wallet_buyer_info(pair, client=client)
            if genesis_ok:
                sm_ok, sm_note = True, f"genesis · {sm_count} cüzdan ({sm_src})"
            elif not self.policy.require_smart_money:
                sm_ok, sm_note = True, f"proxy {sm_count} ({sm_src})"
            else:
                sm_ok, sm_note = smart_money_entry_ok(
                    pair, self.policy.min_alpha_wallets, client=client
                )
            sym = pair.name.split("/")[0].strip().upper() if "/" in pair.name else pair.name[:12].upper()
            whale_row = next(
                (w for w in self._whale_signals if str(w.get("symbol", "")).upper() == sym),
                None,
            )
            pump = analyze_pump_pair(pair, wallet_count=sm_count, whale_row=whale_row)
            ds_sig = evaluate_trending(pair)
            founder_fast = founder_fast_entry_ok(pair, pump)
            ds_ok = (trending_fast_enabled() and ds_sig.entry_ok) or founder_fast or genesis_ok
            conf = compute_trade_confluence(
                skor,
                pair,
                conf_snap,
                entry_min=entry_min,
                smart_money_ok=sm_ok,
                smart_money_count=sm_count,
                moonshot_score=pump.moonshot_score,
                founder_fast=founder_fast,
                genesis_ok=genesis_ok,
            )
            if self.settings.confluence_required and not conf.enter_ok:
                self._log_reject(
                    pair, skor, conf.blocker or "konfluans yetersiz", "filter",
                    extra={"conf_score": round(conf.score, 1), "smart_money_count": sm_count},
                )
                continue
            candidates.append((conf.score, skor, pair, conf, sm_ok, sm_note, sm_count, ds_ok, ds_sig, pump))

        candidates.sort(
            key=lambda x: (
                CHAIN_ENTRY_PRIORITY.get(x[2].chain, 99),
                0 if pump_entry_ok(x[2])[0] else 1,
                1 if int(getattr(x[2], "boost_score", 0) or 0) >= 500 else 0,
                -1 if x[9].whale_signal else 0,
                -1 if (x[9].age_hours or 999) <= 6 else 0,
                -1 if x[9].moonshot_score >= moonshot_min_score() else 0,
                -x[0],
                -x[1],
                -x[8].score,
            )
        )
        return candidates

    def _attempt_entry_buy(
        self,
        cand: tuple,
        client: httpx.Client,
        *,
        open_count_override: int | None = None,
        validate_only: bool = False,
    ) -> bool:
        conf_score, skor, pair, conf, sm_ok, sm_note, _sm_count, ds_ok, ds_sig, pump = cand
        live_chains = self._active_live_chains()
        position_usd = compute_position_usd(
            self.settings, self.broker, pair, max_open=self.policy.max_open_positions
        )
        slip_pct, _ = estimate_entry_slippage_pct(client, pair, position_usd, self.settings)
        cex_hold = cex_hold_score(pair, self._cex_scores())
        founder_fast = founder_fast_entry_ok(pair, pump)
        genesis_ok, gen_note = pump_entry_ok(pair)
        trending_ok = (
            (trending_fast_enabled() and ds_sig.entry_ok) or founder_fast or genesis_ok
        )

        report = self._check_safety(client, pair.chain, pair.token_address)
        safety_ok, safety_note = entry_safety_ok(report, genesis_ok=genesis_ok)
        open_count = (
            open_count_override if open_count_override is not None else self._entry_open_count()
        )
        decision = evaluate_entry(
            skor,
            pair,
            position_usd,
            self.policy,
            safety_ok=safety_ok,
            kill_switch=is_active(),
            open_count=open_count,
            daily_pnl=self._daily_pnl,
            daily_loss_limit=self.settings.daily_loss_limit_usd,
            already_held=False,
            live_allowed=live_chains is None or pair.chain in live_chains,
            macro_avg=self._macro_avg,
            brain_penalty=self._brain_entry_penalty(),
            quote_slippage_pct=slip_pct,
            smart_money_ok=sm_ok,
            smart_money_note=sm_note,
            cex_hold_score=cex_hold,
            trending_ok=trending_ok,
            genesis_ok=genesis_ok,
        )
        if decision.action != "enter":
            if skor >= self.policy.entry_score_min:
                self._record_decision(decision)
            if not validate_only:
                self._log_reject(
                    pair, skor, decision.reason or "entry gate", "filter",
                    extra={"safety_ok": safety_ok, "slip_pct": round(slip_pct, 3),
                           "smart_money_count": _sm_count},
                )
            return False
        if validate_only:
            return True

        px_decision = pair.price_usd
        decided_ts = time.time()
        try:
            reason = f"{decision.reason} · {conf.summary()}"
            if pump.moonshot_score >= moonshot_min_score():
                reason += f" · moon {pump.moonshot_score:.0f}"
            if founder_fast:
                reason += " · founder fast"
            if genesis_ok:
                reason += f" · {gen_note}"
            if safety_note and not report.ok:
                reason += f" · {safety_note}"
            if ds_sig.entry_ok and trending_fast_enabled():
                reason += f" · {ds_sig.reason}"
            bought = self.broker.buy(pair, position_usd, skor)
        except PhantomPendingTrade:
            log.info("Phantom alım imzası bekleniyor: %s", pair.name)
            self._record_decision(
                Decision("enter", f"{decision.reason} · phantom imza bekliyor", pair.name, conf_score)
            )
            return True
        except ValueError as e:
            log.warning("Alım başarısız: %s", e)
            self._record_decision(Decision("skip", str(e), pair.name, skor))
            msg = str(e).lower()
            if "bakiye" in msg or "yetersiz" in msg:
                self._log_reject(pair, skor, str(e), "no_capital", cooldown_sec=300)
            return False
        self._last_prices[pair.pool_address] = pair.price_usd
        pos = bought if isinstance(bought, Position) else None
        if pos is not None:
            try:
                self._log_entry_attribution(
                    pos, pair, px_decision=px_decision, decided_ts=decided_ts,
                    skor=skor, conf=conf, sm_count=_sm_count, slip_pct=slip_pct,
                    cex_hold=cex_hold, ds_sig=ds_sig, ds_ok=ds_ok, genesis_ok=genesis_ok,
                    safety_ok=safety_ok, report=report, pump=pump, position_usd=position_usd,
                )
            except Exception:
                log.debug("attribution log hatası", exc_info=True)
            telemetry.log_event(
                "MONEY", f"BUY {pair.name} ${position_usd:.2f}",
                trade_id=pos.trade_id, pair=pair.name, chain=pair.chain,
                usd=round(position_usd, 2), score=round(skor, 1), source=pos.discovery_source,
            )
        self._record_decision(Decision("enter", reason, pair.name, conf_score))
        return True

    def _log_entry_attribution(
        self,
        pos: Position,
        pair: Pair,
        *,
        px_decision: float,
        decided_ts: float,
        skor: float,
        conf,
        sm_count: int,
        slip_pct: float,
        cex_hold: float,
        ds_sig,
        ds_ok: bool,
        genesis_ok: bool,
        safety_ok: bool,
        report,
        pump,
        position_usd: float,
    ) -> None:
        """Giriş anı damgası: pos'a gecikme/rejim yaz + attribution.jsonl snapshot."""
        fill = pos.entry_price
        pos.px_decision = px_decision
        pos.decision_to_entry_sec = round(max(0.0, time.time() - decided_ts), 3)
        pos.entry_drift_pct = round((fill / px_decision - 1) * 100, 3) if px_decision > 0 else 0.0
        regime, fg, macro = self._regime_stamp()
        pos.entry_regime = regime
        pos.entry_fear_greed = fg
        pos.entry_macro_avg = macro
        bv = self._brain_verdict
        row = {
            "trade_id": pos.trade_id,
            "pair": pair.name,
            "chain": pair.chain,
            "token_address": pair.token_address,
            "pool_address": pair.pool_address,
            "source": pos.discovery_source,
            "score": round(skor, 1),
            "conf_score": round(getattr(conf, "score", 0.0), 1),
            "conf_layers": getattr(conf, "layers", {}),
            "conf_breakdown": getattr(conf, "breakdown", {}),
            "conf_blocker": getattr(conf, "blocker", None),
            "pump": {
                "moonshot_score": round(getattr(pump, "moonshot_score", 0.0), 1),
                "turnover": round(getattr(pump, "turnover", 0.0), 2),
                "age_hours": getattr(pump, "age_hours", None),
                "wallet_count": getattr(pump, "wallet_count", 0),
                "whale_signal": getattr(pump, "whale_signal", False),
            },
            "slip_pct": round(slip_pct, 3),
            "ds_score": round(getattr(ds_sig, "score", 0.0), 1),
            "ds_entry_ok": bool(getattr(ds_sig, "entry_ok", False)),
            "ds_ok": bool(ds_ok),
            "genesis_ok": bool(genesis_ok),
            "safety_ok": bool(safety_ok),
            "regime": regime,
            "fear_greed": fg,
            "macro_avg": macro,
            "action_bias": getattr(bv, "action_bias", None) if bv is not None else None,
            "holder": {
                "top1_pct": report.metrics.get("top1_holder_pct"),
                "top10_pct": report.metrics.get("top10_holder_pct"),
                "insider_count": report.metrics.get("insider_count"),
            },
            "mint_revoked": report.metrics.get("mint_revoked"),
            "freeze_revoked": report.metrics.get("freeze_revoked"),
            "rugcheck_score": report.metrics.get("rugcheck_score"),
            "token_age_h": pool_age_hours(pair),
            "liquidity": round(pair.liquidity_usd, 2),
            "market_cap": round(pair.market_cap_usd, 2),
            "traders_h1": pair.txns_h1,
            "smart_money_count": sm_count,
            "cex_hold": round(cex_hold, 1),
            "position_usd": round(position_usd, 2),
            "price_at_decision": px_decision,
            "price_at_entry": fill,
            "decision_to_entry_sec": pos.decision_to_entry_sec,
            "entry_drift_pct": pos.entry_drift_pct,
        }
        telemetry.log_attribution(row)

    def _try_slot_rotation(
        self,
        pairs: list[Pair],
        ranked: list[tuple[float, Pair]],
        best: tuple,
        client: httpx.Client,
    ) -> None:
        conf_score, skor, pair, _conf, *_rest = best
        score_map = {p.pool_address: s for s, p in ranked}
        picked = pick_weakest_hold(
            list(self.broker.positions),
            dex_scores=score_map,
            last_prices=self._last_prices,
        )
        if not picked:
            return
        victim, weak_score = picked
        ok, note = should_rotate(weak_score, conf_score, skor)
        if not ok:
            log.debug("Slot rotation atlandı: %s", note)
            return
        if not self._attempt_entry_buy(
            best,
            client,
            open_count_override=self._entry_open_count() - 1,
            validate_only=True,
        ):
            return

        victim_pair = self._pair_by_pool(pairs, victim.pool_address)
        price = self._last_prices.get(victim.pool_address, victim.entry_price)
        liq = victim_pair.liquidity_usd if victim_pair else self._liquidity_for_pool(victim.pool_address)
        reason = f"slot rotate → {pair.name.split('/')[0].strip()}: {note}"
        try:
            trade = self.broker.sell(victim, price, liq, reason)
        except PhantomPendingTrade:
            log.info("Phantom rotation satış bekliyor: %s", victim.pair_name)
            return
        self._daily_pnl += trade.pnl_usd
        self._apply_pair_cooldown(victim, trade.pnl_usd)
        self._log_exit_profile(victim, trade)
        telemetry.log_event(
            "MONEY", f"SELL {victim.pair_name} pnl ${trade.pnl_usd:+.2f} — {reason}",
            trade_id=victim.trade_id, pair=victim.pair_name, pnl_usd=round(trade.pnl_usd, 2),
            reason=reason, rotation=True,
        )
        self._record_decision(
            Decision(
                "exit",
                reason,
                victim.pair_name,
                victim.entry_score,
                trade.pnl_usd / max(trade.cost_usd, 0.01) * 100,
            )
        )
        if self._attempt_entry_buy(best, client):
            log.info("Slot rotation: %s → %s (%s)", victim.pair_name, pair.name, note)

    def _try_entry(
        self, pairs: list[Pair], ranked: list[tuple[float, Pair]], client: httpx.Client
    ) -> None:
        if is_active():
            return
        slots_full = self._entry_open_count() >= self.policy.max_open_positions
        if slots_full and not slot_rotation_enabled():
            return
        if self._daily_pnl <= -self.settings.daily_loss_limit_usd:
            if not self._daily_halt_logged:
                self._daily_halt_logged = True
                telemetry.log_event(
                    "SYSTEM", f"gunluk zarar limiti asildi — giris durdu (pnl ${self._daily_pnl:.2f})",
                    daily_pnl=round(self._daily_pnl, 2), limit=self.settings.daily_loss_limit_usd,
                )
            return

        candidates = self._collect_entry_candidates(ranked, client)
        if not candidates:
            return

        if slots_full:
            # İyi aday geldi ama slot dolu — aşırı işlemin gerçek bedeli (kaçan kazanan)
            best = candidates[0]
            self._log_reject(
                best[2], best[1], "slot dolu — rotasyon denenecek", "no_slot",
                cooldown_sec=300,
                extra={"conf_score": round(best[0], 1), "max_open": self.policy.max_open_positions},
            )
            self._try_slot_rotation(pairs, ranked, candidates[0], client)
            return

        for cand in candidates:
            if self._attempt_entry_buy(cand, client):
                return

    def tick(self) -> None:
        try:
            self._tick_started_ts = time.time()
            self._reset_daily_if_needed()
            self._refresh_macro_if_needed()
            self._refresh_brain_if_needed()
            if is_active() and self.broker.positions:
                pairs = scan_all(self.settings.scan_chains)
                ranked = self._rank_pairs(pairs)
                self._refresh_open_position_prices()
                self._exit_positions(pairs, ranked)
                return
            pairs = scan_all(self.settings.scan_chains)
            live_ok = self.settings.mode != "live" or bool(self._active_live_chains())
            with httpx.Client() as client:
                ranked = self._rank_pairs(pairs, client=client)
                self.watchlist = self._prioritized_watchlist(ranked, 20)
                self._refresh_holds_if_needed(pairs, client=client)
                self._prefetch_safety(client, ranked)
                self._refresh_entry_diagnostics(ranked, live_allowed=live_ok, client=client)
                self._refresh_growth_watchlist(ranked)
                self._refresh_open_position_prices()
                self._exit_positions(pairs, ranked)
                if not is_active():
                    self._try_entry(pairs, ranked, client)
        except Exception as exc:
            log.exception("tick hatası")
            telemetry.log_event("ERROR", f"tick hatasi: {type(exc).__name__}: {exc}")

    def run_forever(self) -> None:
        ep = self._exit_policy()
        log.info(
            "Motor başladı — %s mod, %d sn, giriş≥%.0f çıkış<%.0f SL%.0f%% ladder TP+%.0f/+%.0f%s",
            self.settings.mode,
            SCAN_INTERVAL_SEC,
            self.policy.entry_score_min,
            self.policy.exit_score_max,
            ep.stop_loss_pct,
            ep.tp1_pct,
            ep.tp2_pct,
            " · Helius alpha ON" if self.settings.alpha_on_chain() else "",
        )
        alpha = self._alpha_tracking_state()
        if alpha.get("rpc_fallback"):
            log.info(
                "Alpha RPC fallback ON — %d cüzdan (Helius yok, public RPC)",
                alpha.get("alpha_wallets", 0),
            )
        elif not alpha.get("on_chain"):
            log.warning(
                "Alpha kapalı — ALPHA_RPC_FALLBACK=1 (varsayılan) veya ücretli Helius key"
            )
        telemetry.log_event(
            "SYSTEM", f"motor basladi — {self.settings.mode} mod",
            mode=self.settings.mode, scan_chains=list(self.settings.scan_chains),
            daily_limit=self.settings.daily_loss_limit_usd,
        )
        while True:
            self.tick()
            time.sleep(SCAN_INTERVAL_SEC)

    def decision_state(self) -> dict:
        d = self.last_decision
        ep = self._exit_policy()
        return {
            "policy": self.policy.summary(),
            "exit_policy": ep.summary(),
            "confluence_min": self.settings.confluence_min,
            "confluence_min_layers": self.settings.confluence_min_layers,
            "confluence_required": self.settings.confluence_required,
            "aggressive": _aggressive_trading(self.settings),
            "macro_avg": self._macro_avg,
            "macro_updated_at": self._macro_updated_at or None,
            "daily_pnl": round(self._daily_pnl, 2),
            "brain_penalty": self._brain_entry_penalty(),
            "position_usd_base": self.settings.max_position_usd,
            "entry_chains": list(self.settings.entry_chains),
            "scan_chains": list(self.settings.scan_chains),
            "last": None
            if d is None
            else {
                "action": d.action,
                "reason": d.reason,
                "pair": d.pair,
                "score": d.score,
                "pnl_pct": d.pnl_pct,
                "sell_fraction": d.sell_fraction,
            },
        }

    def _refresh_growth_watchlist(self, ranked: list[tuple[float, Pair]]) -> None:
        snap = self._confluence_snapshot()
        conf_by_pool: dict[str, float] = {}
        entry_min = self.policy.effective_entry_min(self._macro_avg, self._brain_entry_penalty())
        for skor, pair in ranked[:20]:
            conf = compute_trade_confluence(
                skor,
                pair,
                snap,
                entry_min=entry_min,
                smart_money_ok=False,
            )
            conf_by_pool[pair.pool_address] = conf.score

        self._growth_watchlist = build_growth_watchlist(
            ranked,
            cex_scores=self._cex_scores(),
            whale_signals=self._whale_signals,
            brain_tam=snap.brain_tam,
            brain_top=snap.brain_top,
            confluence_by_pool=conf_by_pool,
            binance_holds=self._binance_holds,
            okx_holds=self._okx_holds,
            limit=12,
        )

    def growth_state(self) -> dict:
        return {
            "rows": self._growth_watchlist,
            "count": len(self._growth_watchlist),
            "updated_at": self._holds_updated_at or None,
        }

    def entry_diagnostics_state(self) -> dict:
        passing = sum(1 for r in self._entry_diagnostics if r.get("would_enter"))
        return {
            "updated_tick": self._tick_count,
            "candidates": len(self._entry_diagnostics),
            "would_enter_count": passing,
            "rows": self._entry_diagnostics,
        }
