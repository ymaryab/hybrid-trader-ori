"""FastAPI web panel — paper/live motor durumu ve işlem geçmişi."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import httpx

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from web3 import Web3

from hibrit_trader.advanced_scan import list_modes, run_advanced_scan
from hibrit_trader.broker import make_broker
from hibrit_trader.config import CHAIN_ENTRY_PRIORITY, SUPPORTED_CHAINS, Settings
from hibrit_trader.cex_confluence import pair_base_symbol
from hibrit_trader.evm_balances import fetch_portfolio
from hibrit_trader.killswitch import activate, deactivate, is_active
from hibrit_trader.live_sim import enrich_position, live_sim_summary
from hibrit_trader.paper import PaperBroker
from hibrit_trader.phantom_broker import PhantomLiveBroker
from hibrit_trader.phantom_trade import phantom_queue
from hibrit_trader.pump_research import analyze_pump_pair
from hibrit_trader.session import Engine
from hibrit_trader.smart_money import wallet_buyer_info
from hibrit_trader.solana_wallet import fetch_sol_portfolio, is_valid_solana_address

settings = Settings.from_env()
broker = make_broker(settings)
engine = Engine(settings, broker)
_phantom_session: str | None = None

app = FastAPI(title="Hybrid Trade")


def _trades_path() -> Path:
    return Path("data/live_trades.jsonl" if settings.mode == "live" else "data/trades.jsonl")


def _chain_opportunities(watchlist: list[dict]) -> list[dict]:
    """Ağları en yüksek fırsat skoruna göre sırala (ENTRY_CHAINS ile sınırlı)."""
    allowed = set(settings.entry_chains)
    stats: dict[str, dict] = {
        chain: {"best": 0, "count": 0, "top": None}
        for chain in SUPPORTED_CHAINS
        if chain in allowed
    }
    for w in watchlist:
        chain = w["chain"]
        if chain not in stats:
            stats[chain] = {"best": 0, "count": 0, "top": None}
        stats[chain]["count"] += 1
        if w["score"] > stats[chain]["best"]:
            stats[chain]["best"] = w["score"]
            stats[chain]["top"] = w["name"]
    ranked = sorted(
        stats.items(),
        key=lambda x: (-x[1]["best"], CHAIN_ENTRY_PRIORITY.get(x[0], 99), x[0]),
    )
    return [
        {
            "chain": chain,
            "best_score": info["best"],
            "count": info["count"],
            "top_pair": info["top"],
        }
        for chain, info in ranked
    ]


def _restore_phantom_session() -> None:
    global _phantom_session
    pk = getattr(broker, "phantom_pubkey", None)
    if pk:
        _phantom_session = pk


@app.on_event("startup")
def _start_engine() -> None:
    if os.getenv("STRATEGY", "").strip().lower() == "momentum":
        # AYRI kod yolu: momentum paper modu. Normal engine döngüsü BAŞLATILMAZ
        # (paper_state'e yazılmaz); sadece momentum_* dosyalarına yazar.
        if os.getenv("V2_ENABLED", "1") != "0":
            from hibrit_trader.momentum_session import MomentumEngine
            mom = MomentumEngine(settings)
            threading.Thread(target=mom.run_forever, daemon=True).start()
        if os.getenv("GOLGE_ENABLED", "1") != "0":
            # Gölge senaryo: tamamen sanal, golge_* dosyalarına yazar (v2'ye dokunmaz)
            from hibrit_trader.golge_session import GolgeEngine
            golge = GolgeEngine(settings)
            threading.Thread(target=golge.run_forever, daemon=True).start()
        if os.getenv("V3_ENABLED", "1") != "0":
            # V3 senaryo: tamamen sanal, v3_* dosyalarına yazar (v2 + gölgeye dokunmaz)
            from hibrit_trader.v3_session import V3Engine
            v3 = V3Engine(settings)
            threading.Thread(target=v3.run_forever, daemon=True).start()
        if os.getenv("V4_ENABLED", "1") != "0":
            # V4 melez senaryo: tamamen sanal, v4_* dosyalarına yazar
            from hibrit_trader.v4_session import V4Engine
            v4 = V4Engine(settings)
            threading.Thread(target=v4.run_forever, daemon=True).start()
        if os.getenv("V5_ENABLED", "1") != "0":
            # V5 senaryo: gölgenin veri-dayalı yükseltmesi, v5_* dosyalarına yazar
            from hibrit_trader.v5_session import V5Engine
            v5 = V5Engine(settings)
            threading.Thread(target=v5.run_forever, daemon=True).start()
        if os.getenv("V6_ENABLED", "1") != "0":
            # V6 senaryo: arındırılmış gölge (h1 bandı 10..50), v6_* dosyalarına yazar
            from hibrit_trader.v6_session import V6Engine
            v6 = V6Engine(settings)
            threading.Thread(target=v6.run_forever, daemon=True).start()
        if os.getenv("V7_ENABLED", "1") != "0":
            # V7 senaryo: v6 + -%10 felaket freni, v7_* dosyalarına yazar
            from hibrit_trader.v7_session import V7Engine
            v7 = V7Engine(settings)
            threading.Thread(target=v7.run_forever, daemon=True).start()
        if os.getenv("V8_ENABLED", "1") != "0":
            # V8 senaryo: gölge + 200k/20..50/tp3/20dk mutlak tavan, v8_* dosyalarına yazar
            from hibrit_trader.v8_session import V8Engine
            v8 = V8Engine(settings)
            threading.Thread(target=v8.run_forever, daemon=True).start()
        if os.getenv("V9_ENABLED", "1") != "0":
            # V9 senaryo: v7 + TEK fark liq tabanı $300k, v9_* dosyalarına yazar
            from hibrit_trader.v9_session import V9Engine
            v9 = V9Engine(settings)
            threading.Thread(target=v9.run_forever, daemon=True).start()
        if os.getenv("X1_ENABLED", "1") != "0":
            # X1 senaryo: kosucu avcisi (h1>=50, trail -18, 6sa), x1_* dosyalarina yazar
            from hibrit_trader.x1_session import X1Engine
            x1 = X1Engine(settings)
            threading.Thread(target=x1.run_forever, daemon=True).start()
        if os.getenv("V10_ENABLED", "1") != "0":
            # V10 senaryo: saf tp_2 (stop/timeout/rejim/cooldown yok), v10_* dosyalarina yazar
            from hibrit_trader.v10_session import V10Engine
            v10 = V10Engine(settings)
            threading.Thread(target=v10.run_forever, daemon=True).start()
        if os.getenv("M1_ENABLED", "1") != "0":
            # M1 senaryo: major evren (v7 iskeleti, olcekli), m1_* dosyalarina yazar
            from hibrit_trader.m1_session import M1Engine
            m1 = M1Engine(settings)
            threading.Thread(target=m1.run_forever, daemon=True).start()
        if os.getenv("M2_ENABLED", "1") != "0":
            # M2 senaryo: major evren + saf tp (v10 iskeleti), m2_* dosyalarina yazar
            from hibrit_trader.m2_session import M2Engine
            m2 = M2Engine(settings)
            threading.Thread(target=m2.run_forever, daemon=True).start()
        if os.getenv("V7C_ENABLED", "1") != "0":
            # V7C senaryo: v7 kurallari birebir, tek fark major evren (liq>=$3M);
            # SABIT PAPER (BROKER_MODE'dan bagimsiz), v7c_* dosyalarina yazar
            from hibrit_trader.v7c_session import V7CEngine
            v7c = V7CEngine(settings)
            threading.Thread(target=v7c.run_forever, daemon=True).start()
        if os.getenv("EKG_ENABLED", "1") != "0":
            # Koşucu EKG: pasif gözlemci, işlem yok, kosucu_ekg* dosyalarına yazar
            from hibrit_trader.kosucu_ekg import KosucuEkg
            ekg = KosucuEkg(settings)
            threading.Thread(target=ekg.run_forever, daemon=True).start()
        if os.getenv("PROBE_ENABLED", "1") != "0":
            # Makas probe: motorsuz round-trip makas olcumu (canary on-sart verisi)
            from hibrit_trader import makas_probe
            threading.Thread(target=makas_probe.run_forever, daemon=True).start()
        if os.getenv("DENETIM_ENABLED", "1") != "0":
            # Denetim: ay devrinde kapali islem defterini CSV'ye yazar
            from hibrit_trader import denetim
            threading.Thread(target=denetim.run_forever, daemon=True).start()
        if (os.getenv("CANLI_GOSTERGE_ENABLED", "1") != "0"
                and os.getenv("BROKER_MODE", "paper").strip().lower() == "live"):
            # Canli cuzdan gostergesi: SOL bakiye + acik canli poz degeri, kendi
            # dongusunde (RPC kotasi panel poll'undan bagimsiz), sadece okur.
            # Kilit kapaliyken de calisir: kilitli kart MTM/SOL gosterebilsin.
            from hibrit_trader import canli_gosterge
            threading.Thread(target=canli_gosterge.run_forever, daemon=True).start()
        return
    _restore_phantom_session()
    sorunlar = settings.validate()
    if settings.mode == "live" and sorunlar:
        return
    t = threading.Thread(target=engine.run_forever, daemon=True)
    t.start()
    if os.getenv("HIBRIT_BRAIN_AUTO", "1") != "0":
        engine.schedule_brain_startup()


def _canli_bagli_mi() -> bool:
    """BROKER_MODE=live + kilit acik mi? Startup ve sayfa render ortak kontrolu."""
    if os.getenv("BROKER_MODE", "paper").strip().lower() != "live":
        return False
    from hibrit_trader.broker import live_kilit_acik
    try:
        return live_kilit_acik()
    except Exception:
        return False


def _liquidity_for_pool(pool_address: str) -> float:
    for _s, p in engine.watchlist:
        if p.pool_address == pool_address:
            return p.liquidity_usd
    return 100_000.0


def _enrich_summary(summary: dict, positions: list[dict]) -> dict:
    """Mark-to-market equity + oturum P&L (gas dahil nakit bazlı)."""
    unrealized = 0.0
    position_mv = 0.0
    for p in positions:
        pu = (
            p["exit_quote_pnl"]
            if p.get("exit_quote_pnl") is not None
            else p.get("unrealized_pnl", 0.0)
        )
        unrealized += float(pu or 0.0)
        position_mv += float(p.get("cost_usd") or 0.0) + float(pu or 0.0)
    out = dict(summary)
    bal = float(out.get("balance") or 0.0)
    equity = bal + position_mv
    start = out.get("start_balance_usd")
    out["unrealized_pnl"] = round(unrealized, 2)
    out["deployed_usd"] = round(
        sum(float(p.get("cost_usd") or 0.0) for p in positions), 2
    )
    out["positions_value_usd"] = round(position_mv, 2)
    out["equity"] = round(equity, 2)
    if start is not None:
        session = round(equity - float(start), 2)
        out["session_pnl"] = session
        out["session_pnl_pct"] = round(session / float(start) * 100, 2) if start else 0.0
    return out


@app.get("/api/state")
def api_state() -> dict:
    positions = []
    with httpx.Client() as client:
        for pos in broker.positions:
            fallback = engine._last_prices.get(pos.pool_address, pos.entry_price)
            liq = _liquidity_for_pool(pos.pool_address)
            if settings.mode == "paper" or settings.paper_live_quotes:
                positions.append(
                    enrich_position(pos, fallback, liq, client, settings)
                )
            else:
                pnl = broker.unrealized_pnl(pos, fallback)
                positions.append({
                    "pair": pos.pair_name,
                    "chain": pos.chain,
                    "entry_price": round(pos.entry_price, 6),
                    "current_price": round(fallback, 6),
                    "cost_usd": round(pos.cost_usd, 2),
                    "unrealized_pnl": round(pnl, 2),
                    "entry_score": pos.entry_score,
                    "opened_at": pos.opened_at,
                    "trade_type": "live",
                    "prices_live": True,
                })
    watchlist = []
    now = time.time()
    whale_idx = {
        str(w.get("symbol", "")).upper(): w for w in engine._whale_signals
    }
    for s, p in engine.watchlist:
        age_h = None
        if p.pool_created_at:
            age_h = max(0.0, (now - p.pool_created_at) / 3600.0)
        sym = pair_base_symbol(p)
        whale_row = whale_idx.get(sym)
        if whale_row:
            wallets = int(whale_row.get("wallet_count", 0))
            wallet_src = whale_row.get("wallet_source", "proxy")
        else:
            wallets, wallet_src = wallet_buyer_info(p, client=None)
        pump = analyze_pump_pair(p, wallet_count=wallets, whale_row=whale_row)
        from hibrit_trader.early_launch import classify_pump_window

        pw = classify_pump_window(p)
        row = {
            "score": s,
            "chain": p.chain,
            "name": p.name,
            "liquidity_usd": round(p.liquidity_usd),
            "market_cap_usd": round(getattr(p, "market_cap_usd", 0) or 0),
            "vol_h24": round(p.vol_h24),
            "txns_h24": getattr(p, "txns_h24", 0) or p.txns_h1,
            "boost_score": getattr(p, "boost_score", 0),
            "chg_m5": round(p.chg_m5, 1),
            "chg_h1": round(p.chg_h1, 1),
            "chg_h24": round(p.chg_h24, 1),
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "wallet_on_chain": wallet_src in ("helius", "rpc"),
            "wallet_source": wallet_src,
            "pump_window": pw["window"],
            "pump_label": pw["label"],
            "pump_action": pw["action"],
            "genesis_score": pw["genesis_score"],
        }
        row.update(pump.to_dict())
        watchlist.append(row)
    summary = _enrich_summary(broker.summary(), positions)
    chain_ops = _chain_opportunities(watchlist)
    return {
        "mode": settings.mode,
        "kill_switch": is_active(),
        "balance": summary.get("balance"),
        "summary": summary,
        "positions": positions,
        "watchlist": watchlist,
        "chain_opportunities": chain_ops,
        "decision": engine.decision_state(),
        "brain": engine.brain_state(),
        "live_sim": live_sim_summary(settings),
        "market_intel": engine.market_intel_state(),
        "entry_diagnostics": engine.entry_diagnostics_state(),
        "growth_potential": engine.growth_state(),
        "phantom": {
            "connected": bool(
                getattr(broker, "phantom_pubkey", None) or _phantom_session
            ),
            "address": getattr(broker, "phantom_pubkey", None) or _phantom_session,
        },
    }


@app.get("/api/live-sim")
def api_live_sim() -> dict:
    """Canlı simülasyon özeti + güncel pozisyon teklifleri (paper)."""
    positions = []
    with httpx.Client() as client:
        for pos in broker.positions:
            fallback = engine._last_prices.get(pos.pool_address, pos.entry_price)
            liq = _liquidity_for_pool(pos.pool_address)
            positions.append(enrich_position(pos, fallback, liq, client, settings))
    return {"live_sim": live_sim_summary(settings), "positions": positions}


@app.get("/api/trades")
def api_trades() -> list:
    path = _trades_path()
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    trades = [json.loads(line) for line in lines if line.strip()]
    return list(reversed(trades[-100:]))


@app.get("/api/telemetry")
def api_telemetry(stream: str = Query("decisions"), limit: int = Query(100)) -> dict:
    """Gözlem akışı — attribution (giriş anı) / decisions (red/kaçan) + özet."""
    from hibrit_trader import telemetry

    return {
        "summary": telemetry.summarize(),
        "stream": stream,
        "rows": telemetry.read_recent(stream, min(limit, 500)),
    }


@app.get("/api/momentum")
def api_momentum(limit: int = Query(100)) -> dict:
    """Momentum modu gözlemi — momentum_* dosyalarından okur (salt-okunur)."""
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    state: dict = {}
    sp = data_dir / "momentum_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except Exception:
            state = {}
    positions = list(state.get("positions", []))
    for p in positions:
        entry = float(p.get("entry_price") or 0.0)
        last = float(p.get("last_price") or entry)
        p["pnl_pct_live"] = round((last / entry - 1) * 100, 2) if entry > 0 else 0.0
        p["age_min"] = round((time.time() - float(p.get("opened_ts") or time.time())) / 60, 1)
        if p.get("trail_armed"):
            p["stop_mode"] = "trail"
        elif p.get("be_armed"):
            p["stop_mode"] = "breakeven"
        else:
            p["stop_mode"] = "stop_2"
    trades: list[dict] = []
    tp = data_dir / "momentum_trades.jsonl"
    if tp.exists():
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:  # yarım/bozuk satır (crash anı) tüm paneli düşürmesin
                trades.append(json.loads(ln))
            except ValueError:
                continue
    balance = float(state.get("balance") or 0.0)
    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0.0) > 0)
    if state:
        _equity_append(data_dir, "momentum", _live_equity(state))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": state.get("realized_pnl"),
            "equity": _live_equity(state),
            "open_slots": len(positions),
            "trades_total": len(trades),
            "wins": wins,
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
            "exit_reasons": reasons,
            "updated_at": state.get("updated_at"),
        },
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 500):])),
    }


_eq_last_write: dict[str, float] = {}
_eq_lock = threading.Lock()


_MOM_EQ_ROTATE_BYTES = 10 * 1024 * 1024  # 10MB üstünde arşive al (silme YOK)


def _equity_rotate(path: Path) -> None:
    """Dosya tavanı aşınca eskisini arşiv adına taşı; veri kaybı sıfır (rename atomik)."""
    if not path.exists() or path.stat().st_size < _MOM_EQ_ROTATE_BYTES:
        return
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    base = path.name.removesuffix(".jsonl")  # momentum_equity / golge_equity / v3_equity
    target = path.with_name(f"{base}_arsiv_{stamp}.jsonl")
    n = 1
    while target.exists():  # aynı gün ikinci rotasyon: sayaçla, üzerine YAZMA
        n += 1
        target = path.with_name(f"{base}_arsiv_{stamp}.{n}.jsonl")
    path.rename(target)


def _live_equity(state: dict) -> float:
    """TEK GERCEK KAYNAK: equity = nakit + acik pozisyonlarin anlik degeri.

    Tum motor ozetleri ve equity serilerinin canli uc noktasi bu formulden gecer;
    ust rakam ile chart ucu ayni hesap olsun diye tek fonksiyonda toplandi.
    """
    balance = float(state.get("balance") or 0.0)
    pos_value = sum(
        float(p.get("amount_token") or 0.0) * float(p.get("last_price") or 0.0)
        for p in state.get("positions") or []
    )
    return round(balance + pos_value, 2)


def _equity_append(data_dir: Path, prefix: str, equity: float) -> None:
    """Panel poll'unda equity örneklemini biriktir (append-only, motora dokunmaz)."""
    try:
        with _eq_lock:
            now = time.time()
            if now - _eq_last_write.get(prefix, 0.0) < 4.0:  # çoklu sekme şişirmesin
                return
            _eq_last_write[prefix] = now
            p = data_dir / f"{prefix}_equity.jsonl"
            _equity_rotate(p)
            with p.open("a") as fh:
                fh.write(json.dumps({"ts": round(now, 1), "eq": equity}) + "\n")
    except Exception:
        pass


