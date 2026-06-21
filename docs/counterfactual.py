#!/usr/bin/env python3
"""Counterfactual / shadow analiz (read-only). Engine'e DOKUNMAZ, gercek islem ACMAZ.

data/exits.jsonl'deki kayitli fiyat profilinden (entry, early_ticks, peak/trough+ts,
mae/mfe, exit) iki "ne olurdu" sorusunu hesaplar:

  A) SCRATCH ESIGI: "scratch -9 yerine -12 olsaydi bu trade'ler nasil biterdi?"
     Model (tighter-only, muhafazakar): bir pozisyonun en dip noktasi (mae_pct)
     esigi S gectiyse, o pozisyon S%'te scratch'lenirdi -> cf_pnl = S.
     Gecmediyse cf_pnl = gercek pnl. Cikis sonrasi veri olmadigi icin yalniz
     GERCEKTEN-DAHA-ERKEN tetiklenecek (daha siki) esikler modellenir.

  B) KADEMELI GIRIS: early_ticks ile ilk ~2dk'da 2-3 dilimde girilseydi harmanlanmis
     maliyet ve ayni cikista pnl ne olurdu? (kademeli giris karari icin)

Cikti: ekrana okunur rapor + docs/cf-shadow.jsonl'e ozet satiri (append, gitignore).
Calistir: python3 docs/counterfactual.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXITS = ROOT / "data" / "exits.jsonl"
OUT = ROOT / "docs" / "cf-shadow.jsonl"

SCRATCH_GRID = [-4.0, -6.0, -9.0, -12.0, -15.0]  # taranacak scratch esikleri (%)
STAGE_TARGETS = [0, 60, 120]                       # kademeli giris dilim saniyeleri


def load_exits() -> list[dict]:
    if not EXITS.exists():
        return []
    out = []
    for ln in EXITS.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def scratch_sweep(rows: list[dict]) -> dict:
    """Her esik S icin: gercek pnl toplami vs counterfactual pnl toplami."""
    base_sum = sum(r.get("pnl_pct") or 0 for r in rows)
    result = {}
    for S in SCRATCH_GRID:
        cf_sum, helped, hurt, fired = 0.0, 0, 0, 0
        for r in rows:
            pnl = r.get("pnl_pct")
            mae = r.get("mae_pct")
            if pnl is None:
                continue
            if mae is not None and mae <= S:
                cf = S            # esigi gecti -> scratch S'te cikardi
                fired += 1
                d = cf - pnl
                if d > 0.01:
                    helped += 1
                elif d < -0.01:
                    hurt += 1
            else:
                cf = pnl          # esik tetiklenmez -> degismez
            cf_sum += cf
        result[S] = {
            "cf_total_pnl_pct": round(cf_sum, 2),
            "delta_vs_actual": round(cf_sum - base_sum, 2),
            "fired": fired, "helped": helped, "hurt": hurt,
        }
    return {"actual_total_pnl_pct": round(base_sum, 2), "grid": result}


def _price_at(early: list, target_sec: int):
    """early_ticks icinden target_sec'e en yakin (ve <= max mevcut) fiyat."""
    if not early:
        return None
    usable = [(s, p) for s, p in early if s <= target_sec + 20]  # ufak tolerans
    pool = usable or early
    best = min(pool, key=lambda sp: abs(sp[0] - target_sec))
    return best[1]


def staged_entry(rows: list[dict]) -> dict:
    """early_ticks ile 3-dilim kademeli giris vs tek-sefer giris pnl karsilastirmasi."""
    deltas, n_appl, per = [], 0, []
    for r in rows:
        early = r.get("early_ticks") or []
        exitp = r.get("exit_price")
        entry = r.get("entry_price")
        pnl = r.get("pnl_pct")
        if not early or not exitp or not entry or pnl is None:
            continue
        max_sec = max(s for s, _ in early)
        if max_sec < 60:  # 2dk'dan once kapanan -> kademeli giris anlamsiz
            continue
        prices = [_price_at(early, t) for t in STAGE_TARGETS]
        prices = [p for p in prices if p]
        if len(prices) < 2:
            continue
        blended = sum(prices) / len(prices)
        cf_pnl = (exitp / blended - 1) * 100
        d = cf_pnl - pnl
        deltas.append(d)
        n_appl += 1
        per.append((r.get("pair", "?")[:14], round(pnl, 1), round(cf_pnl, 1), round(d, 1), len(prices)))
    avg = sum(deltas) / len(deltas) if deltas else 0.0
    return {"applicable": n_appl, "avg_delta_pct": round(avg, 2), "per": per}


