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
from fastapi.staticfiles import StaticFiles
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
_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


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
        return
    _restore_phantom_session()
    sorunlar = settings.validate()
    if settings.mode == "live" and sorunlar:
        return
    t = threading.Thread(target=engine.run_forever, daemon=True)
    t.start()
    if os.getenv("HIBRIT_BRAIN_AUTO", "1") != "0":
        engine.schedule_brain_startup()


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
    pos_value = sum(
        float(p.get("amount_token") or 0.0) * float(p.get("last_price") or 0.0)
        for p in positions
    )
    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0.0) > 0)
    if state:
        _equity_append(data_dir, "momentum", round(balance + pos_value, 2))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": state.get("realized_pnl"),
            "equity": round(balance + pos_value, 2),
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
    """Equity serisi — trades kümülatifi + panel örneklemleri (salt-okunur birleşim)."""
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    start_bal = 1000.0
    sp = data_dir / f"{prefix}_state.json"
    if sp.exists():
        try:
            start_bal = float(json.loads(sp.read_text()).get("start_balance") or 1000.0)
        except Exception:
            pass
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
    points.sort(key=lambda p: p[0])
    if minutes > 0:
        cutoff = time.time() - minutes * 60
        older = [p for p in points if p[0] < cutoff]
        points = ([older[-1]] if older else []) + [p for p in points if p[0] >= cutoff]
    if len(points) > 1500:  # tarayıcıyı boğma
        stride = len(points) // 1500 + 1
        points = points[::stride] + [points[-1]]
    return {
        "start_balance": start_bal,
        "points": [[round(ts * 1000), eq] for ts, eq in points],
    }


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
    pos_value = sum(
        float(p.get("amount_token") or 0.0) * float(p.get("last_price") or 0.0)
        for p in positions
    )
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
        _equity_append(data_dir, "golge", round(balance + pos_value, 2))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": round(balance + pos_value, 2),
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
    pos_value = sum(
        float(p.get("amount_token") or 0.0) * float(p.get("last_price") or 0.0)
        for p in positions
    )
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
        _equity_append(data_dir, "v3", round(balance + pos_value, 2))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": round(balance + pos_value, 2),
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
    pos_value = sum(
        float(p.get("amount_token") or 0.0) * float(p.get("last_price") or 0.0)
        for p in positions
    )

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
        _equity_append(data_dir, "v4", round(balance + pos_value, 2))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": round(balance + pos_value, 2),
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
    pos_value = sum(
        float(p.get("amount_token") or 0.0) * float(p.get("last_price") or 0.0)
        for p in positions
    )

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
        _equity_append(data_dir, "v5", round(balance + pos_value, 2))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": round(balance + pos_value, 2),
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
    """V6 senaryo gözlemi (arındırılmış gölge) — v6_* dosyalarından okur + altılı kıyas.

    Kıyas satırı: her motorun KENDİ başlangıç anından bu yana realized PnL'i.
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    state: dict = {}
    sp = data_dir / "v6_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except Exception:
            state = {}
    trades: list[dict] = []
    tp = data_dir / "v6_trades.jsonl"
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
    pos_value = sum(
        float(p.get("amount_token") or 0.0) * float(p.get("last_price") or 0.0)
        for p in positions
    )

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
        _equity_append(data_dir, "v6", round(balance + pos_value, 2))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": round(balance + pos_value, 2),
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
            "v5_realized": _realized_of("v5_state.json"),
            "golge_realized": _realized_of("golge_state.json"),
        },
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 200):])),
    }


@app.get("/api/v7")
def api_v7(limit: int = Query(50)) -> dict:
    """V7 senaryo gözlemi (v6 + -%10 fren) — v7_* dosyalarından okur + aktif kıyas.

    Kıyas: her aktif motorun KENDİ başlangıç anından bu yana realized PnL'i.
    """
    data_dir = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    state: dict = {}
    sp = data_dir / "v7_state.json"
    if sp.exists():
        try:
            state = json.loads(sp.read_text())
        except Exception:
            state = {}
    trades: list[dict] = []
    tp = data_dir / "v7_trades.jsonl"
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
    pos_value = sum(
        float(p.get("amount_token") or 0.0) * float(p.get("last_price") or 0.0)
        for p in positions
    )

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
        _equity_append(data_dir, "v7", round(balance + pos_value, 2))
    return {
        "summary": {
            "balance": round(balance, 2),
            "start_balance": state.get("start_balance"),
            "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 2),
            "equity": round(balance + pos_value, 2),
            "open_slots": len(positions),
            "trades_total": len(trades),
            "wins": wins,
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
            "exit_reasons": reasons,
            "since": state.get("updated_at"),
            "created_ts": float(state.get("created_ts") or 0.0),
            "v4_realized": _realized_of("v4_state.json"),
            "v6_realized": _realized_of("v6_state.json"),
            "golge_realized": _realized_of("golge_state.json"),
        },
        "positions": positions,
        "trades": list(reversed(trades[-min(limit, 200):])),
    }


@app.get("/momentum", response_class=HTMLResponse)
def momentum_page() -> str:
    """Momentum modu mini paneli — /api/momentum'u 5sn'de bir yeniler."""
    return """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>Momentum v2</title>
