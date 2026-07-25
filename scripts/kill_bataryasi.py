#!/usr/bin/env python3
"""Kill bataryasi koscusu — docs/kill_bataryasi_onkayit.md'nin bire bir
kodu. OGRENILMIS KOMPOZIT YOK; ozellik basina Mann-Whitney AUC +
recall@20. Varsayilan DRY-RUN: hukum yalniz --resmi + tarih penceresi
(2026-08-08..15) + n_tam >= 200 saglaninca yazilir.

Kullanim: python scripts/kill_bataryasi.py [--veri data] [--resmi]
Cikti: stdout + data/kill_bataryasi_sonuc.json
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

AUC_ESIK = 0.65
RECALL_ESIK = 0.30
MIN_TAM_N = 200
RESMI_PENCERE = ("2026-08-08", "2026-08-15")

OZELLIKLER = [
    ("holder.top1_pay", lambda q: (q.get("holder") or {}).get("top1_pay")),
    ("holder.top5_pay", lambda q: (q.get("holder") or {}).get("top5_pay")),
    ("holder.top10_pay", lambda q: (q.get("holder") or {}).get("top10_pay")),
    ("erken.yeni_eski_orani",
     lambda q: (q.get("erken") or {}).get("yeni_eski_orani")),
    ("erken.ort_yas_gun", lambda q: (q.get("erken") or {}).get("ort_yas_gun")),
    ("erken.medyan_yas_gun",
     lambda q: (q.get("erken") or {}).get("medyan_yas_gun")),
    ("yaratici.runner_var_asof",
     lambda q: (q.get("yaratici") or {}).get("runner_var_asof")),
    ("yaratici.lansman_n_asof",
     lambda q: (q.get("yaratici") or {}).get("lansman_n_asof")),
    ("yaratici.dead_orani_asof",
     lambda q: (q.get("yaratici") or {}).get("dead_orani_asof")),
    ("lp.pumpswap",
     lambda q: (None if not q.get("lp")
                else float(q["lp"].get("amm") == "pumpswap"))),
    ("lp.lp_top1_pay", lambda q: (q.get("lp") or {}).get("lp_top1_pay")),
]


def auc_mann_whitney(degerler: list[float], etiketler: list[bool]) -> float:
    """Bagli degerlerde ortalama sira; AUC = (R1 - n1(n1+1)/2) / (n1*n0)."""
    sirali = sorted(range(len(degerler)), key=lambda i: degerler[i])
    siralar = [0.0] * len(degerler)
    i = 0
    while i < len(sirali):
        j = i
        while (j + 1 < len(sirali)
               and degerler[sirali[j + 1]] == degerler[sirali[i]]):
            j += 1
        ort = (i + j) / 2 + 1
        for k in range(i, j + 1):
            siralar[sirali[k]] = ort
        i = j + 1
    n1 = sum(etiketler)
    n0 = len(etiketler) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    r1 = sum(s for s, e in zip(siralar, etiketler) if e)
    return (r1 - n1 * (n1 + 1) / 2) / (n1 * n0)


def recall_at(degerler, etiketler, pay=0.2, ters=False):
    n = len(degerler)
    runner_n = sum(etiketler)
    if runner_n == 0:
        return None
    k = max(1, math.ceil(pay * n))
    sirali = sorted(range(n), key=lambda i: degerler[i], reverse=not ters)
    return sum(1 for i in sirali[:k] if etiketler[i]) / runner_n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--veri", default="data")
    ap.add_argument("--resmi", action="store_true")
    a = ap.parse_args()
    veri = Path(a.veri)

    satirlar = []
    for ln in open(veri / "q_veri_seti.jsonl"):
        try:
            t = json.loads(ln)
        except ValueError:
            continue
        if t.get("yol") and any((t.get("q") or {}).get(k)
                                for k in ("holder", "lp", "erken",
                                          "yaratici")):
            satirlar.append(t)
    tam_n = sum(1 for t in satirlar
                if all((t["q"] or {}).get(k)
                       for k in ("holder", "lp", "erken", "yaratici")))

    sonuc = []
    for ad, cek in OZELLIKLER:
        cift = [(cek(t["q"]), t["yol"]["ath_pct"] >= 100.0)
                for t in satirlar]
        cift = [(d, e) for d, e in cift if d is not None]
        if len(cift) < 10:
            sonuc.append({"ozellik": ad, "n": len(cift), "auc": None,
                          "recall20": None, "not": "n<10"})
            continue
        degerler = [d for d, _ in cift]
        etiketler = [e for _, e in cift]
        auc = auc_mann_whitney(degerler, etiketler)
        r20 = recall_at(degerler, etiketler, ters=(auc < 0.5))
        sonuc.append({"ozellik": ad, "n": len(cift),
                      "runner_n": sum(etiketler),
                      "auc": round(auc, 3),
                      "auc_yon": round(max(auc, 1 - auc), 3),
                      "recall20": round(r20, 3) if r20 is not None else None})

    gecerli = [s for s in sonuc if s.get("auc") is not None]
    en_auc = max((s["auc_yon"] for s in gecerli), default=None)
    en_r20 = max((s["recall20"] for s in gecerli
                  if s["recall20"] is not None), default=None)
    gun = time.strftime("%Y-%m-%d")
    resmi_kosul = (a.resmi and RESMI_PENCERE[0] <= gun <= RESMI_PENCERE[1]
                   and tam_n >= MIN_TAM_N)
    if not resmi_kosul:
        hukum = "DRY-RUN (hukum degil)"
    elif (en_auc or 0) >= AUC_ESIK and (en_r20 or 0) >= RECALL_ESIK:
        hukum = "GECTI: kosullama sinyali var, kosullama modeli acilabilir"
    else:
        hukum = "KALDI: on-kayit geregi edge-siniflandirma programi kapanir"

    cikti = {"uretim_ts": time.time(), "gun": gun,
             "evren_n": len(satirlar), "tam_n": tam_n,
             "min_tam_n": MIN_TAM_N,
             "esikler": {"auc": AUC_ESIK, "recall20": RECALL_ESIK},
             "en_iyi": {"auc_yon": en_auc, "recall20": en_r20},
             "hukum": hukum, "ozellikler": sonuc}
    (veri / "kill_bataryasi_sonuc.json").write_text(
        json.dumps(cikti, indent=1))
    print(f"KILL BATARYASI  {gun}  evren={len(satirlar)} tam={tam_n} "
          f"(min {MIN_TAM_N})\nHUKUM: {hukum}\n")
    for s in sonuc:
        print(f"  {s['ozellik']:<24} n={s['n']:<4} auc={s.get('auc')} "
              f"yon={s.get('auc_yon')} recall20={s.get('recall20')}")


if __name__ == "__main__":
    main()
