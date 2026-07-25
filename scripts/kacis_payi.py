#!/usr/bin/env python3
"""Kacis payi: TP cikislarinin rug-kacisi kirilganligi (25 Tem).

Soru: TP kazanclarinin ne kadari cokusten saniyelerle kacis? Ve cikis
D saniye GECIKSEYDI (canli kayma/latency gercegi) kazanc neye donerdi?

Yontem (yogun arsiv, ~15 sn cozunurluk):
- Her tp_* cikisi icin cikis sonrasi PENCERE_DK icindeki asgari fiyat:
  dusus_pct = min/cikis_fiyati - 1. dusus <= RUG_ESIK -> "rug_kacisi".
- Gecikme senaryosu: cikis fiyati yerine cikis_ts+D'den sonraki ILK
  tick fiyatiyla pnl yeniden hesaplanir; fark dagilimi raporlanir.
SINIR: 15 sn tick araligi D=15 senaryosunu alt-sinirdan olcer; kayma
modellenmez (gecikme vekildir). Olcum betimseldir, kural onerisi degil.

Kullanim: python scripts/kacis_payi.py [--motor yz] [--gun 3]
Cikti: stdout + data/kacis_payi.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hibrit_trader.edge.yol_arsivi import GozlemYolArsivi  # noqa: E402

PENCERE_DK = 10.0
RUG_ESIK = -50.0
GECIKMELER = (15.0, 30.0, 60.0, 120.0)


def islem_analiz(t: dict, yol) -> dict | None:
    cikis_ts = float(t["ts"])
    cikis_f = float(t.get("exit_price") or 0)
    giris_f = float(t.get("entry_price") or 0)
    if cikis_f <= 0 or giris_f <= 0 or yol is None:
        return None
    sonra = [(ts, p) for ts, p in yol.ticks
             if cikis_ts < ts <= cikis_ts + PENCERE_DK * 60]
    if len(sonra) < 2:
        return None
    dusus = 100 * (min(p for _, p in sonra) / cikis_f - 1)
    gercek_pnl = float(t.get("pnl_pct") or 0)
    gecikmeli = {}
    for d in GECIKMELER:
        ilk = next((p for ts, p in sonra if ts >= cikis_ts + d), None)
        if ilk is not None:
            gecikmeli[str(int(d))] = round(
                100 * (ilk / giris_f - 1) - gercek_pnl, 3)
    return {"token": (t.get("token_address") or "")[:8],
            "pnl": gercek_pnl, "dusus_pct": round(dusus, 2),
            "rug_kacisi": dusus <= RUG_ESIK, "gecikme_delta": gecikmeli}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--motor", default="yz")
    ap.add_argument("--gun", type=float, default=3.0)
    ap.add_argument("--veri", default=os.getenv("MOMENTUM_DATA_DIR", "data"))
    a = ap.parse_args()
    veri = Path(a.veri)
    esik = time.time() - a.gun * 86400

    gunler = sorted({time.strftime("%Y%m%d", time.gmtime(
        time.time() - g * 86400)) for g in range(int(a.gun) + 2)})
    arsiv = {y.token: y for y in
             GozlemYolArsivi(veri, gun_onek=gunler).yollar()}

    tpler = []
    for ln in open(veri / f"{a.motor}_trades.jsonl"):
        if not ln.strip():
            continue
        try:
            t = json.loads(ln)
        except ValueError:
            continue
        if (t.get("type") or float(t.get("ts") or 0) < esik
                or not str(t.get("exit_reason") or "").startswith("tp")):
            continue
        tpler.append(t)

    analizler = [a2 for a2 in (islem_analiz(t, arsiv.get(
        t.get("token_address"))) for t in tpler) if a2 is not None]
    rapor = {"uretim_ts": time.time(), "motor": a.motor,
             "pencere_gun": a.gun, "pencere_dk": PENCERE_DK,
             "rug_esik_pct": RUG_ESIK, "tp_n": len(tpler),
             "analiz_n": len(analizler)}
    if analizler:
        ruglar = [x for x in analizler if x["rug_kacisi"]]
        rapor.update({
            "rug_kacisi_n": len(ruglar),
            "rug_kacisi_orani": round(len(ruglar) / len(analizler), 3),
            "medyan_cikis_sonrasi_dusus": round(median(
                x["dusus_pct"] for x in analizler), 2),
            "gecikme_senaryolari": {}})
        for d in GECIKMELER:
            anahtar = str(int(d))
            deltalar = [x["gecikme_delta"][anahtar] for x in analizler
                        if anahtar in x["gecikme_delta"]]
            if deltalar:
                rapor["gecikme_senaryolari"][anahtar + "sn"] = {
                    "n": len(deltalar),
                    "medyan_delta": round(median(deltalar), 2),
                    "p10_delta": round(sorted(deltalar)[
                        max(0, len(deltalar) // 10 - 1)], 2),
                    "toplam_delta": round(sum(deltalar), 1),
                    "negatife_donen": sum(
                        1 for x in analizler
                        if anahtar in x["gecikme_delta"]
                        and x["pnl"] + x["gecikme_delta"][anahtar] < 0)}
        rapor["en_kotu_5"] = sorted(analizler,
                                    key=lambda x: x["dusus_pct"])[:5]
    (veri / "kacis_payi.json").write_text(json.dumps(rapor, indent=1))
    print(json.dumps({k: v for k, v in rapor.items() if k != "en_kotu_5"},
                     indent=1))
    for x in rapor.get("en_kotu_5", []):
        print("  en_kotu:", x["token"], "pnl", x["pnl"],
              "cikis_sonrasi_dusus", x["dusus_pct"])


if __name__ == "__main__":
    main()