<style>
 body{background:#0d1117;color:#c9d1d9;font:13px/1.5 monospace;margin:16px}
 h2{color:#58a6ff;margin:12px 0 6px} .pos{color:#3fb950} .neg{color:#f85149}
 table{border-collapse:collapse;width:100%;margin-bottom:14px}
 th,td{border:1px solid #30363d;padding:3px 8px;text-align:right;white-space:nowrap}
 th{background:#161b22;color:#8b949e} td:first-child,th:first-child{text-align:left}
 .chip{display:inline-block;padding:0 6px;border-radius:8px;background:#21262d}
 #sum span{margin-right:18px}
 .eqbtns{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 8px}
 .eqbtns button{background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:6px;
   padding:2px 10px;cursor:pointer;font:inherit}
 .eqbtns button.act{background:#1f6feb;color:#fff;border-color:#1f6feb}
 .eqwrap{position:relative;width:100%;height:280px;margin-bottom:16px}
 .eqlabel{color:#8b949e;margin:8px 0 2px} .eqlabel b{color:#c9d1d9}
 @media(max-width:600px){.eqwrap{height:220px}}
</style></head><body>
<h2>AKTİF YARIŞ · v4 / gölge / v6 / v7</h2>
<div id="cmp3" style="margin:4px 0 12px">yükleniyor…</div>
<h2>V4 Melez Senaryo (sanal, v3 girişi h1 5..15 · kademeli trail -3/-6 · karda 120dk tavan)</h2>
<div id="v4sum">yükleniyor…</div>
<table id="v4tr"><thead><tr><th>pair</th><th>exit_reason</th><th>kademe</th><th>pnl $</th>
<th>pnl%</th><th>mfe%</th><th>mae%</th><th>chg_m5</th><th>chg_h1</th><th>sol_h1</th>
<th>hold sn</th><th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Gölge Senaryo (sanal, liq&ge;$100k + h1&ge;10 · TP+2 · 30dk sabır sonrası stop-2 · 60dk tavan)</h2>
<div id="gsum">yükleniyor…</div>
<table id="gtr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_h1</th><th>hold sn</th><th>kapanış</th></tr></thead>
<tbody></tbody></table>
<h2>V6 Senaryo (sanal, arındırılmış gölge: liq&ge;$100k + h1 10..50 · tp+2 · 30dk sabır · stop-2 · 60dk)</h2>
<div id="v6sum">yükleniyor…</div>
<table id="v6tr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_h1</th><th>sol_h1</th><th>liq $</th><th>hold sn</th>
<th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>V7 Senaryo (sanal, v6 + TEK fark: -%10 felaket freni · sabır iptal, anında sat)</h2>
<div id="v7sum">yükleniyor…</div>
<table id="v7tr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_h1</th><th>sol_h1</th><th>liq $</th><th>hold sn</th>
<th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>CANLI KIYAS · senkron equity (v4 · gölge · v6 · v7)</h2>
<div class="eqbtns" id="eqsyncbtns"></div>
<div id="eqv4label" class="eqlabel">V4 MELEZ</div>
<div class="eqwrap"><canvas id="eqv4chart"></canvas></div>
<div id="eqglabel" class="eqlabel">GÖLGE</div>
<div class="eqwrap"><canvas id="eqgchart"></canvas></div>
<div id="eqv6label" class="eqlabel">V6</div>
<div class="eqwrap"><canvas id="eqv6chart"></canvas></div>
<div id="eqv7label" class="eqlabel">V7</div>
<div class="eqwrap"><canvas id="eqv7chart"></canvas></div>
<details id="arsivBox" style="margin-top:28px;border-top:1px solid #30363d;padding-top:8px">
<summary style="cursor:pointer;color:#8b949e"><b>ARŞİV · durdurulan motorlar (v2 · v3 · v5) · tıkla aç</b></summary>
<div id="arsivIc">
<h2>MOMENTUM v2 (durduruldu — slot 5 · liq&ge;$40k · m5&gt;0 · h1 5..50 · stop-2/BE+3/trail 5/-3 · 60dk)</h2>
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
<h2>V3 Senaryo (durduruldu — h1 5..15 düşük önce · rejim&ge;0.5 · BE+1.5 · cooldown 45dk)</h2>
<div id="v3sum">arşiv, açınca yüklenir…</div>
<table id="v3tr"><thead><tr><th>pair</th><th>exit_reason</th><th>pnl $</th><th>pnl%</th>
<th>mfe%</th><th>mae%</th><th>chg_m5</th><th>chg_h1</th><th>sol_h1</th><th>hold sn</th>
<th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (V3)</h2>
<div class="eqbtns" id="eqv3btns"></div>
<div class="eqwrap"><canvas id="eqv3chart"></canvas></div>
<h2>V5 Senaryo (durduruldu — gölge zemini + taban -%8 · tp yarım + koşucu trail -3/be+1.5)</h2>
<div id="v5sum">arşiv, açınca yüklenir…</div>
<table id="v5tr"><thead><tr><th>pair</th><th>exit_reason</th><th>koşucu</th><th>pnl $</th>
<th>pnl%</th><th>mfe%</th><th>mae%</th><th>chg_h1</th><th>liq $</th><th>hold sn</th>
<th>kapanış</th></tr></thead><tbody></tbody></table>
<h2>Equity (V5)</h2>
<div class="eqbtns" id="eqv5btns"></div>
<div class="eqwrap"><canvas id="eqv5chart"></canvas></div>
</div>
</details>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<script>
const f=(x,d=2)=>x==null?"-":Number(x).toFixed(d);
const cls=x=>x>0?"pos":(x<0?"neg":"");

// ---- AKTIF: Golge -----------------------------------------------------------
async function gtick(){
  let d; try{const r=await fetch("/api/golge?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("gsum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.getElementById("eqglabel").innerHTML=
    `GÖLGE · equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b>`;
  document.querySelector("#gtr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_h1,1)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=9>henüz yok</td></tr>";
}
gtick(); setInterval(gtick,5000);

// ---- AKTIF: V4 melez ----------------------------------------------------------
async function v4tick(){
  let d; try{const r=await fetch("/api/v4?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("v4sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.getElementById("eqv4label").innerHTML=
    `V4 MELEZ · equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b>`;
  document.querySelector("#v4tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td>${t.trail_kademe==null?"-":t.trail_kademe}</td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_m5,1)}</td>`+
    `<td>${f(t.chg_h1,1)}</td><td>${f(t.sol_chg_h1,2)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=12>henüz yok</td></tr>";
}
v4tick(); setInterval(v4tick,5000);

// ---- AKTIF: V6 (arindirilmis golge) + uclu kiyas satiri --------------------------
async function v6tick(){
  let d; try{const r=await fetch("/api/v6?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("v6sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.getElementById("eqv6label").innerHTML=
    `V6 · equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b>`;
  document.querySelector("#v6tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_h1,1)}</td>`+
    `<td>${f(t.sol_chg_h1,2)}</td><td>${f(t.liq_entry,0)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=11>henüz yok</td></tr>";
}
v6tick(); setInterval(v6tick,5000);

// ---- AKTIF: V7 (v6 + -%10 fren) + dortlu kiyas satiri ---------------------------
async function v7tick(){
  let d; try{const r=await fetch("/api/v7?limit=30"); d=await r.json();}catch(e){return;}
  const s=d.summary;
  document.getElementById("v7sum").innerHTML=
    `<span>bakiye <b>$${f(s.balance)}</b></span><span>equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b></span>`+
    `<span>realized <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`+
    `<span>işlem ${s.trades_total}</span><span>win ${s.win_rate_pct==null?"-":s.win_rate_pct+"%"}</span>`+
    `<span>açık ${s.open_slots}/5</span>`+
    `<span>${Object.entries(s.exit_reasons||{}).map(([k,v])=>`<span class="chip">${k}:${v}</span>`).join(" ")}</span>`;
  document.getElementById("cmp3").innerHTML=
    `<span>Kümülatif realized PnL (her motor kendi başlangıcından): `+
    `v4 <b class="${cls(s.v4_realized)}">$${f(s.v4_realized)}</b> · `+
    `gölge <b class="${cls(s.golge_realized)}">$${f(s.golge_realized)}</b> · `+
    `v6 <b class="${cls(s.v6_realized)}">$${f(s.v6_realized)}</b> · `+
    `v7 <b class="${cls(s.realized_pnl)}">$${f(s.realized_pnl)}</b></span>`;
  document.getElementById("eqv7label").innerHTML=
    `V7 · equity <b class="${cls(s.equity-s.start_balance)}">$${f(s.equity)}</b>`;
  document.querySelector("#v7tr tbody").innerHTML=(d.trades||[]).map(t=>
    `<tr><td>${t.pair}</td><td><span class="chip">${t.exit_reason}</span></td>`+
    `<td class="${cls(t.pnl_usd)}">${f(t.pnl_usd)}</td><td class="${cls(t.pnl_pct)}">${f(t.pnl_pct)}</td>`+
    `<td>${f(t.mfe_pct,1)}</td><td>${f(t.mae_pct,1)}</td><td>${f(t.chg_h1,1)}</td>`+
    `<td>${f(t.sol_chg_h1,2)}</td><td>${f(t.liq_entry,0)}</td>`+
    `<td>${f(t.hold_sec,0)}</td><td>${(t.closed_at||"").slice(11,19)}</td></tr>`).join("")||"<tr><td colspan=11>henüz yok</td></tr>";
}
v7tick(); setInterval(v7tick,5000);

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
document.getElementById("arsivBox").addEventListener("toggle",e=>{
  if(!e.target.open||arsivYuklendi)return;
  arsivYuklendi=true;  // BIR kez: donuk ozet + donuk chartlar (interval yok)
  arsivV2(); arsivV3(); arsivV5();
  mkEqChart("eqv2","/api/momentum/equity",false);
  mkEqChart("eqv3","/api/v3/equity",false);
  mkEqChart("eqv5","/api/v5/equity",false);
});

// ---- Equity chart'lari (v2 / gölge / v3, aynı görsel dil) -------------------
const EQWINS=[["5dk",5],["15dk",15],["30dk",30],["1s",60],["2s",120],["5s",300],
  ["12s",720],["24s",1440],["48s",2880],["1h",10080],["2h",20160],["Tümü",0]];
function mkEqChart(prefix, api, live=true, shared=false){
  // shared=true: kendi buton seridi yok, pencereyi ortak eqSyncWin belirler
  const st={win:0, chart:null, start:1000};
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
    const pts=(d.points||[]).map(p=>({x:p[0],y:p[1]}));
    const now=Date.now();
    const xmin=win>0?now-win*60000:undefined;
    if(!st.chart){
      st.chart=new Chart(document.getElementById(prefix+"chart"),{type:"line",
        data:{datasets:[{data:pts,borderColor:"#58a6ff",borderWidth:1.5,pointRadius:0,
          pointHitRadius:10,tension:0,
          fill:{target:{value:st.start},above:"rgba(63,185,80,.13)",below:"rgba(248,81,73,.13)"}}]},
        options:{responsive:true,maintainAspectRatio:false,animation:false,
          interaction:{mode:"nearest",axis:"x",intersect:false},
          plugins:{legend:{display:false},tooltip:{
            backgroundColor:"#161b22",borderColor:"#30363d",borderWidth:1,
            titleColor:"#c9d1d9",bodyColor:"#58a6ff",displayColors:false,
            callbacks:{title:it=>new Date(it[0].parsed.x).toLocaleString("tr-TR"),
              label:it=>" $"+it.parsed.y.toFixed(2)}}},
          scales:{x:{type:"time",min:xmin,max:now,
              time:{displayFormats:{millisecond:"HH:mm:ss",second:"HH:mm:ss",minute:"HH:mm",
                hour:"dd.MM HH:mm",day:"dd.MM"}},
              ticks:{color:"#8b949e",maxTicksLimit:8,maxRotation:0},grid:{color:"#21262d"}},
            y:{ticks:{color:"#8b949e",callback:v=>"$"+v},grid:{color:"#21262d"}}}},
        plugins:[refLine]});
    }else{
      st.chart.data.datasets[0].data=pts;
      st.chart.data.datasets[0].fill.target.value=st.start;
      st.chart.options.scales.x.min=xmin;
      st.chart.options.scales.x.max=now;
      st.chart.update("none");
    }
  }
  if(!shared) buttons();
  tick(); if(live) setInterval(tick,5000);
  return tick;
}

// ---- Canli Kiyas: uc chart, TEK ortak zaman filtresi seridi (senkron) ---------
let eqSyncWin=0;
const eqSyncTicks=[
  mkEqChart("eqv4","/api/v4/equity",true,true),
  mkEqChart("eqg","/api/golge/equity",true,true),
  mkEqChart("eqv6","/api/v6/equity",true,true),
  mkEqChart("eqv7","/api/v7/equity",true,true),
];
function eqSyncButtons(){
  const el=document.getElementById("eqsyncbtns");
  el.innerHTML=EQWINS.map(([l,m])=>
    `<button data-m="${m}" class="${m===eqSyncWin?"act":""}">${l}</button>`).join("");
  el.querySelectorAll("button").forEach(b=>b.onclick=()=>{
    eqSyncWin=Number(b.dataset.m);eqSyncButtons();
    eqSyncTicks.forEach(t=>t());  // uc chart birden ayni pencereye
  });
}
eqSyncButtons();
</script></body></html>"""


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


def _static_asset_version() -> int:
    static = Path(__file__).resolve().parent / "static"
    return int(
        max(
            static.joinpath("panel.css").stat().st_mtime,
            static.joinpath("panel-quantum.css").stat().st_mtime,
            static.joinpath("panel.js").stat().st_mtime,
        )
    )


_ASSET_V = _static_asset_version()

HTML = """<!DOCTYPE html>
<html lang="tr" class="dark" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hybrid Trade · Vault Command</title>
<script>
(function(){var t=localStorage.getItem("theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t);})();
</script>
<link rel="stylesheet" href="/static/panel.css">
<link rel="stylesheet" href="/static/panel-quantum.css">
<script src="https://unpkg.com/@solana/web3.js@1.95.8/lib/index.iife.min.js"></script>
</head>
<body class="qc-body qc-profit-focus">
<div class="app qc-app">
  <nav class="qc-nav ob-nav">
    <div class="qc-brand">
      <div class="qc-brand-text">
        <span class="qc-logo ob-logo-anim" aria-label="Hybrid Trade">HYBRID<span class="qc-logo-accent">TRADE</span></span>
        <span class="qc-tagline">Vault Command</span>
      </div>
      <span class="qc-mode-badge mode-badge" id="modeBadge">—</span>
    </div>
    <div class="qc-nav-links ob-nav-links">
      <a class="qc-nav-link" href="#watchlistPanel">Trending</a>
      <a class="qc-nav-link" href="#positionsPanel">Pozisyon</a>
      <a class="qc-nav-link" href="#decisionPanel">Karar</a>
    </div>
    <div class="qc-nav-actions">
      <button type="button" class="qc-icon-btn" id="themeToggle" title="Tema" aria-label="Tema">
        <span class="material-symbols-outlined" id="themeIconSun">light_mode</span>
        <span class="material-symbols-outlined" id="themeIconMoon" style="display:none">dark_mode</span>
      </button>
      <button type="button" class="qc-btn-wallet ob-btn-connect" id="phantomBtn">Phantom</button>
      <p class="qc-subtitle" id="subtitle">Yükleniyor…</p>
    </div>
  </nav>

  <div class="qc-dash">
    <div class="qc-ambient" aria-hidden="true"></div>

    <div class="qc-dash-metrics qc-glass">
      <article class="qc-metric qc-metric-compact">
        <div class="qc-metric-head"><span class="qc-label">Bakiye</span></div>
        <div class="qc-value-lg" id="hudBalance">—</div>
        <div class="qc-delta" id="hudPnlDelta"><span id="hudSessionPnl">—</span><span class="qc-delta-sub" id="hudSessionPct"></span></div>
      </article>
      <article class="qc-metric qc-metric-compact">
        <div class="qc-metric-head"><span class="qc-label">Sermaye</span></div>
        <div class="qc-value-lg" id="hudEquityMetric">—</div>
        <div class="qc-metric-sub" id="hudRealizedLine"></div>
      </article>
      <article class="qc-metric qc-metric-compact">
        <div class="qc-metric-head"><span class="qc-label">Kazanma oranı</span></div>
        <div class="qc-value-md" id="hudWinrate">—</div>
        <div class="qc-progress"><div class="qc-progress-bar" id="hudWinBar" style="width:0%"></div></div>
      </article>
      <article class="qc-metric qc-metric-compact">
        <div class="qc-metric-head"><span class="qc-label">Pozisyon</span></div>
        <div class="qc-value-lg" id="hudPositionUsd">$0.00</div>
        <div class="qc-metric-sub" id="hudPositionMeta">0 açık · $0.00 değer</div>
        <div class="qc-status-dot live" id="hudSignalDot"><span class="dot"></span><span id="hudSignalText">Bekleniyor</span></div>
      </article>
    </div>

    <section class="panel hud-deck glass qc-section qc-bento-tile qc-tile-positions qc-positions-hero" id="positionsPanel">
      <div class="hud-deck-head">
        <h2 class="hud-deck-title">Açık Pozisyonlar</h2>
        <span class="hud-deck-badge qc-pos-live">CANLI</span>
        <span class="hud-deck-hint section-hint" id="positionsModeHint"></span>
      </div>
      <div class="qc-positions-total-bar" id="positionsTotalBar" aria-live="polite">
        <span class="qc-pos-total-item"><span class="qc-label">Yatırılan</span> <strong id="positionsTotalCost">$0.00</strong></span>
        <span class="qc-pos-total-item"><span class="qc-label">Güncel değer</span> <strong id="positionsTotalValue">$0.00</strong></span>
        <span class="qc-pos-total-item"><span class="qc-label">Açık kar/zarar</span> <strong id="positionsTotalPnl" class="qc-pos-total-pnl">$0.00</strong></span>
        <span class="qc-pos-total-item qc-pos-total-count"><span class="qc-label">Adet</span> <strong id="positionsTotalCount">0</strong></span>
      </div>
      <div class="table-wrap hud-table-wrap qc-positions-table-wrap"><table class="hud-table qc-positions-table"><thead><tr><th>Çift</th><th>Ağ</th><th>Giriş</th><th>Güncel (DEX)</th><th>Çıkış teklifi</th><th>Maliyet</th><th>PnL</th><th>Skor</th><th>İşlem</th></tr></thead>
      <tbody id="positions"><tr class="empty-row"><td colspan="9">Yükleniyor…</td></tr></tbody>
      <tfoot id="positionsFoot"><tr class="qc-pos-total-row"><td colspan="5"><strong>Toplam</strong></td><td id="positionsFootCost"><strong>$0.00</strong></td><td id="positionsFootPnl"><strong>$0.00</strong></td><td colspan="2"></td></tr></tfoot></table></div>
    </section>

    <section class="panel hud-deck glass qc-section qc-bento-tile qc-tile-watch qc-tile-dex-trend" id="watchlistPanel">
      <div class="hud-deck-head">
        <h2 class="hud-deck-title" id="watchlistTitle">DEX Trending</h2>
        <span class="hud-deck-badge hud-deck-badge-scan">DEXSCREENER</span>
      </div>
      <div class="table-wrap hud-table-wrap dex-trend-wrap"><table class="hud-table dex-trend-table"><thead><tr><th>#</th><th>Token</th><th>MCAP</th><th>Age</th><th>Vol 24h</th><th>Txns</th><th>Cüzdan</th><th>Turn</th><th>5M</th><th>1H</th><th>24H</th><th>Liq</th><th>🎯</th><th>Skor</th></tr></thead>
      <tbody id="watchlist"></tbody></table></div>
    </section>

    <section class="pb-terminal qc-glass" id="saitoHub">
      <div class="pb-term-bar">
        <span class="pb-term-user">hibrit@dex</span>
        <span class="pb-term-path">~/live/terminal</span>
        <span class="pb-term-badge" id="obNetwork">PAPER · DEX</span>
        <span class="pb-term-status" id="pbApiStatus"><span class="pb-pulse"></span> STREAMING</span>
        <span class="pb-term-motor" id="hudBotStatus">ONLINE</span>
        <div class="saito-core pb-status-orb" id="saitoBrainVisual" aria-hidden="true"></div>
      </div>
      <div class="pb-formula-row" aria-hidden="true">
        <div class="pb-formula-col">
          <span class="pb-formula-k">BAYESIAN MODEL</span>
          <span class="pb-formula-v" id="pbFormulaBayes">P(H|D) = P(D|H)·P(H)/P(D)</span>
        </div>
        <div class="pb-formula-col">
          <span class="pb-formula-k">EDGE + SPREAD</span>
          <span class="pb-formula-v" id="pbFormulaEdge">EV_net = q·p − c</span>
        </div>
        <div class="pb-formula-col">
          <span class="pb-formula-k">EXECUTION LAYER</span>
          <span class="pb-formula-v" id="pbFormulaExec">peak trail · dynamic exit</span>
        </div>
      </div>
      <div class="pb-path-stage">
        <canvas id="pbPathFan" width="960" height="220" aria-label="Probability path fan"></canvas>
        <span class="pb-path-origin" id="pbPathLabel">scan → entry paths</span>
      </div>
      <div class="pb-term-grid">
        <div class="pb-col pb-col-chart">
          <div class="pb-col-head">
            <span class="pb-col-title">EQUITY</span>
            <span class="pb-col-val" id="hudEquity">$—</span>
            <span class="pb-col-sub" id="liveSimDesc">—</span>
          </div>
          <div class="pb-chart-wrap">
            <canvas id="pbEquityChart" width="480" height="128" aria-label="Equity chart"></canvas>
          </div>
          <div class="pb-chart-legend">
            <span id="pbChartHigh">H —</span>
            <span id="pbChartLow">L —</span>
            <span id="obTrendStatus">SCAN</span>
          </div>
          <canvas id="hudSparkline" width="1" height="1" hidden aria-hidden="true"></canvas>
        </div>
        <div class="pb-col pb-col-depth">
          <div class="pb-col-head">
            <span class="pb-col-title">MOMENTUM DEPTH</span>
            <span class="pb-col-sub" id="pbDepthLabel">DEX trending</span>
          </div>
          <div class="pb-depth-book" id="pbDepthBars"></div>
        </div>
        <div class="pb-col pb-col-tape">
          <div class="pb-col-head">
            <span class="pb-col-title">TRAINING STREAM</span>
            <span class="pb-col-sub" id="pbTapeCount">0 events</span>
          </div>
          <ul class="pb-tape pb-stream" id="pbTape"></ul>
        </div>
      </div>
      <div class="qc-hub-metrics ob-metrics qc-hidden-legacy" id="hudOrbitMetrics"></div>
    </section>

    <section class="qc-glass qc-section qc-bento-tile qc-tile-trend" id="trendPanel">
      <div class="hud-deck-head">
        <h2 class="hud-deck-title">Trend Stack</h2>
        <span class="hud-deck-badge">PRIMARY · SUPERTREND</span>
      </div>
      <div class="trend-stack-grid" id="trendGrid">
        <div class="trend-pill" data-ind="st"><span class="trend-pill-k">Supertrend</span><span class="trend-pill-v" id="trendST">—</span></div>
        <div class="trend-pill" data-ind="ht"><span class="trend-pill-k">HalfTrend</span><span class="trend-pill-v" id="trendHT">—</span></div>
        <div class="trend-pill" data-ind="ut"><span class="trend-pill-k">UT Bot</span><span class="trend-pill-v" id="trendUT">—</span></div>
        <div class="trend-pill" data-ind="ce"><span class="trend-pill-k">Chandelier</span><span class="trend-pill-v" id="trendCE">TRAIL</span></div>
      </div>
      <div class="trend-status hud-status-bar" id="trendStatus">Motor giriş için trend + konfluans tarıyor…</div>
      <div class="trend-entry-bar" id="trendEntryBar">
        <span class="qc-label">Giriş modu</span>
        <strong id="trendEntryMode">AGGRESSIVE</strong>
        <span class="muted" id="trendConfluenceMin">min 52</span>
      </div>
    </section>

    <section class="panel hud-deck decision-panel glass qc-section qc-bento-tile qc-decision-strip" id="decisionPanel">
      <div class="qc-decision-inline">
        <span class="qc-decision-tag">BOT</span>
        <span class="qc-decision-chip"><span class="qc-label">Giriş</span> <strong id="decEntryMin">—</strong></span>
        <span class="qc-decision-chip"><span class="qc-label">SL/TP</span> <strong id="decTpSl">—</strong></span>
        <span class="qc-decision-chip qc-decision-macro"><span class="qc-label">Makro</span> <strong id="decMacro">—</strong></span>
        <span id="decRunnerTrail" hidden></span>
        <span class="qc-decision-last-inline" id="decisionLast">Son karar: henüz tick yok</span>
      </div>
      <details class="entry-diag-wrap entry-diag-collapsed" id="entryDiagPanel">
        <summary class="entry-diag-head">
          <span class="qc-label">Giriş teşhisi</span>
          <span class="entry-diag-summary" id="entryDiagSummary">—</span>
        </summary>
        <div class="table-wrap hud-table-wrap entry-diag-table-wrap">
          <table class="hud-table entry-diag-table">
            <thead><tr><th>Çift</th><th>DEX</th><th>Konf.</th><th>CEX</th><th>Engel</th></tr></thead>
            <tbody id="entryDiagBody"><tr class="empty-row"><td colspan="5">Tick bekleniyor…</td></tr></tbody>
          </table>
        </div>
      </details>
      <span id="decExitMax" hidden></span>
      <span id="decExitLadder" hidden></span>
    </section>

    <section class="qc-glass qc-section qc-bento-tile qc-tile-bar qc-chain-bar" id="walletBar">
      <div class="live-sim-tags hud-tags" id="liveSimTags"></div>
      <div class="chain-ops" id="chainOps"></div>
      <div class="wallet-status" id="walletStatus">Cüzdan bağlı değil</div>
      <div class="wallet-holdings" id="walletHoldings" hidden>
        <div class="holdings-title">On-chain bakiyeler</div>
        <div class="table-wrap holdings-table"><table>
          <thead><tr><th>Ağ</th><th>Coin</th><th>Miktar</th></tr></thead>
          <tbody id="holdingsBody"></tbody>
        </table></div>
      </div>
    </section>

    <aside class="qc-bento-tile qc-tile-side">
      <div class="qc-glass qc-ledger qc-section" id="tradesPanel">
        <div class="qc-ledger-head">
          <span class="qc-label">Recent Executions</span>
        </div>
        <div class="qc-ledger-scroll">
          <table><thead><tr><th>PAIR</th><th>TYPE</th><th class="text-right">PNL</th><th class="text-right">REASON</th></tr></thead>
          <tbody id="trades"><tr class="empty-row"><td colspan="4">Yükleniyor…</td></tr></tbody></table>
        </div>
      </div>
    </aside>

    <section class="panel hud-deck glass qc-section qc-bento-tile qc-tile-growth" id="growthPanel">
      <div class="hud-deck-head">
        <h2 class="hud-deck-title">Artış Potansiyeli</h2>
        <span class="hud-deck-badge">DEX + CEX ERKEN</span>
        <span class="hud-deck-hint section-hint" id="growthSummary">—</span>
      </div>
      <div class="table-wrap hud-table-wrap"><table class="hud-table growth-table">
        <thead><tr><th>Durum</th><th>Upside</th><th>Çift</th><th>Ağ</th><th>Bekl. %</th><th>Sinyaller</th></tr></thead>
        <tbody id="growthBody"><tr class="empty-row"><td colspan="6">Tick bekleniyor…</td></tr></tbody>
      </table></div>
    </section>

    <section class="panel hud-deck glass market-intel-panel qc-section qc-bento-tile qc-tile-market" id="marketIntelPanel">
      <div class="hud-deck-head">
        <h2 class="hud-deck-title">Piyasa Zekâsı</h2>
        <span class="hud-deck-badge">SMART MONEY · CEX</span>
      </div>
      <div class="market-intel-grid">
        <div class="market-intel-col">
          <h3 class="market-intel-title">Balina / 3+ Cüzdan</h3>
          <div class="table-wrap hud-table-wrap"><table class="hud-table market-intel-table">
            <thead><tr><th>Coin</th><th>Cüzdan</th><th>Hacim</th><th>H1%</th><th>Sinyal</th></tr></thead>
            <tbody id="whaleTable"><tr class="empty-row"><td colspan="5">Yükleniyor…</td></tr></tbody>
          </table></div>
        </div>
        <div class="market-intel-col">
          <h3 class="market-intel-title">Binance — Tutulabilir</h3>
          <div class="table-wrap hud-table-wrap"><table class="hud-table market-intel-table">
            <thead><tr><th>Coin</th><th>Skor</th><th>24s Hacim</th><th>Gerekçe</th></tr></thead>
            <tbody id="binanceHolds"><tr class="empty-row"><td colspan="4">Yükleniyor…</td></tr></tbody>
          </table></div>
        </div>
        <div class="market-intel-col">
          <h3 class="market-intel-title">OKX — Tutulabilir</h3>
          <div class="table-wrap hud-table-wrap"><table class="hud-table market-intel-table">
            <thead><tr><th>Coin</th><th>Skor</th><th>24s Hacim</th><th>Gerekçe</th></tr></thead>
            <tbody id="okxHolds"><tr class="empty-row"><td colspan="4">Yükleniyor…</td></tr></tbody>
          </table></div>
        </div>
      </div>
    </section>

    <section class="panel hud-deck scan-panel glass qc-section qc-bento-tile qc-tile-scan" id="scanPanel">
      <div class="hud-deck-head scan-head">
        <h2 class="hud-deck-title scan-title">Gelişmiş Tarama</h2>
        <button type="button" class="btn btn-primary hud-btn" id="runScanBtn">Taramayı çalıştır</button>
      </div>
      <div class="scan-modes hud-mode-pills" id="scanModes"></div>
      <div class="scan-status hud-status-bar" id="scanStatus">Modlar yüklendi — taramayı çalıştır</div>
      <div class="table-wrap hud-table-wrap"><table class="hud-table">
        <thead><tr><th></th><th>Coin</th><th>Borsa</th><th>Skor</th><th>Gerekçe</th></tr></thead>
        <tbody id="scanResults"><tr class="empty-row"><td colspan="5">Henüz tarama yok</td></tr></tbody>
      </table></div>
    </section>
  </div>

  <div class="qc-hidden-legacy" aria-hidden="true">
    <div class="hud-metrics-float" id="hudMetrics">
      <span id="hudPnlOrb"></span>
    </div>
    <section id="hudCockpit"></section>
    <div class="cards cards-legacy">
      <div class="card"><div class="value" id="balance">—</div></div>
      <div class="card"><div class="value" id="pnl">—</div></div>
      <div class="card"><div class="value" id="open">—</div></div>
      <div class="card"><div class="value" id="winrate">—</div></div>
    </div>
    <section id="liveSimPanel"></section>
    <span id="hudWinBreakdown"></span>
    <span id="decBrainPenalty"></span>
  </div>
</div>
<script src="/static/panel.js"></script>
</body>
</html>"""

HTML = HTML.replace("/static/panel.css", f"/static/panel.css?v={_ASSET_V}").replace(
    "/static/panel-quantum.css", f"/static/panel-quantum.css?v={_ASSET_V}"
).replace(
    "/static/panel.js", f"/static/panel.js?v={_ASSET_V}"
)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML
