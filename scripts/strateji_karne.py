#!/usr/bin/env python3
"""Strateji karnesi — kullanicinin kabul kriterleri (21 Tem 2026).

Bir motorun defterinden 8 metrigi hesaplar ve esiklerle karsilastirir:
  islem sayisi >=100 · win >=%60 · profit factor >1.5 · expectancy >0
  maxDD <%10 (baslangic kasasina gore) · Sharpe >1 (gunluk, yillik.)
  walk-forward pozitif (kronolojik 2. yari neti >0, parametre fit'i yok)
  canli-paper islem basi getiri farki <%15 (canli veri varsa)

Karnenin ustunde CONFIDENCE: olcumun kendisine guven (21 Tem, kullanici):
  orneklem n/(n+22) x kapsam (gun cesitliligi) x canli-dogrulama.
  8/8 gecen ama 40 islemlik strateji dusuk guvenlidir; 1000 islemlik baska.

Kullanim: python scripts/strateji_karne.py <motor>   (or: yz, v7hizli, r2...)
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

DATA = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))

ESIKLER = [
    ("islem_sayisi", ">=", 100),
    ("win_pct", ">=", 60.0),
    ("profit_factor", ">", 1.5),
    ("expectancy_usd", ">", 0.0),
    ("maxdd_pct", "<", 10.0),
    ("sharpe_yillik", ">", 1.0),
    ("walk_forward_net", ">", 0.0),
    ("canli_paper_fark_pct", "<", 15.0),
]


def pozlar(prefix: str) -> tuple[list[dict], float]:
    sp = DATA / f"{prefix}_state.json"
    created = 0.0
    start_bal = 1000.0
    if sp.exists():
        s = json.loads(sp.read_text())
        created = float(s.get("created_ts") or 0)
        start_bal = float(s.get("start_balance") or 1000.0)
    g: dict[str, list[dict]] = defaultdict(list)
    tp = DATA / f"{prefix}_trades.jsonl"
    if tp.exists():
        for ln in tp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            if t.get("type") or t.get("exit_reason") == "manuel_kapanis":
                continue
            if float(t.get("ts") or 0) < created:
                continue
            g[t.get("trade_id") or t.get("pair", "?") + str(t.get("opened_at"))].append(t)
    out = []
    for parts in g.values():
        out.append(dict(
            ts=max(float(p.get("ts") or 0) for p in parts),
            pnl=sum(float(p.get("pnl_usd") or 0) for p in parts),
            cost=sum(float(p.get("cost_usd") or 0) for p in parts),
        ))
    out.sort(key=lambda x: x["ts"])
    return out, start_bal


def karne(motor: str) -> int:
    P, start_bal = pozlar("canli" if motor == "canlim" else motor)
    m: dict[str, float | None] = {}
    n = len(P)
    m["islem_sayisi"] = n
    if n:
        m["win_pct"] = 100.0 * sum(1 for p in P if p["pnl"] > 0) / n
        bk = sum(p["pnl"] for p in P if p["pnl"] > 0)
        bz = abs(sum(p["pnl"] for p in P if p["pnl"] <= 0))
        m["profit_factor"] = bk / bz if bz > 0 else float("inf")
        m["expectancy_usd"] = sum(p["pnl"] for p in P) / n
        kum = tepe = dd = 0.0
        for p in P:
            kum += p["pnl"]
            tepe = max(tepe, kum)
            dd = min(dd, kum - tepe)
        m["maxdd_pct"] = abs(dd) / start_bal * 100.0
        gunluk: dict[str, float] = defaultdict(float)
        for p in P:
            gunluk[time.strftime("%Y-%m-%d", time.gmtime(p["ts"]))] += p["pnl"]
        gs = [v / start_bal for _, v in sorted(gunluk.items())]
        if len(gs) >= 2:
            ort = sum(gs) / len(gs)
            sig = math.sqrt(sum((x - ort) ** 2 for x in gs) / (len(gs) - 1))
            m["sharpe_yillik"] = (ort / sig * math.sqrt(365)) if sig > 0 else None
        else:
            m["sharpe_yillik"] = None
        yarim = n // 2
        m["walk_forward_net"] = sum(p["pnl"] for p in P[yarim:]) if n >= 20 else None
    # canli-paper fark: canli defterinde bu motoru kaynak alan imzali islemler
    canli_rows = []
    cp = DATA / "canli_trades.jsonl"
    if cp.exists() and motor not in ("canlim", "canli"):
        for ln in cp.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            if t.get("type") or t.get("exit_reason") == "manuel_kapanis":
                continue
            if (t.get("kaynak_motor") or "") == motor and t.get("signature"):
                canli_rows.append(float(t.get("pnl_pct") or 0))
    if canli_rows and n >= 10:
        paper_ort = sum(p["pnl"] / p["cost"] * 100 for p in P if p["cost"] > 0) / n
        canli_ort = sum(canli_rows) / len(canli_rows)
        if abs(paper_ort) > 0.01:
            m["canli_paper_fark_pct"] = abs(canli_ort - paper_ort) / abs(paper_ort) * 100.0
        else:
            m["canli_paper_fark_pct"] = None
    else:
        m["canli_paper_fark_pct"] = None

    print(f"=== STRATEJI KARNESI: {motor} (baz ${start_bal:.0f}) ===")
    gecen = 0
    toplam = 0
    for ad, op, esik in ESIKLER:
        v = m.get(ad)
        if v is None:
            print(f"  {ad:<22} VERI YOK        (esik {op}{esik})")
            continue
        toplam += 1
        ok = (v >= esik if op == ">=" else v > esik if op == ">" else v < esik)
        gecen += ok
        print(f"  {ad:<22} {v:>10.2f}  {'GECTI ' if ok else 'KALDI '} (esik {op}{esik})")
    print(f"  SONUC: {gecen}/{toplam} kriter gecti"
          + ("  -> ISTATISTIKSEL OLARAK DESTEKLI" if gecen == toplam and toplam == len(ESIKLER)
             else "  -> henuz 'iyi fikir' statusunde"))
    # ---- Confidence: olcume guven skoru --------------------------------
    gun_sayisi = len({time.strftime("%Y-%m-%d", time.gmtime(p["ts"])) for p in P})
    orneklem = max(0.02, n / (n + 22.0))
    kapsam = 0.7 + 0.3 * min(1.0, gun_sayisi / 10.0)
    dogrulama = 0.8 + 0.2 * min(1.0, len(canli_rows) / 30.0)
    conf = 100.0 * orneklem * kapsam * dogrulama
    print(f"  CONFIDENCE: %{conf:.0f}  (orneklem %{orneklem*100:.0f} [n={n}] x "
          f"kapsam %{kapsam*100:.0f} [{gun_sayisi} gun] x "
          f"canli-dogrulama %{dogrulama*100:.0f} [{len(canli_rows)} imzali])")
    return 0


if __name__ == "__main__":
    sys.exit(karne(sys.argv[1] if len(sys.argv) > 1 else "yz"))
