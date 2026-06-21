#!/usr/bin/env python3
"""Equity canli kaynagi (read-only sidecar).

Her ~12 sn'de bu reponun data/trades.jsonl + data/paper_state.json dosyalarini
OKUR ve docs/equity.json yazar. equity-chart.html ayni origin'den (/equity.json)
fetch eder -> sayfa "online" olur, CORS yok. Engine/bot dosyalarina YAZMAZ.

Calistir:  python3 docs/equity-sidecar.py
Sun:       cd docs && python3 -m http.server 8751  ->  http://localhost:8751/equity-chart.html

Cikti semasi (chart'in rebuild(d) bekledigi sekil):
  {"base": <start sermaye>, "data": [[ms, pnl_usd], ...], "events": [],
   "now": {"ms","equity","realized","unrealized","cash","deployed"}}

BASE = paper_state.start_balance (acik pozisyon olsa da sabit baseline).
equity = balance + deployed (nakit + kilitli maliyet; canli MTM yok, read-only).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRADES = ROOT / "data" / "trades.jsonl"
STATE = ROOT / "data" / "paper_state.json"
OUT = ROOT / "docs" / "equity.json"
EVENTS: list[dict] = []
INTERVAL = float(os.getenv("EQUITY_SIDECAR_SEC", "12"))


def build() -> dict | None:
    if not STATE.exists():
        return None
    state = json.loads(STATE.read_text(encoding="utf-8"))
    balance = float(state.get("balance", 0.0))
    realized = float(state.get("realized_pnl", 0.0))
    base = float(state.get("start_balance", balance))
    deployed = sum(float(p.get("cost_usd", 0.0)) for p in state.get("positions", []))

    rows: list[list] = []
    if TRADES.exists():
        for line in TRADES.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                ms = int(datetime.fromisoformat(t["closed_at"]).timestamp() * 1000)
                rows.append([ms, round(float(t["pnl_usd"]), 4)])
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
    rows.sort(key=lambda r: r[0])

    now_ms = int(time.time() * 1000)
    equity = round(balance + deployed, 2)  # nakit + kilitli maliyet (MTM yok)
    return {
        "base": round(base, 6),
        "data": rows,
        "events": EVENTS,
        "now": {
            "ms": now_ms,
            "equity": equity,
            "realized": round(realized, 2),
            "unrealized": 0.0,           # read-only: canli fiyat yok -> MTM hesaplanmaz
            "cash": round(balance, 2),
            "deployed": round(deployed, 2),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_once() -> str:
    d = build()
    if d is None:
        return "paper_state yok"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, OUT)  # atomik: yarim okuma olmaz
    return f"{len(d['data'])} trade, equity ${d['now']['equity']}, deployed ${d['now']['deployed']}"


def main() -> int:
    print(f"equity-sidecar: {OUT} her {INTERVAL:.0f}s (Ctrl+C ile durdur)")
    while True:
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {write_once()}", flush=True)
        except Exception as exc:  # sidecar asla cokmez
            print(f"sidecar hata: {exc}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