def _equity_series(prefix: str, minutes: int) -> dict:
    """Equity serisi: trades kumulatifi + panel orneklemleri + canli uc nokta.

    Uc nokta istek aninda state'ten _live_equity ile hesaplanir; ust ozetle
    ayni formul, ayni kaynak. Seri boylece hicbir zaman bayat bitmez.
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    start_bal = 1000.0
    state: dict = {}
    sp = data_dir / f"{prefix}_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
            start_bal = float(state.get("start_balance") or 1000.0)
        except Exception:
            state = {}
    points: list[tuple[float, float]] = []
    tp = data_dir / f"{prefix}_trades.jsonl"
    if tp.exists():
        cum = start_bal
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
                ts = float(t.get("ts") or 0.0)
                if ts <= 0:
                    continue
                if not points:  # başlangıç çapası: ilk işlemin açılış anı, $start
                    points.append((ts - float(t.get("hold_sec") or 0.0), start_bal))
                cum += float(t.get("pnl_usd") or 0.0)
                points.append((ts, round(cum, 2)))
            except Exception:
                continue
    ep = data_dir / f"{prefix}_equity.jsonl"
    if ep.exists():
        for ln in ep.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                d = json.loads(ln)
                points.append((float(d["ts"]), float(d["eq"])))
            except Exception:
                continue
    if "balance" in state:  # canli uc nokta: ust ozetle AYNI hesap (_live_equity)
        points.append((time.time(), _live_equity(state)))
    return {
        "start_balance": start_bal,
        "points": _seri_pencere(points, minutes),
    }


def _seri_pencere(points: list[tuple[float, float]], minutes: int) -> list[list]:
    """Seriyi pencereye kirp + seyrelt; ms cinsine cevir (tum equity endpointleri)."""
    points.sort(key=lambda p: p[0])
    if minutes > 0:
        cutoff = time.time() - minutes * 60
        older = [p for p in points if p[0] < cutoff]
        points = ([older[-1]] if older else []) + [p for p in points if p[0] >= cutoff]
    if len(points) > 1500:  # tarayıcıyı boğma
        stride = len(points) // 1500 + 1
        points = points[::stride] + [points[-1]]
    return [[round(ts * 1000), eq] for ts, eq in points]


def _oku_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _realized_of(data_dir: Path, name: str) -> float | None:
    p = data_dir / name
    if not p.exists():
        return None
    try:
        return round(float(json.loads(p.read_text()).get("realized_pnl") or 0.0), 2)
    except Exception:
        return None


def _motor_ozet(data_dir: Path, prefix: str, now: float, limit: int,
                evren: dict | None = None) -> dict:
    """Tek motorun ozet+pozisyon+trades paketi, TEK 'now' ile hesaplanir.

    Tek gercek kaynak ilkesi: /api/filo ve motor-bazli endpointlerin hepsi bu
    fonksiyondan gecer; ayni gosterge iki ayri yerde ayri formulle hesaplanmaz.
    """
    state = _oku_json(data_dir / f"{prefix}_state.json")
    trades: list[dict] = []
    tp = data_dir / f"{prefix}_trades.jsonl"
    if tp.exists():
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                trades.append(json.loads(ln))
            except ValueError:
                continue
    positions = list(state.get("positions", []))
    for p in positions:
        entry = float(p.get("entry_price") or 0.0)
        last = float(p.get("last_price") or entry)
        peak = float(p.get("peak_price") or entry)
        p["pnl_pct_live"] = round((last / entry - 1) * 100, 2) if entry > 0 else 0.0
        p["dd_pct_live"] = round((last / peak - 1) * 100, 2) if peak > 0 else 0.0
        p["age_min"] = round((now - float(p.get("opened_ts") or now)) / 60, 1)
    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0.0) > 0)
    summary = {
        "balance": round(float(state.get("balance") or 0.0), 2),
        "start_balance": state.get("start_balance"),
        "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
        "equity": _live_equity(state),
        "open_slots": len(positions),
        "trades_total": len(trades),
        "wins": wins,
        "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
        "exit_reasons": reasons,
        "since": state.get("updated_at"),
        "created_ts": float(state.get("created_ts") or 0.0),
        "oldest_slot_hours": round(max(p["age_min"] for p in positions) / 60, 1) if positions else None,
    }
    if evren is not None:
        summary["universe_n"] = len(evren.get("tokens") or [])
        summary["universe_at"] = evren.get("updated_at")
        summary["universe_symbols"] = [t.get("symbol") for t in (evren.get("tokens") or [])]
    if state:
        _equity_append(data_dir, prefix, summary["equity"])
    return {
        "summary": summary,
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 200):])),
    }


def _canli_blok() -> dict | None:
    """CANLI kart verisi: canli_gosterge snapshot'indan turetilir, RPC cagrisi yok."""
    from hibrit_trader import canli_gosterge
    snap = canli_gosterge.son()
    if not snap:
        return None
    baz = canli_gosterge.baz_usd()
    pct = round((snap["mtm"] / baz - 1) * 100, 2) if baz > 0 else None
    return {"mtm": snap["mtm"], "baz": baz, "pnl_pct": pct,
            "sol": snap["sol"], "sol_fiyat": snap["sol_fiyat"],
            "poz_usd": snap["poz_usd"], "acik_poz": snap["acik_poz"],
            "islem_n": snap["islem_n"], "ts": snap["ts"]}


def _canli_pozlar(v7_positions: list[dict]) -> list[dict]:
    """Canli acik pozisyon tablosu: v7 state'inden canli_miktar>0 olanlar."""
    out = []
    for p in v7_positions:
        miktar = float(p.get("canli_miktar") or 0.0)
        if miktar <= 0:
            continue
        giris = float(p.get("entry_price") or 0.0)
        guncel = float(p.get("last_price") or giris)
        kz_usd = miktar * (guncel - giris)
        kz_pct = round((guncel / giris - 1) * 100, 2) if giris > 0 else 0.0
        out.append({"pair": p.get("pair", "?"), "miktar": miktar,
                    "giris": giris, "guncel": guncel,
                    "kz_usd": round(kz_usd, 2), "kz_pct": kz_pct})
    return out


@app.get("/api/filo")
def api_filo(limit: int = Query(30)) -> dict:
    """AKTIF filo TEK tick: uc motor (v6/v7/x1) + kiyas satiri tek geciste, tek 'now' ile.

    Panel senkron ilkesi: /momentum sayfasindaki her gosterge (ozetler, MTM ve
    slot rozetleri, kiyas satiri, chartlarin canli uc noktasi) bu tek cevaptan
    basilir; ayri fetch / ayri an / ikinci hesap yok. M1/M2 arsivde (kendi
    endpointleri duruyor, arsiv acilinca bir kez okunur).
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    now = time.time()
    out: dict = {"ts": round(now, 3)}
    for prefix in ("v6", "v7", "x1", "v7c"):
        out[prefix] = _motor_ozet(data_dir, prefix, now, limit)
    out["cmp"] = {p: out[p]["summary"]["realized_pnl"]
                  for p in ("v6", "v7", "x1", "v7c")}
    out["kill"] = is_active()
    # rejim rozeti: paylasimli sol_h1 cache'inden deger + olcum yasi (fetch tetiklemez)
    from hibrit_trader.momentum_session import sol_h1_son_olcum
    sol_h1_val, sol_h1_ts = sol_h1_son_olcum()
    if sol_h1_ts > 0 and sol_h1_val is not None:
        out["rejim"] = {"sol_h1": sol_h1_val, "yas_sec": round(now - sol_h1_ts, 1)}
    from hibrit_trader import kota
    out["tarama"] = kota.tarama_sagligi()
    canli = _canli_blok()
    if canli is not None:
        canli["pozisyonlar"] = _canli_pozlar(out["v7"]["positions"])
        out["canli"] = canli
        # kiyas satiri canli kalemi: MTM bazli (realized degil), ayni snapshot'tan
        out["cmp"]["canli"] = {"mtm": canli["mtm"], "pnl_pct": canli["pnl_pct"]}
    return out


@app.get("/api/momentum/equity")
def api_momentum_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("momentum", minutes)


@app.get("/api/golge/equity")
def api_golge_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("golge", minutes)


@app.get("/api/v3/equity")
def api_v3_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("v3", minutes)


@app.get("/api/v4/equity")
def api_v4_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("v4", minutes)


@app.get("/api/v5/equity")
def api_v5_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("v5", minutes)


@app.get("/api/v6/equity")
def api_v6_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("v6", minutes)


@app.get("/api/v7/equity")
def api_v7_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("v7", minutes)


@app.get("/api/v7c/equity")
def api_v7c_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("v7c", minutes)


@app.get("/api/canli/equity")
def api_canli_equity(minutes: int = Query(0, ge=0)) -> dict:
    """Gercek cuzdan egrisi: canli_equity.jsonl + snapshot uc noktasi, baz referans."""
    from hibrit_trader import canli_gosterge
    points: list[tuple[float, float]] = []
    ep = Path(os.getenv("MOMENTUM_DATA_DIR", "data")) / "canli_equity.jsonl"
    if ep.exists():
        for ln in ep.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                d = json.loads(ln)
                points.append((float(d["ts"]), float(d["eq"])))
            except Exception:
                continue
    snap = canli_gosterge.son()
    if snap:  # canli uc nokta: seri bayat bitmesin (motor serileriyle ayni ilke)
        points.append((time.time(), snap["mtm"]))
    return {"start_balance": canli_gosterge.baz_usd(),
            "points": _seri_pencere(points, minutes)}


@app.get("/api/v8/equity")
def api_v8_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("v8", minutes)


@app.get("/api/v9/equity")
def api_v9_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("v9", minutes)


@app.get("/api/x1/equity")
def api_x1_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("x1", minutes)


@app.get("/api/v10/equity")
def api_v10_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("v10", minutes)


@app.get("/api/m1/equity")
def api_m1_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("m1", minutes)


@app.get("/api/m2/equity")
def api_m2_equity(minutes: int = Query(0, ge=0)) -> dict:
    return _equity_series("m2", minutes)


@app.get("/api/golge")
def api_golge(limit: int = Query(50)) -> dict:
    """Gölge senaryo gözlemi — golge_* dosyalarından okur, v2 ile yan yana kıyas."""
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    state: dict = {}
    sp = data_dir / "golge_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except Exception:
            state = {}
    trades: list[dict] = []
    tp = data_dir / "golge_trades.jsonl"
    if tp.exists():
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                trades.append(json.loads(ln))
            except ValueError:
                continue
    positions = list(state.get("positions", []))
    for p in positions:
        entry = float(p.get("entry_price") or 0.0)
        last = float(p.get("last_price") or entry)
        p["pnl_pct_live"] = round((last / entry - 1) * 100, 2) if entry > 0 else 0.0
        p["age_min"] = round((time.time() - float(p.get("opened_ts") or time.time())) / 60, 1)
    balance = float(state.get("balance") or 0.0)
    created_ts = float(state.get("created_ts") or 0.0)
    # v2'nin AYNI donemdeki realized PnL'i (golge basladigindan beri): adil kiyas
    v2_since = 0.0
    mp = data_dir / "momentum_trades.jsonl"
    if mp.exists() and created_ts > 0:
        for ln in mp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
                if float(t.get("ts") or 0.0) >= created_ts:
                    v2_since += float(t.get("pnl_usd") or 0.0)
            except ValueError:
                continue
    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0.0) > 0)
    if state:
        _equity_append(data_dir, "golge", _live_equity(state))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": _live_equity(state),
            "open_slots": len(positions),
            "trades_total": len(trades),
            "wins": wins,
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
            "exit_reasons": reasons,
            "since": state.get("updated_at"),
            "created_ts": created_ts,
            "v2_realized_since": round(v2_since, 2),
        },
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 200):])),
    }


@app.get("/api/v3")
def api_v3(limit: int = Query(50)) -> dict:
    """V3 senaryo gözlemi — v3_* dosyalarından okur + üç yönlü kıyas (v2/v3/gölge)."""
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    state: dict = {}
    sp = data_dir / "v3_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except Exception:
            state = {}
    trades: list[dict] = []
    tp = data_dir / "v3_trades.jsonl"
    if tp.exists():
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                trades.append(json.loads(ln))
            except ValueError:
                continue
    positions = list(state.get("positions", []))
    for p in positions:
        entry = float(p.get("entry_price") or 0.0)
        last = float(p.get("last_price") or entry)
        p["pnl_pct_live"] = round((last / entry - 1) * 100, 2) if entry > 0 else 0.0
        p["age_min"] = round((time.time() - float(p.get("opened_ts") or time.time())) / 60, 1)
    balance = float(state.get("balance") or 0.0)
    created_ts = float(state.get("created_ts") or 0.0)

    # Üç yönlü kıyas: V3'ün başladığı andan itibaren kümülatif realized PnL
    def _pnl_since(path: Path) -> float:
        total = 0.0
        if path.exists() and created_ts > 0:
            for ln in path.read_text().splitlines():
                if not ln.strip():
                    continue
                try:
                    t = json.loads(ln)
                    if float(t.get("ts") or 0.0) >= created_ts:
                        total += float(t.get("pnl_usd") or 0.0)
                except ValueError:
                    continue
        return total

    v2_since = _pnl_since(data_dir / "momentum_trades.jsonl")
    golge_since = _pnl_since(data_dir / "golge_trades.jsonl")
    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0.0) > 0)
    if state:
        _equity_append(data_dir, "v3", _live_equity(state))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": _live_equity(state),
            "open_slots": len(positions),
            "trades_total": len(trades),
            "wins": wins,
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
            "exit_reasons": reasons,
            "since": state.get("updated_at"),
            "created_ts": created_ts,
            "v2_realized_since": round(v2_since, 2),
            "golge_realized_since": round(golge_since, 2),
        },
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 200):])),
    }


@app.get("/api/v4")
def api_v4(limit: int = Query(50)) -> dict:
    """V4 melez senaryo gözlemi — v4_* dosyalarından okur + dörtlü kıyas.

    Kıyas satırı: her motorun KENDİ başlangıç anından bu yana realized PnL'i
    (her state dosyasındaki kümülatif realized_pnl).
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    state: dict = {}
    sp = data_dir / "v4_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except Exception:
            state = {}
    trades: list[dict] = []
    tp = data_dir / "v4_trades.jsonl"
    if tp.exists():
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                trades.append(json.loads(ln))
            except ValueError:
                continue
    positions = list(state.get("positions", []))
    for p in positions:
        entry = float(p.get("entry_price") or 0.0)
        last = float(p.get("last_price") or entry)
        p["pnl_pct_live"] = round((last / entry - 1) * 100, 2) if entry > 0 else 0.0
        p["age_min"] = round((time.time() - float(p.get("opened_ts") or time.time())) / 60, 1)
    balance = float(state.get("balance") or 0.0)

    def _realized_of(name: str) -> float | None:
        p = data_dir / name
        if not p.exists():
            return None
        try:
            return round(float(json.loads(p.read_text()).get("realized_pnl") or 0.0), 2)
        except Exception:
            return None

    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0.0) > 0)
    if state:
        _equity_append(data_dir, "v4", _live_equity(state))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": _live_equity(state),
            "open_slots": len(positions),
            "trades_total": len(trades),
            "wins": wins,
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
            "exit_reasons": reasons,
            "since": state.get("updated_at"),
            "created_ts": float(state.get("created_ts") or 0.0),
            "v2_realized": _realized_of("momentum_state.json"),
            "v3_realized": _realized_of("v3_state.json"),
            "golge_realized": _realized_of("golge_state.json"),
        },
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 200):])),
    }


@app.get("/api/v5")
def api_v5(limit: int = Query(50)) -> dict:
    """V5 senaryo gözlemi — v5_* dosyalarından okur + beşli kıyas.

    Kıyas satırı: her motorun KENDİ başlangıç anından bu yana realized PnL'i.
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    state: dict = {}
    sp = data_dir / "v5_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except Exception:
            state = {}
    trades: list[dict] = []
    tp = data_dir / "v5_trades.jsonl"
    if tp.exists():
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                trades.append(json.loads(ln))
            except ValueError:
                continue
    positions = list(state.get("positions", []))
    for p in positions:
        entry = float(p.get("entry_price") or 0.0)
        last = float(p.get("last_price") or entry)
        p["pnl_pct_live"] = round((last / entry - 1) * 100, 2) if entry > 0 else 0.0
        p["age_min"] = round((time.time() - float(p.get("opened_ts") or time.time())) / 60, 1)
    balance = float(state.get("balance") or 0.0)

    def _realized_of(name: str) -> float | None:
        p = data_dir / name
        if not p.exists():
            return None
        try:
            return round(float(json.loads(p.read_text()).get("realized_pnl") or 0.0), 2)
        except Exception:
            return None

    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0.0) > 0)
    if state:
        _equity_append(data_dir, "v5", _live_equity(state))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": _live_equity(state),
            "open_slots": len(positions),
            "trades_total": len(trades),
            "wins": wins,
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
            "exit_reasons": reasons,
            "since": state.get("updated_at"),
            "created_ts": float(state.get("created_ts") or 0.0),
            "v2_realized": _realized_of("momentum_state.json"),
            "v3_realized": _realized_of("v3_state.json"),
            "v4_realized": _realized_of("v4_state.json"),
            "golge_realized": _realized_of("golge_state.json"),
        },
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 200):])),
    }


@app.get("/api/v6")
def api_v6(limit: int = Query(50)) -> dict:
    """V6 senaryo gözlemi (güçlendirilmiş gölge: rejim 0.5 + hızlı göz) — v6_* dosyaları."""
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    return _motor_ozet(data_dir, "v6", time.time(), limit)


@app.get("/api/v7")
def api_v7(limit: int = Query(50)) -> dict:
    """V7 senaryo gözlemi (v6 + -%10 fren) — v7_* dosyalarından okur + aktif kıyas.

    Kıyas: her aktif motorun KENDİ başlangıç anından bu yana realized PnL'i.
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    d = _motor_ozet(data_dir, "v7", time.time(), limit)
    d["summary"].update({
        "v4_realized": _realized_of(data_dir, "v4_state.json"),
        "v6_realized": _realized_of(data_dir, "v6_state.json"),
        "golge_realized": _realized_of(data_dir, "golge_state.json"),
    })
    return d


