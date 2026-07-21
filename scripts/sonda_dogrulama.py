#!/usr/bin/env python3
"""Sonda-ve-olcekle dogrulama: gercek paper sonuclari replay tahminiyle kiyasla.

Replay hedefleri (22 Tem, anti-sonda varsayimlarla taban tahmin):
  SCALP (YZ):  E +2.35$/islem, PF 2.32, win ~%82
  RUNNER (R2): E +0.65$/islem, PF 1.09, win ~%27
Gercek sonuc bu tabanla ayni yonde ve mertebede degilse GERCEGE GUVEN,
replay varsayimlarini sorgula. Kullanim: sonda_dogrulama.py <motor> <bas_ts>
"""
import json, sys, os, time
from collections import Counter
from pathlib import Path

DATA = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
motor = sys.argv[1] if len(sys.argv) > 1 else "yz"
bas = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
rows = []
for ln in (DATA / f"{motor}_trades.jsonl").read_text().splitlines():
    if not ln.strip():
        continue
    t = json.loads(ln)
    if t.get("type") or float(t.get("ts") or 0) < bas:
        continue
    rows.append(t)
n = len(rows)
if not n:
    print(f"{motor}: sonda donemi islem yok"); sys.exit(0)
pnls = [float(t.get("pnl_usd") or 0) for t in rows]
kaz = [p for p in pnls if p > 0]; kay = [p for p in pnls if p <= 0]
pf = sum(kaz) / abs(sum(kay)) if kay and sum(kay) < 0 else float("inf")
print(f"=== SONDA DOGRULAMA: {motor} (n={n}) ===")
print(f"  E {sum(pnls)/n:+.3f}$/islem | win %{100*len(kaz)/n:.0f} | PF {pf:.2f} | net {sum(pnls):+.1f}")
print(f"  sonda durumlari: {dict(Counter(t.get('sonda_durum') or 'yok' for t in rows))}")
print(f"  ort kazanan {sum(kaz)/len(kaz) if kaz else 0:+.2f} | ort kaybeden {sum(kay)/len(kay) if kay else 0:+.2f}")