def leak_by_reason(rows: list[dict]) -> dict:
    """exit_reason bazinda ortalama 'masada kalan' (mfe - pnl)."""
    agg = defaultdict(list)
    for r in rows:
        mfe, pnl = r.get("mfe_pct"), r.get("pnl_pct")
        if mfe is None or pnl is None:
            continue
        agg[(r.get("exit_reason") or "?")[:28]].append(mfe - pnl)
    return {k: {"n": len(v), "avg_left_pct": round(sum(v) / len(v), 1)} for k, v in agg.items()}


def main() -> int:
    rows = load_exits()
    if not rows:
        print("exits.jsonl bos/yok - once veri biriksin.")
        return 1
    print(f"exits ornek: {len(rows)}  (kucuk ornek = yonelim, kesin degil)\n")

    sw = scratch_sweep(rows)
    print("A) SCRATCH ESIGI WHAT-IF  (toplam pnl% , esit ~$15 pozisyon varsayimi)")
    print(f"   gercek toplam pnl: {sw['actual_total_pnl_pct']:+.2f}%")
    print(f"   {'esik':>6} {'cf_toplam':>10} {'delta':>8}  {'tetik':>5} {'fayda':>5} {'zarar':>5}")
    for S in SCRATCH_GRID:
        g = sw["grid"][S]
        print(f"   {S:>6.0f} {g['cf_total_pnl_pct']:>+10.2f} {g['delta_vs_actual']:>+8.2f}  "
              f"{g['fired']:>5} {g['helped']:>5} {g['hurt']:>5}")
    best = max(SCRATCH_GRID, key=lambda S: sw["grid"][S]["delta_vs_actual"])
    print(f"   >>> en iyi delta: {best:.0f}% esik ({sw['grid'][best]['delta_vs_actual']:+.2f}%)")

    st = staged_entry(rows)
    print(f"\nB) KADEMELI GIRIS (3 dilim ~0/60/120s) vs tek-sefer  | uygulanabilir: {st['applicable']}")
    if st["per"]:
        print(f"   {'coin':14} {'tek_pnl':>8} {'kademeli':>9} {'delta':>7} {'dilim':>5}")
        for pair, a, c, d, k in st["per"]:
            print(f"   {pair:14} {a:>+8.1f} {c:>+9.1f} {d:>+7.1f} {k:>5}")
    print(f"   >>> ort delta: {st['avg_delta_pct']:+.2f}%  "
          f"({'kademeli iyi' if st['avg_delta_pct']>0 else 'kademeli kotu/notr'})")

    leak = leak_by_reason(rows)
    print("\nC) KAR SIZINTISI (exit_reason bazinda ort masada kalan = mfe-pnl)")
    for k, v in sorted(leak.items(), key=lambda kv: -kv[1]["avg_left_pct"]):
        print(f"   {k:30} n={v['n']:2}  ort masada +{v['avg_left_pct']:.1f}%")

    summary = {
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "n": len(rows),
        "actual_total_pnl_pct": sw["actual_total_pnl_pct"],
        "scratch_grid": {str(S): sw["grid"][S] for S in SCRATCH_GRID},
        "best_scratch": best,
        "staged_avg_delta_pct": st["avg_delta_pct"],
        "leak_by_reason": leak,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, separators=(",", ":")) + "\n")
    print(f"\nozet -> {OUT.name} (append). Read-only, engine'e dokunulmadi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