@app.get("/api/v8")
def api_v8(limit: int = Query(50)) -> dict:
    """V8 senaryo gözlemi (gölge + 200k/20..50/tp3/20dk) - v8_* dosyalarından okur + aktif kıyas.

    Kıyas: her aktif motorun KENDİ başlangıç anından bu yana realized PnL'i.
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    state: dict = {}
    sp = data_dir / "v8_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except Exception:
            state = {}
    trades: list[dict] = []
    tp = data_dir / "v8_trades.jsonl"
    if tp.exists():
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                trades.append(json.loads(ln))
            except ValueError:
                continue
    positions = list(state.get("positions", []))
    for p in positions:
        entry = float(p.get("entry_price") or 0.0)
        last = float(p.get("last_price") or entry)
        p["pnl_pct_live"] = round((last / entry - 1) * 100, 2) if entry > 0 else 0.0
        p["age_min"] = round((time.time() - float(p.get("opened_ts") or time.time())) / 60, 1)
    balance = float(state.get("balance") or 0.0)

    def _realized_of(name: str) -> float | None:
        p = data_dir / name
        if not p.exists():
            return None
        try:
            return round(float(json.loads(p.read_text()).get("realized_pnl") or 0.0), 2)
        except Exception:
            return None

    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0.0) > 0)
    if state:
        _equity_append(data_dir, "v8", _live_equity(state))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": _live_equity(state),
            "open_slots": len(positions),
            "trades_total": len(trades),
            "wins": wins,
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
            "exit_reasons": reasons,
            "since": state.get("updated_at"),
            "created_ts": float(state.get("created_ts") or 0.0),
            "v4_realized": _realized_of("v4_state.json"),
            "v6_realized": _realized_of("v6_state.json"),
            "v7_realized": _realized_of("v7_state.json"),
            "golge_realized": _realized_of("golge_state.json"),
        },
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 200):])),
    }


@app.get("/api/v9")
def api_v9(limit: int = Query(50)) -> dict:
    """V9 senaryo gözlemi (v7 + liq 300k) - v9_* dosyalarından okur + aktif kıyas.

    Kıyas: her aktif motorun KENDİ başlangıç anından bu yana realized PnL'i.
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    state: dict = {}
    sp = data_dir / "v9_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except Exception:
            state = {}
    trades: list[dict] = []
    tp = data_dir / "v9_trades.jsonl"
    if tp.exists():
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                trades.append(json.loads(ln))
            except ValueError:
                continue
    positions = list(state.get("positions", []))
    for p in positions:
        entry = float(p.get("entry_price") or 0.0)
        last = float(p.get("last_price") or entry)
        p["pnl_pct_live"] = round((last / entry - 1) * 100, 2) if entry > 0 else 0.0
        p["age_min"] = round((time.time() - float(p.get("opened_ts") or time.time())) / 60, 1)
    balance = float(state.get("balance") or 0.0)

    def _realized_of(name: str) -> float | None:
        p = data_dir / name
        if not p.exists():
            return None
        try:
            return round(float(json.loads(p.read_text()).get("realized_pnl") or 0.0), 2)
        except Exception:
            return None

    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0.0) > 0)
    if state:
        _equity_append(data_dir, "v9", _live_equity(state))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": _live_equity(state),
            "open_slots": len(positions),
            "trades_total": len(trades),
            "wins": wins,
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
            "exit_reasons": reasons,
            "since": state.get("updated_at"),
            "created_ts": float(state.get("created_ts") or 0.0),
            "v4_realized": _realized_of("v4_state.json"),
            "v6_realized": _realized_of("v6_state.json"),
            "v7_realized": _realized_of("v7_state.json"),
            "v8_realized": _realized_of("v8_state.json"),
            "golge_realized": _realized_of("golge_state.json"),
        },
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 200):])),
    }


@app.get("/api/x1")
def api_x1(limit: int = Query(50)) -> dict:
    """X1 senaryo gözlemi (koşucu avcısı) - x1_* dosyalarından okur + aktif kıyas.

    Kıyas: her aktif motorun KENDİ başlangıç anından bu yana realized PnL'i.
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    d = _motor_ozet(data_dir, "x1", time.time(), limit)
    d["summary"].update({
        "v4_realized": _realized_of(data_dir, "v4_state.json"),
        "v6_realized": _realized_of(data_dir, "v6_state.json"),
        "v7_realized": _realized_of(data_dir, "v7_state.json"),
        "v8_realized": _realized_of(data_dir, "v8_state.json"),
        "v9_realized": _realized_of(data_dir, "v9_state.json"),
        "golge_realized": _realized_of(data_dir, "golge_state.json"),
    })
    return d


@app.get("/api/v10")
def api_v10(limit: int = Query(50)) -> dict:
    """V10 senaryo gözlemi (saf tp_2, durduruldu) - v10_* dosyalarından okur."""
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    state: dict = {}
    sp = data_dir / "v10_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except Exception:
            state = {}
    trades: list[dict] = []
    tp = data_dir / "v10_trades.jsonl"
    if tp.exists():
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                trades.append(json.loads(ln))
            except ValueError:
                continue
    positions = list(state.get("positions", []))
    for p in positions:
        entry = float(p.get("entry_price") or 0.0)
        last = float(p.get("last_price") or entry)
        p["pnl_pct_live"] = round((last / entry - 1) * 100, 2) if entry > 0 else 0.0
        p["age_min"] = round((time.time() - float(p.get("opened_ts") or time.time())) / 60, 1)
    balance = float(state.get("balance") or 0.0)
    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0.0) > 0)
    if state:
        _equity_append(data_dir, "v10", _live_equity(state))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": _live_equity(state),
            "open_slots": len(positions),
            "trades_total": len(trades),
            "wins": wins,
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
            "exit_reasons": reasons,
            "since": state.get("updated_at"),
            "created_ts": float(state.get("created_ts") or 0.0),
        },
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 200):])),
    }


@app.get("/api/m1")
def api_m1(limit: int = Query(50)) -> dict:
    """M1 senaryo gözlemi (major evren) - m1_* dosyalarından okur."""
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    evren = _oku_json(data_dir / "m1_universe.json")
    return _motor_ozet(data_dir, "m1", time.time(), limit, evren=evren)


@app.get("/api/m2")
def api_m2(limit: int = Query(50)) -> dict:
    """M2 senaryo gözlemi (major evren, saf tp) - m2_* dosyalarından okur + aktif kıyas.

    Kıyas: her aktif motorun KENDİ başlangıç anından bu yana realized PnL'i.
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    evren = _oku_json(data_dir / "m1_universe.json")  # evren M1 ile ortak dosya
    d = _motor_ozet(data_dir, "m2", time.time(), limit, evren=evren)
    d["summary"].update({
        "v6_realized": _realized_of(data_dir, "v6_state.json"),
        "v7_realized": _realized_of(data_dir, "v7_state.json"),
        "x1_realized": _realized_of(data_dir, "x1_state.json"),
        "m1_realized": _realized_of(data_dir, "m1_state.json"),
    })
    return d


# ---- /momentum sayfasi: filo konfigurasyonu -----------------------------------------
# Kural: kart grid ve chart sutunu BU listeden uretilir (elle esleme yok). Bot
# eklenince/cikinca kart ve chart otomatik eslesir; JS tarafi ayni listeyi
# MOTORLAR olarak alir. "canli" placeholder karttir.

_FILO_MOTORLAR: list[dict] = [
    {"id": "canli", "tip": "canli", "ad": "CANLI", "renk": "#e3b341"},
    {"id": "v6", "tip": "bot", "ad": "V6", "renk": "#3fb950", "slots": 5,
     "rozet": "hızlı göz",
     "desc": "güçlendirilmiş gölge: liq&ge;$100k · h1 10..50 · tp+2 · 30dk sabır, stop-2 · 60dk tavan · rejim sol_h1&ge;0.5 · hızlı göz 2s"},
    # gizli: motor calismaya ve /api/filo'da veri uretmeye devam eder; sadece
    # panel gorunumunden (kart+chart+islem tablosu+kiyas) cikarilir. Canli test
    # doneminde ekranda yalniz V6 + CANLI kalir (12 Tem karari).
    {"id": "v7", "tip": "bot", "ad": "V7", "renk": "#58a6ff", "slots": 5,
     "rozet": "fren -%10", "gizli": True,
     "desc": "-%10 felaket freni · sabır iptal, anında sat · rejim sol_h1&ge;0.35 · h1 20-40 skip"},
    # arka: kart+chart ana ekrandan "Arka plan deneyleri" katlanir bolumune iner;
    # motor, veri uretimi ve kiyas kayitlari aynen surer (sadece gorunum).
    {"id": "x1", "tip": "bot", "ad": "X1", "renk": "#d29922", "slots": 3,
     "rozet": "koşucu avcısı", "arka": True, "gizli": True,
     "desc": "koşucu avcısı: h1&ge;50 + m5&gt;0 + liq&ge;$20k · bilet&le;$70 · yarım tp mfe&ge;+15 · trail -18 · 6sa tavan"},
    {"id": "v7c", "tip": "bot", "ad": "V7C", "renk": "#bc8cff", "slots": 5,
     "rozet": "majör 2-10", "gizli": True,
     "desc": "v7 iskeleti majör/likit evrende: liq&ge;$3M · h1 2..10 · tp+2 · fren -%10 · rejim sol_h1&ge;0.5 · PAPER sabit"},
]


_CANLI_CUZDAN = "DZXZGD5FURZDwa5BWByxxd7iLdCvGxSCy6RWHsgupaYa"


def _filo_kart_canli(durum: str) -> str:
    """CANLI karti uc durumlu: bagli (gercek para) / kilitli (tam iskelet +
    kilit rozeti) / yok (placeholder). Kilitliyken de tam gosterge basilir ki
    cuzdan MTM/SOL izlenebilsin; sadece rozet degisir."""
    if durum in ("bagli", "kilitli"):
        pct = os.getenv("LIVE_TICKET_PCT", "25").strip() or "25"
        rozet = ('<span class="rozet" style="background:#da3633;color:#fff">gerçek para</span>'
                 if durum == "bagli" else
                 '<span class="rozet">kilitli &#128274;</span>')
        return ('<div class="kart" id="kart-canli">'
                f'<div class="khead"><b style="color:#e3b341">CANLI</b>{rozet}</div>'
                '<div class="mtmbig" id="mtm-canli">yükleniyor…</div>'
                '<div class="ksub" id="sub-canli">-</div>'
                '<div class="ksub" id="sol-canli">serbest SOL: -</div>'
                '<canvas class="spark" id="spark-canli" height="36"></canvas>'
                '<div class="kfoot" id="foot-canli">-</div>'
                '<button id="killBtn" disabled>...</button>'
                f'<div class="kfoot">V7 canlı · bilet: MTM %{pct} · muhasebe paper '
                "boyutta · fill'ler tx imzalı, denetim defterine düşer · cüzdan "
                f'{_CANLI_CUZDAN[:4]}…{_CANLI_CUZDAN[-4:]}</div></div>')
    return ('<div class="kart bos" id="kart-canli">'
            '<div class="khead"><b>CANLI</b><span class="rozet">kilitli</span></div>'
            '<div class="kilit">&#128274;</div>'
            '<div class="bosmetin">Gerçek para. Karne günü kazanan buraya bağlanır.</div>'
            '<button id="killBtn" disabled>...</button>'
            '<div class="kfoot">cüzdan: bağlı değil</div></div>')


def _filo_kart(m: dict, canli_durum: str = "yok") -> str:
    if m["tip"] == "bot":
        return (f'<div class="kart" id="kart-{m["id"]}" title="{m["desc"]}">'
                f'<div class="khead"><b style="color:{m["renk"]}">{m["ad"]}</b>'
                f'<span class="rozet">{m["rozet"]}</span>'
                '<span class="rozet liderroz">lider</span></div>'
                f'<div class="mtmbig" id="mtm-{m["id"]}">yükleniyor…</div>'
                f'<div class="ksub" id="sub-{m["id"]}">-</div>'
                f'<canvas class="spark" id="spark-{m["id"]}" height="36"></canvas>'
                f'<div class="kfoot" id="foot-{m["id"]}">-</div></div>')
    return _filo_kart_canli(canli_durum)


def _filo_chart(m: dict, canli_durum: str = "yok") -> str:
    if m["tip"] == "bot":
        return (f'<div class="chhead"><span class="dot" style="background:{m["renk"]}"></span>'
                f'<b>{m["ad"]}</b> <span class="chdesc">{m["desc"]}</span>'
                f'<span id="eq{m["id"]}label" class="eqlabel"></span>'
                f'<span id="{m["id"]}upd" class="updlabel"></span></div>'
                f'<div class="eqwrap"><span id="eq{m["id"]}trend" class="trendroz"></span>'
                f'<canvas id="eq{m["id"]}chart"></canvas></div>')
    if canli_durum in ("bagli", "kilitli"):
        from hibrit_trader import canli_gosterge
        ek = "" if canli_durum == "bagli" else " · kilit kapalı, giriş yok"
        return ('<div class="chhead"><span class="dot" style="background:#e3b341"></span>'
                '<b>CANLI</b> <span class="chdesc">gerçek cüzdan (SOL + açık poz), '
                f'baz ${canli_gosterge.baz_usd():.2f}{ek}</span>'
                '<span id="eqcanlilabel" class="eqlabel"></span></div>'
                '<div class="eqwrap"><span id="eqcanlitrend" class="trendroz"></span>'
                '<canvas id="eqcanlichart"></canvas></div>'
                '<h2>AÇIK POZİSYONLAR · canlı</h2>'
                '<div class="tablewrap"><table id="canliPoz"><thead><tr>'
                '<th>token</th><th>miktar</th><th>giriş</th><th>güncel</th>'
                '<th>K/Z $</th><th>K/Z %</th></tr></thead><tbody></tbody></table></div>')
    return ('<div class="chhead"><span class="dot" style="background:#e3b341"></span>'
            '<b>CANLI</b></div>'
            '<div class="ph">kazanan bağlandığında gerçek para eğrisi burada akacak</div>')


