#!/usr/bin/env python3
"""Yeniden-giris calkalama olcumu (26 Tem kullanici karari: cooldown
EKLEME, once sikligi ve etkiyi OLC).

Ayni tokene ayni motordan pencere icinde >=3 giris = calkalama adayi.
Cikti motor bazinda: kac token, kac islem, net PnL, en pahali ornekler.
Betimseldir; kural onerisi degildir.

Kullanim: python scripts/calkalama_olcum.py [--saat 24] [--min-giris 3]
Cikti: stdout + data/calkalama.json (gece zinciri kosar)
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time
from collections import defaultdict
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saat", type=float, default=24.0)
    ap.add_argument("--min-giris", type=int, default=3)
    ap.add_argument("--veri", default=os.getenv("MOMENTUM_DATA_DIR", "data"))
    a = ap.parse_args()
    veri = Path(a.veri)
    esik = time.time() - a.saat * 3600

    rapor = {"uretim_ts": time.time(), "pencere_saat": a.saat,
             "min_giris": a.min_giris, "motorlar": {}}
    for yolad in sorted(glob.glob(str(veri / "*_trades.jsonl"))):
        motor = os.path.basename(yolad).replace("_trades.jsonl", "")
        gruplar: dict[str, list] = defaultdict(list)
        for ln in open(yolad):
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            if t.get("type") or float(t.get("ts") or 0) < esik:
                continue
            tok = t.get("token_address")
            if tok:
                gruplar[tok].append(t)
        calk = {tok: ts for tok, ts in gruplar.items()
                if len(ts) >= a.min_giris}
        if not calk:
            continue
        detay = []
        for tok, ts in calk.items():
            net = round(sum(float(x.get("pnl_usd") or 0) for x in ts), 2)
            detay.append({"pair": ts[0].get("pair"), "giris_n": len(ts),
                          "net_usd": net,
                          "etiketler": sorted(
                              {x.get("exit_reason") for x in ts})})
        detay.sort(key=lambda d: d["net_usd"])
        toplam_islem = sum(len(v) for v in gruplar.values())
        rapor["motorlar"][motor] = {
            "islem_n": toplam_islem,
            "calkalanan_token_n": len(calk),
            "calkalama_islem_n": sum(d["giris_n"] for d in detay),
            "calkalama_islem_payi": round(
                sum(d["giris_n"] for d in detay) / toplam_islem, 3),
            "calkalama_net_usd": round(
                sum(d["net_usd"] for d in detay), 2),
            "en_pahali_5": detay[:5],
            "en_karli_3": detay[-3:][::-1],
        }
    (veri / "calkalama.json").write_text(json.dumps(rapor, indent=1))
    for m, r in rapor["motorlar"].items():
        print(f"{m:>8}: {r['calkalanan_token_n']} token / "
              f"{r['calkalama_islem_n']} islem "
              f"(pay {r['calkalama_islem_payi']:.0%}) "
              f"net {r['calkalama_net_usd']:+.2f}$")


if __name__ == "__main__":
    main()