_MOMENTUM_HTML = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>Momentum filo</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{background:#0d1117;color:#c9d1d9;font:13px/1.55 monospace;margin:0 auto;
   max-width:1400px;padding:18px 20px 40px}
 h2{color:#58a6ff;margin:22px 0 8px;font-size:15px}
 .pos{color:#3fb950} .neg{color:#f85149}
 table{border-collapse:collapse;width:100%;margin-bottom:14px}
 th,td{border:1px solid #30363d;padding:4px 9px;text-align:right;white-space:nowrap}
 th{background:#161b22;color:#8b949e} td:first-child,th:first-child{text-align:left}
 .chip{display:inline-block;padding:0 6px;border-radius:8px;background:#21262d}
 div[id$="sum"] span{margin-right:16px}
 .tablewrap{overflow-x:auto}
 #topbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
 #topbar h1{color:#58a6ff;font-size:19px;margin:0;letter-spacing:1px}
 .badge{display:inline-block;padding:2px 10px;border-radius:10px;background:#21262d;
   color:#8b949e;font-size:12px;margin-left:6px;border:1px solid #30363d}
 .badge.ok{color:#3fb950;border-color:#238636}
 .badge.err{background:#da3633;color:#fff;border-color:#da3633}
 .badge.bayat{opacity:.55;color:#8b949e;border-color:#30363d}
 #kartGrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:16px 0 6px}
 @media(max-width:1100px){#kartGrid{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}}
 .kart{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px;
   min-height:158px;display:flex;flex-direction:column}
 .kart.bos{background:transparent;border-style:dashed;color:#8b949e}
 .kart.lider{border:2px solid #1f6feb;padding:11px 13px}
 .khead{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
 .rozet{font-size:11px;padding:0 8px;border-radius:10px;background:#21262d;color:#8b949e}
 .liderroz{display:none;background:#1f6feb;color:#fff}
 .kart.lider .liderroz{display:inline-block}
 .mtmbig{font-size:25px;margin:8px 0 2px}
 .ksub{color:#8b949e}
 .spark{width:100%;height:36px;margin:8px 0 4px}
 .kfoot{color:#8b949e;font-size:12px;margin-top:auto}
 .kilit{font-size:22px;margin:8px 0 4px}
 .bosmetin{margin:4px 0 8px}
 .chhead{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin:18px 0 4px}
 .chhead b{font-size:14px}
 .chdesc{color:#8b949e;font-size:11px}
 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#30363d;
   align-self:center}
 .ph{border:1px dashed #30363d;border-radius:10px;min-height:90px;display:flex;
   align-items:center;justify-content:center;color:#484f58;padding:14px;text-align:center;
   margin-bottom:8px}
 .eqbtns{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0 8px}
 .eqbtns button{background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:6px;
   padding:2px 10px;cursor:pointer;font:inherit}
 .eqbtns button.act{background:#1f6feb;color:#fff;border-color:#1f6feb}
 .eqwrap{position:relative;width:100%;height:260px;margin-bottom:10px}
 .eqlabel{color:#8b949e} .eqlabel b{color:#c9d1d9}
 .mtm{font-size:24px;margin:2px 0 6px;color:#8b949e}
 .slotbadge{display:inline-block;font-size:11px;vertical-align:middle;margin-left:10px;
   padding:1px 8px;border-radius:10px;background:#21262d;color:#8b949e}
 .slotbadge.b24{background:#9e6a03;color:#fff}
 .slotbadge.b48{background:#da3633;color:#fff}
 .updlabel{font-size:11px;font-weight:normal;color:#8b949e;margin-left:6px}
 .updlabel.stale{color:#f85149}
 .trendroz{position:absolute;top:6px;right:8px;z-index:2;display:none;font-size:11px;
   padding:1px 9px;border-radius:10px;color:#fff}
 .trendroz.up{display:inline-block;background:#1D9E75}
 .trendroz.down{display:inline-block;background:#E24B4A}
 .exchip{display:inline-block;padding:0 8px;border-radius:8px;background:#21262d;
   font-size:12px;border:1px solid transparent}
 .ex-tp{background:rgba(63,185,80,.15);color:#3fb950}
 .ex-stop{background:transparent;color:#f85149;border-color:#f85149}
 .ex-to{background:#21262d;color:#8b949e}
 .ex-amber{background:rgba(210,153,34,.18);color:#d29922}
 tr.loss td{background:rgba(248,81,73,.06)}
 tr.canli td{background:rgba(227,179,65,.07)}
 tr.canli td:first-child{border-left:3px solid #e3b341}
 .livechip{display:inline-block;margin-left:4px;padding:0 6px;border-radius:8px;
   background:#e3b341;color:#1c2128;font-size:10px;font-weight:bold;letter-spacing:1px}
 .txlink{color:#58a6ff;text-decoration:none;font-size:12px}
 #killBant{background:#f85149;color:#fff;font-weight:bold;text-align:center;
   padding:10px;margin-bottom:14px;border-radius:6px;letter-spacing:1px}
 #killBtn{width:100%;margin-top:8px;padding:7px 0;font:bold 12px monospace;
   border-radius:6px;cursor:pointer;background:transparent;
   border:1px solid #30363d;color:#8b949e}
 #killBtn.dur{border-color:#f85149;color:#f85149}
 #killBtn.bas{border-color:#3fb950;color:#3fb950}
 @media(max-width:600px){.eqwrap{height:210px}body{padding:12px}}
</style></head><body>
<div id="killBant" style="display:none">DURDURULDU · kill-switch aktif · yeni işlem açılmaz</div>
<div id="topbar">
 <div><h1>AKTİF YARIŞ</h1></div>
 <div>
  <span id="feedBadge" class="badge">feed: -</span>
  <span id="rejimBadge" class="badge">rejim sol_h1: -</span>
  <span id="taramaBadge" class="badge">tarama: -</span>
  <!--MODROZET-->
 </div>
</div>
<div id="kartGrid"><!--KARTLAR--></div>
<div id="cmp3" style="color:#8b949e;margin:2px 0 14px">yükleniyor…</div>
<div class="eqbtns" id="eqsyncbtns"></div>
<div id="chartCol"><!--CHARTCOL--></div>
<h2>SON İŞLEMLER · aktif filo</h2>
<div class="tablewrap"><table id="isltr"><thead><tr><th>bot</th><th>pair</th><th>exit</th>
<th>pnl $</th><th>pnl%</th><th>mfe/mae</th><th>chg_h1</th><th>sol_h1</th><th>liq $</th>
<th>giriş</th><th>çıkış</th><th>hold sn</th><th>kapanış</th><th>tx</th></tr></thead><tbody></tbody></table></div>
<details id="arkaBox" style="margin-top:28px;border-top:1px solid #30363d;padding-top:8px">
<summary style="cursor:pointer;color:#8b949e"><b>ARKA PLAN DENEYLERİ · çalışır durumda (x1) · tıkla aç</b></summary>
<div id="arkaIc"><!--ARKA-->
<h2>SON İŞLEMLER · x1</h2>
<div class="tablewrap"><table id="isltrArka"><thead><tr><th>bot</th><th>pair</th><th>exit</th>
<th>pnl $</th><th>pnl%</th><th>mfe/mae</th><th>chg_h1</th><th>sol_h1</th><th>liq $</th>
<th>giriş</th><th>çıkış</th><th>hold sn</th><th>kapanış</th><th>tx</th></tr></thead><tbody></tbody></table></div>
</div>
</details>
<details id="arsivBox" style="margin-top:28px;border-top:1px solid #30363d;padding-top:8px">
<summary style="cursor:pointer;color:#8b949e"><b>ARŞİV · durdurulan motorlar (m1 · m2 · v2 · v3 · v4 · v5 · gölge · v8 · v9 · v10) · tıkla aç</b></summary>
<div id="arsivIc">
<h2>M1 Senaryo (durduruldu · MAJOR evren: liq&ge;$3M · h1 1.5..15, m5 sıralı · rejim sol_h1&ge;0.3 · tp+1.2 / fren -4 / 20dk sabır stop -1.5 / 90dk tavan)</h2>
<div id="m1mtm" class="mtm">arşiv, açınca yüklenir…</div>
<div id="m1sum">arşiv, açınca yüklenir…</div>
<table id="m1tr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_m5</th><th>chg_h1</th><th>sol_h1</th><th>liq $</th>
<th>hold sn</th><th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (M1)</h2>
<div class="eqbtns" id="eqm1btns"></div>
<div class="eqwrap"><canvas id="eqm1chart"></canvas></div>
<h2>M2 Senaryo (durduruldu · MAJOR evren: liq&ge;$3M · h1 1.5..15, h1 sıralı · saf tp+1.2 · stop/timeout/rejim/cooldown YOK)</h2>
<div id="m2mtm" class="mtm">arşiv, açınca yüklenir…</div>
<div id="m2sum">arşiv, açınca yüklenir…</div>
<table id="m2tr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_m5</th><th>chg_h1</th><th>sol_h1</th><th>liq $</th>
<th>hold sn</th><th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (M2)</h2>
<div class="eqbtns" id="eqm2btns"></div>
<div class="eqwrap"><canvas id="eqm2chart"></canvas></div>
<h2>MOMENTUM v2 (durduruldu · slot 5 · liq&ge;$40k · m5&gt;0 · h1 5..50 · stop-2/BE+3/trail 5/-3 · 60dk)</h2>
<div id="sum">arşiv, açınca yüklenir…</div>
<table id="pos"><thead><tr><th>pair</th><th>chain</th><th>giriş</th><th>son</th>
<th>pnl%</th><th>peak mfe%</th><th>stop modu</th><th>chg_m5</th><th>chg_h1</th>
<th>liq $</th><th>yaş dk</th><th>maliyet $</th></tr></thead><tbody></tbody></table>
<table id="tr"><thead><tr><th>pair</th><th>chain</th><th>exit_reason</th><th>pnl $</th>
<th>pnl%</th><th>friction%</th><th>chg_m5</th><th>chg_h1</th><th>liq $</th>
<th>hold sn</th><th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (MOMENTUM v2)</h2>
<div class="eqbtns" id="eqv2btns"></div>
<div class="eqwrap"><canvas id="eqv2chart"></canvas></div>
<h2>V3 Senaryo (durduruldu · h1 5..15 düşük önce · rejim&ge;0.5 · BE+1.5 · cooldown 45dk)</h2>
<div id="v3sum">arşiv, açınca yüklenir…</div>
<table id="v3tr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_m5</th><th>chg_h1</th><th>sol_h1</th><th>hold sn</th>
<th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (V3)</h2>
<div class="eqbtns" id="eqv3btns"></div>
<div class="eqwrap"><canvas id="eqv3chart"></canvas></div>
<h2>V5 Senaryo (durduruldu · gölge zemini + taban -%8 · tp yarım + koşucu trail -3/be+1.5)</h2>
<div id="v5sum">arşiv, açınca yüklenir…</div>
<table id="v5tr"><thead><tr><th>pair</th><th>exit_reason</th><th>koşucu</th><th>pnl $</th>
<th>pnl%</th><th>mfe%</th><th>mae%</th><th>chg_h1</th><th>liq $</th><th>hold sn</th>
<th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (V5)</h2>
<div class="eqbtns" id="eqv5btns"></div>
<div class="eqwrap"><canvas id="eqv5chart"></canvas></div>
<h2>Gölge Senaryo (durduruldu · liq&ge;$100k + h1&ge;10 · TP+2 · 30dk sabır sonrası stop-2 · 60dk tavan)</h2>
<div id="gsum">arşiv, açınca yüklenir…</div>
<table id="gtr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_h1</th><th>hold sn</th><th>kapanış</th></tr></thead>
<tbody></tbody></table>
<h2>Equity (Gölge)</h2>
<div class="eqbtns" id="eqgbtns"></div>
<div class="eqwrap"><canvas id="eqgchart"></canvas></div>
<h2>V8 Senaryo (durduruldu · gölge + liq 200k · h1 20..50 · tp+3 · mutlak 20dk tavan)</h2>
<div id="v8sum">arşiv, açınca yüklenir…</div>
<table id="v8tr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_h1</th><th>sol_h1</th><th>liq $</th><th>hold sn</th>
<th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (V8)</h2>
<div class="eqbtns" id="eqv8btns"></div>
<div class="eqwrap"><canvas id="eqv8chart"></canvas></div>
<h2>V4 Melez Senaryo (durduruldu · v3 girişi h1 5..15 · kademeli trail -3/-6 · karda 120dk tavan)</h2>
<div id="v4sum">arşiv, açınca yüklenir…</div>
<table id="v4tr"><thead><tr><th>pair</th><th>exit_reason</th><th>kademe</th><th>pnl $</th>
<th>pnl%</th><th>mfe%</th><th>mae%</th><th>chg_m5</th><th>chg_h1</th><th>sol_h1</th>
<th>hold sn</th><th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (V4)</h2>
<div class="eqbtns" id="eqv4btns"></div>
<div class="eqwrap"><canvas id="eqv4chart"></canvas></div>
<h2>V9 Senaryo (durduruldu · v7 + TEK fark: likidite tabanı $300k)</h2>
<div id="v9sum">arşiv, açınca yüklenir…</div>
<table id="v9tr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_h1</th><th>sol_h1</th><th>liq $</th><th>hold sn</th>
<th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (V9)</h2>
<div class="eqbtns" id="eqv9btns"></div>
<div class="eqwrap"><canvas id="eqv9chart"></canvas></div>
<h2>V10 Senaryo (durduruldu · saf tp+2: liq&ge;$300k + h1 10..50 · stop/timeout/rejim/cooldown YOK)</h2>
<div id="v10sum">arşiv, açınca yüklenir…</div>
<table id="v10tr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_h1</th><th>sol_h1</th><th>liq $</th><th>hold sn</th>
<th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (V10)</h2>
<div class="eqbtns" id="eqv10btns"></div>
<div class="eqwrap"><canvas id="eqv10chart"></canvas></div>
</div>
</details>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<script>
const f=(x,d=2)=>x==null?"-":Number(x).toFixed(d);
const cls=x=>x>0?"pos":(x<0?"neg":"");
const eqCharts={};  // canli chartlar: filo tick'i ayni poll'un equity'sini uca basar
const MOTORLAR="__MOTORLAR__";  // sunucu _FILO_MOTORLAR listesinden basar (tek konfig)

// ---- ARSIV: Golge (donuk, acilinca bir kez) -----------------------------------
async function arsivGolge(){
  let d; try{const r=await fetch("/api/golge?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("gsum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.querySelector("#gtr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_h1,1)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=9>henüz yok</td></tr>";
}

// ---- ARSIV: V4 melez (donuk, acilinca bir kez) ----------------------------------
async function arsivV4(){
  let d; try{const r=await fetch("/api/v4?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("v4sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.querySelector("#v4tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td>${t.trail_kademe==null?"-":t.trail_kademe}</td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_m5,1)}</td>`+
    `<td>${f(t.chg_h1,1)}</td><td>${f(t.sol_chg_h1,2)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=12>henüz yok</td></tr>";
}

// ---- ARSIV: V8 (donuk, acilinca bir kez) ----------------------------------------
async function arsivV8(){
  let d; try{const r=await fetch("/api/v8?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("v8sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.querySelector("#v8tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_h1,1)}</td>`+
    `<td>${f(t.sol_chg_h1,2)}</td><td>${f(t.liq_entry,0)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=11>henüz yok</td></tr>";
}

// ---- ARSIV: V9 (donuk, acilinca bir kez) --------------------------------------------
async function arsivV9(){
  let d; try{const r=await fetch("/api/v9?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("v9sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.querySelector("#v9tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_h1,1)}</td>`+
    `<td>${f(t.sol_chg_h1,2)}</td><td>${f(t.liq_entry,0)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=11>henüz yok</td></tr>";
}

// ---- ARSIV: V10 (donuk, acilinca bir kez) -------------------------------------------
async function arsivV10(){
  let d; try{const r=await fetch("/api/v10?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("v10sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.querySelector("#v10tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_h1,1)}</td>`+
    `<td>${f(t.sol_chg_h1,2)}</td><td>${f(t.liq_entry,0)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=11>henüz yok</td></tr>";
}

function mtmSatir(s){
  // MTM = nakit + acik pozisyonlarin anlik degeri (s.equity, _live_equity ile ayni).
  // Rozet: kill-criterion'daki 48 saat slot kilidi gozle izlensin (24s sari, 48s kirmizi).
  const osh=s.oldest_slot_hours;
  const badge=osh==null?"":
    `<span class="slotbadge ${osh>48?"b48":(osh>24?"b24":"")}">en yaşlı slot ${f(osh,1)}s</span>`;
  return `MTM <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b>${badge}`;
}
// ---- ARSIV: M1 (durduruldu, acilinca bir kez) ---------------------------------------
async function arsivM1(){
  let d; try{const r=await fetch("/api/m1?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("m1mtm").innerHTML=mtmSatir(s);
  document.getElementById("m1sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span><span>evren <b>${s.universe_n}</b> token</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.querySelector("#m1tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_m5,2)}</td>`+
    `<td>${f(t.chg_h1,2)}</td><td>${f(t.sol_chg_h1,2)}</td><td>${f(t.liq_entry,0)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=12>henüz yok</td></tr>";
}

// ---- ARSIV: M2 (durduruldu, acilinca bir kez) ---------------------------------------
async function arsivM2(){
  let d; try{const r=await fetch("/api/m2?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("m2mtm").innerHTML=mtmSatir(s);
  document.getElementById("m2sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span><span>evren <b>${s.universe_n}</b> token</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.querySelector("#m2tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_m5,2)}</td>`+
    `<td>${f(t.chg_h1,2)}</td><td>${f(t.sol_chg_h1,2)}</td><td>${f(t.liq_entry,0)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=12>henüz yok</td></tr>";
}

// ---- ARSIV: v2/v3/v5, katlanir bolum acilinca BIR kez yuklenir (donuk) ----------
async function arsivV2(){
  let d; try{const r=await fetch("/api/momentum?limit=100"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>slot ${s.open_slots}/5</span><span>işlem ${s.trades_total}</span>`+
    `<span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.querySelector("#pos tbody").innerHTML=(d.positions||[]).map(p=>
    `<tr><td>${p.pair}</td><td>${p.chain}</td><td>${Number(p.entry_price).toPrecision(6)}</td>`+
    `<td>${Number(p.last_price).toPrecision(6)}</td><td class="${cls(p.pnl_pct_live)}">${f(p.pnl_pct_live)}</td>`+
    `<td>${f(p.mfe_pct,1)}</td><td><span class="chip">${p.stop_mode}</span></td>`+
    `<td>${f(p.chg_m5,1)}</td><td>${f(p.chg_h1,1)}</td><td>${f(p.liq_entry,0)}</td>`+
    `<td>${f(p.age_min,1)}</td><td>${f(p.cost_usd)}</td></tr>`).join("")||"<tr><td colspan=12>boş</td></tr>";
  document.querySelector("#tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td>${t.chain}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.friction_pct)}</td><td>${f(t.chg_m5,1)}</td><td>${f(t.chg_h1,1)}</td>`+
    `<td>${f(t.liq_entry,0)}</td><td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=11>henüz yok</td></tr>";
}
async function arsivV3(){
  let d; try{const r=await fetch("/api/v3?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("v3sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.querySelector("#v3tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_m5,1)}</td>`+
    `<td>${f(t.chg_h1,1)}</td><td>${f(t.sol_chg_h1,2)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=11>henüz yok</td></tr>";
}
async function arsivV5(){
  let d; try{const r=await fetch("/api/v5?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("v5sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.querySelector("#v5tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td>${t.runner?"evet":"-"}</td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_h1,1)}</td>`+
    `<td>${f(t.liq_entry,0)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=11>henüz yok</td></tr>";
}
let arsivYuklendi=false;
const arsivBoxEl=document.getElementById("arsivBox");
if(arsivBoxEl)arsivBoxEl.addEventListener("toggle",e=>{
  if(!e.target.open||arsivYuklendi)return;
  arsivYuklendi=true;  // BIR kez: donuk ozet + donuk chartlar (interval yok)
  arsivM1(); arsivM2();
  arsivV2(); arsivV3(); arsivV5(); arsivGolge(); arsivV8();
  arsivV4(); arsivV9(); arsivV10();
  mkEqChart("eqm1","/api/m1/equity",false);
  mkEqChart("eqm2","/api/m2/equity",false);
  mkEqChart("eqv2","/api/momentum/equity",false);
  mkEqChart("eqv3","/api/v3/equity",false);
  mkEqChart("eqv5","/api/v5/equity",false);
  mkEqChart("eqg","/api/golge/equity",false);
  mkEqChart("eqv8","/api/v8/equity",false);
  mkEqChart("eqv4","/api/v4/equity",false);
  mkEqChart("eqv9","/api/v9/equity",false);
  mkEqChart("eqv10","/api/v10/equity",false);
});

// ---- Equity chart'lari: ortak gorsel dil ($start kesikli referans, canli uc) -----
const EQWINS=[["5dk",5],["15dk",15],["30dk",30],["1s",60],["2s",120],["5s",300],
  ["12s",720],["24s",1440],["48s",2880],["1h",10080],["2h",20160],["Tümü",0]];
const SYNCWINS=[["5dk",5],["15dk",15],["30dk",30],["1s",60],["2s",120],["5s",300],
  ["12s",720],["24s",1440],["48s",2880],["Tümü",0]];
function sparkHazirla(pts,now){
  // spark verisi: son 24 saat, ~72 noktaya seyreltilir (ilk/son nokta korunur);
  // renk 24s net degisime gore: pozitif/notr yesil, negatif kirmizi
  const SAAT=24,NOKTA=72;
  const t0=now-SAAT*3600000;
  let p=pts.filter(q=>q.x>=t0);
  if(p.length>NOKTA){
    const adim=(p.length-1)/(NOKTA-1),sey=[];
    for(let i=0;i<NOKTA;i++)sey.push(p[Math.round(i*adim)]);
    p=sey;
  }
  const renk=p.length>=2&&p[p.length-1].y-p[0].y<0?"#f85149":"#3fb950";
  return {p,renk};
}
function cizSpark(id,pts,start){
  // kart mini sparkline: son 24 saat equity, referans cizgisi kesikli
  const c=document.getElementById(id); if(!c)return;
  const w=c.width=c.clientWidth||160, h=c.height=36;
  const x=c.getContext("2d"); x.clearRect(0,0,w,h);
  const {p,renk}=sparkHazirla(pts,Date.now());
  let lo=start,hi=start;
  for(const q of p){if(q.y<lo)lo=q.y;if(q.y>hi)hi=q.y;}
  const pad=(hi-lo)*0.12||1; lo-=pad; hi+=pad;
  const Y=v=>h-2-(v-lo)/(hi-lo)*(h-4);
  x.strokeStyle="#30363d"; x.setLineDash([3,3]); x.beginPath();
  x.moveTo(0,Y(start)); x.lineTo(w,Y(start)); x.stroke(); x.setLineDash([]);
  if(p.length<2)return;
  const x0=p[0].x, dx=(p[p.length-1].x-x0)||1;
  const X=v=>(v-x0)/dx*(w-6)+3;
  x.strokeStyle=renk; x.lineWidth=1.5; x.beginPath();
  p.forEach((q,i)=>i?x.lineTo(X(q.x),Y(q.y)):x.moveTo(X(q.x),Y(q.y)));
  x.stroke();
  const son=p[p.length-1];
  x.fillStyle=renk; x.beginPath(); x.arc(X(son.x),Y(son.y),2,0,7); x.fill();
}
// ---- Trend katmani: kumulatif ortalama + hiz rozeti -------------------------------
function renkAlfa(hex,a){
  const n=parseInt(hex.slice(1),16);
  return "rgba("+((n>>16)&255)+","+((n>>8)&255)+","+(n&255)+","+a+")";
}
function kumulatifSeri(pts){
  // her t noktasi: serinin BASINDAN t'ye kadarki tum degerlerin ortalamasi
  const out=[];let t=0;
  for(let i=0;i<pts.length;i++){
    t+=pts[i].y;
    out.push({x:pts[i].x,y:t/(i+1)});
  }
  return out;
}
function trendHiz(seri){
  // uzun vadeli gidisat: kumulatif egrinin son ~%20 diliminin egimi ($/saat)
  const n=seri.length;
  if(n<2)return null;
  const i0=Math.min(Math.floor(n*0.8),n-2);
  const dt=seri[n-1].x-seri[i0].x;
  if(dt<=0)return null;
  return (seri[n-1].y-seri[i0].y)/dt*3600000;
}
function mkEqChart(prefix, api, live=true, shared=false, renk="#58a6ff", sparkId=null){
  // shared=true: kendi buton seridi yok, pencereyi ortak eqSyncWin belirler
  const st={win:0, chart:null, start:1000, pts:[], sparkPts:[], live:null, kum:[], kumSum:0, kumN:0};
  const getWin=()=>shared?eqSyncWin:st.win;
  const refLine={id:prefix+"ref",afterDatasetsDraw(c){
    const y=c.scales.y.getPixelForValue(st.start),a=c.chartArea;
    if(!a||isNaN(y)||y<a.top||y>a.bottom)return;
    const x=c.ctx;x.save();x.strokeStyle="#8b949e";x.setLineDash([4,4]);x.lineWidth=1;
    x.beginPath();x.moveTo(a.left,y);x.lineTo(a.right,y);x.stroke();x.restore();}};
  function buttons(){
    const el=document.getElementById(prefix+"btns");
    if(!el)return;
    el.innerHTML=EQWINS.map(([l,m])=>
      `<button data-m="${m}" class="${m===st.win?"act":""}">${l}</button>`).join("");
    el.querySelectorAll("button").forEach(b=>b.onclick=()=>{
      st.win=Number(b.dataset.m);buttons();tick();});
  }
  async function tick(){
    const win=getWin();
    let d;
    try{const r=await fetch(`${api}?minutes=${win}`);d=await r.json();}
    catch(e){return;}
    st.start=d.start_balance||1000;
    st.pts=(d.points||[]).map(p=>({x:p[0],y:p[1]}));
    // spark hep 24 saat: secili pencere 24s'ten darsa spark kendi verisini ceker
    if(sparkId&&win>0&&win<1440){
      try{
        const r2=await fetch(`${api}?minutes=1440`);
        const d2=await r2.json();
        st.sparkPts=(d2.points||[]).map(p=>({x:p[0],y:p[1]}));
      }catch(e){st.sparkPts=st.pts;}
    }else{
      st.sparkPts=st.pts;
    }
    // kumulatif taban bir kez O(n); canli uc her tick'te toplam+sayacla O(1)
    st.kum=kumulatifSeri(st.pts);
    st.kumN=st.pts.length;
    st.kumSum=st.pts.reduce((a,p)=>a+p.y,0);
    render();
  }
  function setLive(eq){
    // ust ozetle AYNI poll'un equity'si chartin son noktasi olur (tek gercek kaynak)
    if(eq==null)return;
    st.live={x:Date.now(),y:Number(eq)};
    if(st.chart)render();
  }
  function render(){
    const win=getWin();
    let pts=st.pts;
    if(st.live){
      pts=pts.filter(p=>p.x<st.live.x).concat([st.live]);
    }
    const now=Date.now();
    const xmin=win>0?now-win*60000:undefined;
    // trend: TUM serinin kumulatif ortalamasi; pencere sadece goruntuyu kirpar
    let trend=st.kum;
    if(st.live){
      trend=st.kumN>0
        ?trend.concat([{x:st.live.x,y:(st.kumSum+st.live.y)/(st.kumN+1)}])
        :[{x:st.live.x,y:st.live.y}];
    }
    const roz=document.getElementById(prefix+"trend");
    if(roz){
      const hiz=trendHiz(trend);
      if(hiz==null){roz.classList.remove("up","down");}
      else{
        const yukari=hiz>=0;
        roz.textContent=(yukari?"↗ yükseliş ":"↘ düşüş ")+
          (yukari?"+":"")+hiz.toFixed(2)+" $/saat";
        roz.classList.toggle("up",yukari);
        roz.classList.toggle("down",!yukari);
      }
    }
    // dikey olcek: gorunur veri + referans ($start) HER ZAMAN kadrajda, %3 pay
    let lo=st.start,hi=st.start;
    for(const p of pts){
      if(xmin&&p.x<xmin)continue;
      if(p.y<lo)lo=p.y;
      if(p.y>hi)hi=p.y;
    }
    for(const p of trend){
      if(xmin&&p.x<xmin)continue;
      if(p.y<lo)lo=p.y;
      if(p.y>hi)hi=p.y;
    }
    const pad=Math.max((hi-lo)*0.03,hi*0.005,1);
    const ymin=lo-pad,ymax=hi+pad;
    if(!st.chart){
      st.chart=new Chart(document.getElementById(prefix+"chart"),{type:"line",
        data:{datasets:[{data:pts,borderColor:renkAlfa(renk,0.55),borderWidth:1.5,
          pointRadius:c=>c.dataIndex===c.dataset.data.length-1?3:0,
          pointBackgroundColor:renk,
          pointHitRadius:10,tension:0,
          fill:{target:{value:st.start},above:"rgba(63,185,80,.13)",below:"rgba(248,81,73,.13)"}},
         {data:trend,borderColor:"#e6edf3",borderWidth:3,borderCapStyle:"round",
          borderJoinStyle:"round",pointRadius:0,pointHitRadius:0,tension:0,
          fill:false,order:-1}]},
        options:{responsive:true,maintainAspectRatio:false,animation:false,
          interaction:{mode:"nearest",axis:"x",intersect:false},
          plugins:{legend:{display:false},tooltip:{
            filter:it=>it.datasetIndex===0,
            backgroundColor:"#161b22",borderColor:"#30363d",borderWidth:1,
            titleColor:"#c9d1d9",bodyColor:renk,displayColors:false,
            callbacks:{title:it=>new Date(it[0].parsed.x).toLocaleString("tr-TR"),
              label:it=>" $"+it.parsed.y.toFixed(2)}}},
          scales:{x:{type:"time",min:xmin,max:now,
              time:{displayFormats:{millisecond:"HH:mm:ss",second:"HH:mm:ss",minute:"HH:mm",
                hour:"dd.MM HH:mm",day:"dd.MM"}},
              ticks:{color:"#8b949e",maxTicksLimit:8,maxRotation:0},grid:{color:"#21262d"}},
            y:{min:ymin,max:ymax,ticks:{color:"#8b949e",callback:v=>"$"+v},grid:{color:"#21262d"}}}},
        plugins:[refLine]});
    }else{
      st.chart.data.datasets[0].data=pts;
      st.chart.data.datasets[1].data=trend;
      st.chart.data.datasets[0].fill.target.value=st.start;
      st.chart.options.scales.x.min=xmin;
      st.chart.options.scales.x.max=now;
      st.chart.options.scales.y.min=ymin;
      st.chart.options.scales.y.max=ymax;
      st.chart.update("none");
    }
    if(sparkId){
      let sp=st.sparkPts.length?st.sparkPts:st.pts;
      if(st.live)sp=sp.filter(p=>p.x<st.live.x).concat([st.live]);
      cizSpark(sparkId,sp,st.start);
    }
  }
  if(!shared) buttons();
  tick(); if(live) setInterval(tick,5000);
  return {tick,setLive};
}

// ---- AKTIF FILO: TEK poll, TEK hesap (/api/filo) ------------------------------------
// Senkron ilkesi: kartlar, MTM/slot rozetleri, lider secimi, kiyas satiri, islem
// tablosu ve chartlarin canli uc noktasi AYNI cevaptan basilir; ayri fetch yok.
let filoSonMs=0;
function updEtiket(){
  const sn=filoSonMs?Math.round((Date.now()-filoSonMs)/1000):null;
  const txt=sn==null?"son güncelleme: -":`son güncelleme: ${sn} sn önce`;
  for(const m of MOTORLAR){
    const e=document.getElementById(m.id+"upd");
    if(e){e.textContent=txt;e.classList.toggle("stale",sn!=null&&sn>15);}
  }
  const fb=document.getElementById("feedBadge");
  fb.textContent=sn==null?"feed: -":`feed ${sn} sn`;
  fb.classList.toggle("err",sn!=null&&sn>15);
  fb.classList.toggle("ok",sn!=null&&sn<=15);
}
setInterval(updEtiket,1000);
function basBot(m,d){
  const s=d.summary;
  const dp=s.equity-s.start_balance;
  const pct=s.start_balance?dp/s.start_balance*100:null;
  const osh=s.oldest_slot_hours;
  const badge=osh==null?"":
    `<span class="slotbadge ${osh>48?"b48":(osh>24?"b24":"")}">en yaşlı ${f(osh,1)}s</span>`;
  document.getElementById("mtm-"+m.id).innerHTML=
    `<b class="${cls(dp)}">$${f(s.equity)}</b>${badge}`;
  document.getElementById("sub-"+m.id).innerHTML=
    `<span class="${cls(pct)}">${pct>0?"+":""}${f(pct)}%</span> · ${s.trades_total} işlem`;
  const dolu="●".repeat(s.open_slots)+"○".repeat(Math.max(m.slots-s.open_slots,0));
  document.getElementById("foot-"+m.id).innerHTML=
    `slot ${dolu} ${s.open_slots}/${m.slots} · win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}`;
  document.getElementById("eq"+m.id+"label").innerHTML=
    `MTM <b class="${cls(dp)}">$${f(s.equity)}</b>`;
  eqCharts["eq"+m.id]&&eqCharts["eq"+m.id].setLive(s.equity);
  return s;
}
function basCanli(c){
  // CANLI karti: /api/filo cevabindaki canli blogundan (canli_gosterge snapshot)
  const el=document.getElementById("mtm-canli");
  if(!el)return;
  const dp=c.mtm-c.baz;
  el.innerHTML=`<b class="${cls(dp)}">$${f(c.mtm)}</b>`;
  document.getElementById("sub-canli").innerHTML=
    `<span class="${cls(c.pnl_pct)}">${c.pnl_pct>0?"+":""}${f(c.pnl_pct)}%</span> · ${c.islem_n} canlı işlem`;
  const solEl=document.getElementById("sol-canli");
  if(solEl)solEl.innerHTML=
    `serbest <b>${f(c.sol,4)} SOL</b> (~$${f(c.sol*c.sol_fiyat)})`;
  document.getElementById("foot-canli").innerHTML=
    `poz ${"●".repeat(c.acik_poz)}${"○".repeat(Math.max(5-c.acik_poz,0))} ${c.acik_poz}/5`+
    ` · baz $${f(c.baz)}`;
  const lb=document.getElementById("eqcanlilabel");
  if(lb)lb.innerHTML=`MTM <b class="${cls(dp)}">$${f(c.mtm)}</b>`;
  eqCharts["eqcanli"]&&eqCharts["eqcanli"].setLive(c.mtm);
  basCanliPoz(c.pozisyonlar||[]);
}
function basCanliPoz(rows){
  // Acik canli pozisyon tablosu: v7 state'inden canli_miktar>0 kayitlar
  const tb=document.querySelector("#canliPoz tbody");
  if(!tb)return;
  tb.innerHTML=rows.map(p=>
    `<tr><td>${p.pair}</td><td>${f(p.miktar,2)}</td>`+
    `<td>${p.giris.toPrecision(6)}</td><td>${p.guncel.toPrecision(6)}</td>`+
    `<td class="${cls(p.kz_usd)}">${f(p.kz_usd)}</td>`+
    `<td class="${cls(p.kz_pct)}">${p.kz_pct>0?"+":""}${f(p.kz_pct)}%</td></tr>`).join("")
    ||"<tr><td colspan=6>açık canlı pozisyon yok</td></tr>";
}
function basCmp(c){
  // kiyas satiri ayni /api/filo cevabinin cmp blogundan: ikinci okuma/hesap yok
  let s=`<span>Kümülatif realized PnL (her motor kendi başlangıcından): `+
    MOTORLAR.map(m=>`${m.id} <b class="${cls(c[m.id])}">$${f(c[m.id])}</b>`).join(" · ");
  if(c&&c.canli){
    const cp=c.canli;
    s+=` · canlı MTM <b class="${cls(cp.pnl_pct)}">$${f(cp.mtm)}</b>`+
      (cp.pnl_pct==null?"":` <span class="${cls(cp.pnl_pct)}">(${cp.pnl_pct>0?"+":""}${f(cp.pnl_pct)}% baza göre)</span>`);
  }
  document.getElementById("cmp3").innerHTML=s+`</span>`;
}
function exitSinif(r){
  r=r||"";
  if(r.indexOf("yarim")>=0)return "ex-amber";
  if(r.indexOf("tp")===0)return "ex-tp";
  if(r.indexOf("stop")>=0)return "ex-stop";
  if(r.indexOf("timeout")>=0)return "ex-to";
  return "";
}
function mfeMaeBar(mfe,mae){
  // orta cizgi giris; saga yesil tepe (mfe), sola kirmizi dip (mae), 30% tavanla oranli
  const W=70,H=12,mid=W/2,K=30;
  const g=Math.min(Math.abs(mfe||0),K)/K*(mid-2);
  const r=Math.min(Math.abs(mae||0),K)/K*(mid-2);
  return `<svg width="${W}" height="${H}" style="vertical-align:middle">`+
    `<rect x="${mid-r}" y="2" width="${r}" height="${H-4}" fill="#f85149" opacity=".75"/>`+
    `<rect x="${mid}" y="2" width="${g}" height="${H-4}" fill="#3fb950" opacity=".85"/>`+
    `<line x1="${mid}" y1="0" x2="${mid}" y2="${H}" stroke="#8b949e" stroke-width="1"/></svg>`;
}
function basIslemler(d){
  // son islemler iki tabloda: on plan botlari (#isltr), arka plan botlari (#isltrArka);
  // ayni /api/filo cevabindan basilir, veri uretimi degismez
  const on=[],arkaRows=[];
  for(const m of MOTORLAR)for(const t of d[m.id].trades||[])(m.arka?arkaRows:on).push([m,t]);
  // v7 CANLI satirlari: YALNIZ zincirde imzasi olan gercek islemler tabloya
  // girer; v7'nin paper/kilit-kapali kayitlari karismaz (motor gizli).
  if(!MOTORLAR.some(m=>m.id==="v7"))
    for(const t of ((d.v7||{}).trades||[]))if(t.signature)
      on.push([{ad:"V7",renk:"#e3b341",canli:true},t]);
  const fp=x=>x==null?"-":String(parseFloat(Number(x).toPrecision(5)));
  const bas=(sec,rows)=>{
    rows.sort((a,b)=>(b[1].ts||0)-(a[1].ts||0));
    document.querySelector(sec).innerHTML=rows.slice(0,40).map(([m,t])=>{
      const cn=!!(m.canli&&t.signature);
      const pnl=cn?t.canli_pnl_usd:t.pnl_usd;  // canli satirda gercek cuzdan pnl
      const sig=t.signature||"";
      const tx=sig?`<a class="txlink" href="https://solscan.io/tx/${sig}" target="_blank" rel="noopener" title="${sig}">${sig.slice(0,4)}…${sig.slice(-4)}</a>`:"";
      return `<tr class="${(pnl<0?"loss ":"")+(cn?"canli":"")}">`+
      `<td><span class="dot" style="background:${m.renk}"></span> ${m.ad}${cn?' <span class="livechip">CANLI</span>':""}</td>`+
      `<td>${t.pair}</td><td><span class="exchip ${exitSinif(t.exit_reason)}">${t.exit_reason}</span></td>`+
      `<td class="${cls(pnl)}">${f(pnl)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
      `<td title="mfe ${f(t.mfe_pct,1)}% / mae ${f(t.mae_pct,1)}%">${mfeMaeBar(t.mfe_pct,t.mae_pct)}</td>`+
      `<td>${f(t.chg_h1,1)}</td><td>${f(t.sol_chg_h1,2)}</td><td>${f(t.liq_entry,0)}</td>`+
      `<td>${fp(t.entry_price)}</td><td>${fp(t.exit_price)}</td>`+
      `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td><td>${tx}</td></tr>`;}).join("")
      ||"<tr><td colspan=14>henüz yok</td></tr>";
  };
  bas("#isltr tbody",on); bas("#isltrArka tbody",arkaRows);
}
function basRejim(d){
  const e=document.getElementById("rejimBadge");
  // birincil: paylasimli cache olcumu (deger + yas); 45 dk ustu bayat=gri/soluk
  const r=d.rejim;
  if(r&&r.sol_h1!=null&&r.yas_sec!=null){
    const dk=Math.round(r.yas_sec/60);
    const bayat=r.yas_sec>45*60;
    e.textContent=`rejim sol_h1 ${f(r.sol_h1,2)} · ${dk} dk`;
    e.className="badge"+(bayat?" bayat":(r.sol_h1>0?" ok":""));
    return;
  }
  // yedek: islem kayitlarindaki en guncel sol_chg_h1 etiketi (olcum yasi bilinmez)
  let rj=null,rt=0;
  for(const m of MOTORLAR)for(const t of d[m.id].trades||[])
    if(t.sol_chg_h1!=null&&(t.ts||0)>=rt){rt=t.ts||0;rj=t.sol_chg_h1;}
  if(rj==null){e.textContent="rejim sol_h1: -";e.className="badge";return;}
  e.textContent=`rejim sol_h1 ${f(rj,2)}`;
  e.className="badge"+(rj>0?" ok":"");
}
function basTarama(d){
  const e=document.getElementById("taramaBadge");
  const t=d.tarama;
  if(!t){e.textContent="tarama: -";e.className="badge";return;}
  e.textContent=`tarama: ${t}`;
  e.className="badge"+(t==="normal"?" ok":(t==="kor"?" err":" bayat"));
}
async function filoTick(){
  let d; try{const r=await fetch("/api/filo?limit=30"); d=await r.json();}catch(e){return;}
  filoSonMs=Date.now();
  const eqs={};
  for(const m of MOTORLAR)eqs[m.id]=basBot(m,d[m.id]).equity;
  // lider: en yuksek MTM'li bot karti (tek bot varsa rozet anlamsiz, basma)
  if(MOTORLAR.length>1){
    let lid=null,best=-Infinity;
    for(const m of MOTORLAR)if(eqs[m.id]>best){best=eqs[m.id];lid=m.id;}
    for(const m of MOTORLAR){
      const k=document.getElementById("kart-"+m.id);
      if(k)k.classList.toggle("lider",m.id===lid);
    }
  }
  if(d.canli)basCanli(d.canli);
  basCmp(d.cmp); basIslemler(d); basRejim(d); basTarama(d); basKill(d.kill);
  updEtiket();
}

// ---- Acil durdur/baslat: durum KILL dosyasindan (/api/filo kill alani) -----------
let killAktif=null;
function basKill(k){
  killAktif=!!k;
  const bant=document.getElementById("killBant");
  if(bant)bant.style.display=killAktif?"block":"none";
  const b=document.getElementById("killBtn");
  if(!b)return;
  b.disabled=false;
  b.textContent=killAktif?"BAŞLAT":"DURDUR";
  b.className=killAktif?"bas":"dur";
}
const killBtnEl=document.getElementById("killBtn");
if(killBtnEl)killBtnEl.addEventListener("click",async()=>{
  if(killAktif===null)return;
  const soru=killAktif
    ?"Kill-switch kaldırılsın mı? Filo normal akışa döner."
    :"Filo DURDURULSUN mu? Tüm motorlar yeni işlem açmayı keser.";
  if(!confirm(soru))return;
  try{await fetch("/api/kill",{method:killAktif?"DELETE":"POST"});}catch(e){}
  filoTick();
});

// ---- Canli chartlar: kart sirasiyla birebir ayni konfig listesinden ---------------
let eqSyncWin=0;
for(const m of MOTORLAR){
  eqCharts["eq"+m.id]=mkEqChart("eq"+m.id, "/api/"+m.id+"/equity", true, true,
    m.renk, "spark-"+m.id);
}
if(document.getElementById("eqcanlichart"))
  eqCharts["eqcanli"]=mkEqChart("eqcanli","/api/canli/equity",true,true,
    "#e3b341","spark-canli");
function eqSyncButtons(){
  const el=document.getElementById("eqsyncbtns");
  el.innerHTML=SYNCWINS.map(([l,m])=>
    `<button data-m="${m}" class="${m===eqSyncWin?"act":""}">${l}</button>`).join("");
  el.querySelectorAll("button").forEach(b=>b.onclick=()=>{
    eqSyncWin=Number(b.dataset.m);eqSyncButtons();
    for(const c of Object.values(eqCharts))c.tick();  // tum chartlar ayni pencereye
  });
}
eqSyncButtons();
filoTick(); setInterval(filoTick,5000);
</script></body></html>"""


@app.get("/momentum", response_class=HTMLResponse)
def momentum_page() -> str:
    """Momentum paneli: kart grid + chart sutunu _FILO_MOTORLAR listesinden uretilir;
    aktif filo /api/filo'dan 5sn'de bir TEK poll ile beslenir (tek gercek kaynak).
    arka=True motorlar ana ekran yerine katlanir "Arka plan deneyleri" bolumune
    basilir; MOTORLAR JS listesi degismez, canli guncelleme aynen surer."""
    # Mod rozeti + CANLI karti render aninda okunur (sayfa yenilemede guncel)
    mode = os.getenv("BROKER_MODE", "paper").strip().lower()
    canli_bagli = _canli_bagli_mi()
    if canli_bagli:
        mod_rozet = '<span class="badge err">CANLI (V7)</span>'
        canli_durum = "bagli"
    elif mode == "live":
        mod_rozet = '<span class="badge">live (kilit kapalı)</span>'
        canli_durum = "kilitli"
    elif mode == "dryrun":
        mod_rozet = ('<span class="badge" style="color:#e3b341;border-color:#9e6a03">'
                     'dryrun</span>')
        canli_durum = "yok"
    else:
        mod_rozet = '<span class="badge">paper</span>'
        canli_durum = "yok"
    gorunur = [m for m in _FILO_MOTORLAR if not m.get("gizli")]
    kartlar = "".join(_filo_kart(m, canli_durum)
                      for m in gorunur if not m.get("arka"))
    chartlar = "".join(_filo_chart(m, canli_durum)
                       for m in gorunur if not m.get("arka"))
    arka = "".join(
        f'<div style="max-width:300px;margin:12px 0">{_filo_kart(m)}</div>{_filo_chart(m)}'
        for m in gorunur if m.get("arka")
    )
    motor_js = json.dumps([
        {"id": m["id"], "ad": m["ad"], "renk": m["renk"], "slots": m["slots"],
         "arka": bool(m.get("arka"))}
        for m in gorunur if m["tip"] == "bot"
    ])
    html = (_MOMENTUM_HTML
            .replace("<!--KARTLAR-->", kartlar)
            .replace("<!--CHARTCOL-->", chartlar)
            .replace("<!--ARKA-->", arka)
            .replace("<!--MODROZET-->", mod_rozet)
            .replace('"__MOTORLAR__"', motor_js))
    if not arka:
        # arka plan bolumu bos: kutuyu gizle (element kalir, JS null gormesin)
        html = html.replace('<details id="arkaBox" style="',
                            '<details id="arkaBox" style="display:none;')
    return html


@app.post("/api/kill")
def api_kill_activate() -> dict:
    activate("panel")
    return {"kill_switch": True}


@app.delete("/api/kill")
def api_kill_deactivate() -> dict:
    deactivate()
    return {"kill_switch": False}


@app.post("/api/positions/{pool_address}/close")
def api_position_close(pool_address: str) -> dict:
    try:
        return engine.manual_close_position(pool_address, fraction=1.0)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/api/positions/{pool_address}/partial")
def api_position_partial(
    pool_address: str,
    fraction: float = Query(0.5, ge=0.05, le=1.0),
) -> dict:
    try:
        return engine.manual_close_position(pool_address, fraction=fraction)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/wallet/portfolio")
def api_wallet_portfolio(address: str = Query(..., min_length=42, max_length=42)) -> dict:
    if not Web3.is_address(address):
        raise HTTPException(status_code=400, detail="Geçersiz EVM adresi")
    return fetch_portfolio(settings.rpc, address)


class SolConnectBody(BaseModel):
    pubkey: str = Field(..., min_length=32, max_length=64)


class PhantomConfirmBody(BaseModel):
    trade_id: str = Field(..., min_length=8, max_length=64)
    signature: str = Field(..., min_length=32, max_length=128)


@app.get("/api/wallet/sol/portfolio")
def api_sol_wallet_portfolio(address: str = Query(..., min_length=32, max_length=64)) -> dict:
    if not is_valid_solana_address(address):
        raise HTTPException(status_code=400, detail="Geçersiz Solana adresi")
    try:
        return fetch_sol_portfolio(settings.rpc["solana"], address)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


def _sync_phantom_balance(pubkey: str, portfolio: dict) -> float:
    global _phantom_session
    _phantom_session = pubkey
    deployable = float(portfolio.get("deployable_usd") or 0.0)
    if isinstance(broker, PaperBroker):
        return broker.apply_wallet_balance(deployable, pubkey=pubkey)
    if isinstance(broker, PhantomLiveBroker):
        broker.set_phantom(pubkey, deployable)
        broker._save()
        return broker.balance
    return deployable


@app.post("/api/wallet/sol/connect")
def api_sol_wallet_connect(body: SolConnectBody) -> dict:
    pubkey = body.pubkey.strip()
    if not is_valid_solana_address(pubkey):
        raise HTTPException(status_code=400, detail="Geçersiz Solana adresi")
    try:
        portfolio = fetch_sol_portfolio(settings.rpc["solana"], pubkey)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    synced = _sync_phantom_balance(pubkey, portfolio)
    return {
        "ok": True,
        "address": pubkey,
        "portfolio": portfolio,
        "balance_synced_usd": synced,
        "mode": settings.mode,
    }


@app.delete("/api/wallet/sol/connect")
def api_sol_wallet_disconnect() -> dict:
    global _phantom_session
    _phantom_session = None
    if isinstance(broker, PhantomLiveBroker):
        broker.clear_phantom()
        broker._save()
    return {"ok": True}


@app.get("/api/phantom/pending")
def api_phantom_pending() -> dict:
    return {"pending": phantom_queue.list_pending()}


@app.post("/api/phantom/confirm")
def api_phantom_confirm(body: PhantomConfirmBody) -> dict:
    if not isinstance(broker, PhantomLiveBroker):
        raise HTTPException(status_code=400, detail="Phantom canlı mod aktif değil")
    try:
        return broker.complete_trade(body.trade_id, body.signature)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


class ScanRequest(BaseModel):
    modes: list[str] = Field(
        default_factory=lambda: ["cex", "news", "whale", "derivatives", "grid"]
    )
    limit: int = Field(default=15, ge=1, le=50)


@app.get("/api/brain")
def api_brain_state() -> dict:
    return engine.brain_state()


@app.post("/api/brain/run")
def api_brain_run() -> dict:
    try:
        return engine.request_brain_run()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/scan/modes")
def api_scan_modes() -> list:
    return list_modes()


@app.post("/api/scan")
def api_scan_run(body: ScanRequest) -> dict:
    try:
        return run_advanced_scan(body.modes, limit=body.limit)
    except Exception as exc:  # noqa: BLE001 — panel read-only
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return momentum_page()
