#!/usr/bin/env python3
"""VETO degeri gunluk olcumu (26 Tem kullanici karari: Edge'in ilk
onceligi CASH/VETO).

Golgenin "salter" dedigi anlarin izleyen 5 dakikasinda filonun
GERCEKLESEN PnL'i ile "aktif" dediklerinin ayni penceresi kiyaslanir.
SINIRLAR (durustluk, her ciktida): pencereler ortusur; filo PnL'i tam
karsi-olgusal degildir (onceden acilmis pozisyonlarin kapanislari
dahildir); anlamlilik testi yoktur. Gunluk seri biriktikce yorum
guclenir. Ayrica golge adaylari politika AILESI bazinda da sayilir
(26 Tem mimari dili: Edge aile secer, motorlar uygulayicidir).

Kullanim: python scripts/veto_degeri.py [--saat 24] [--veri data]
Cikti: stdout + data/veto_degeri.json (gece zinciri kosar)
"""

from __future__ import annotations

import argparse
import bisect
import glob
import io
import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path
from statistics import median

PENCERE_SN = 300.0
AILE = {"r1": "runner", "r2": "runner"}   # digerleri scalp ailesi


def aile(motor: str | None) -> str | None:
    if motor is None:
        return None
    return AILE.get(motor, "scalp")


def golge_olaylari(veri: Path, saat: float):
    esik = (time.time() - saat * 3600) * 1000
    for yolad in sorted(glob.glob(
            str(veri / "gozlem/events/*/*.otonom.jsonl*"))):
        if yolad.endswith(".zst"):
            p = subprocess.run(["zstd", "-dc", yolad],
                               capture_output=True, check=True)
            fh = io.BytesIO(p.stdout)
        else:
            fh = open(yolad, "rb")
        with fh:
            for ln in fh:
                if b"EdgeShadowEvaluated" not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if (e.get("kind") == "EdgeShadowEvaluated"
                        and e.get("ts_ms", 0) >= esik):
                    yield e["ts_ms"] / 1000, (e.get("payload") or {})


def filo_islemleri(veri: Path, saat: float):
    out = []
    for yolad in glob.glob(str(veri / "*_trades.jsonl")):
        for ln in open(yolad):
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            if t.get("type"):
                continue
            ts = float(t.get("ts") or 0)
            if ts >= time.time() - (saat + 0.2) * 3600:
                out.append((ts, float(t.get("pnl_usd") or 0)))
    out.sort()
    return out


def pencere_ozeti(zamanlar, islemler) -> dict:
    ts_list = [x[0] for x in islemler]
    degerler = []
    for t0 in zamanlar:
        i = bisect.bisect_left(ts_list, t0)
        j = bisect.bisect_right(ts_list, t0 + PENCERE_SN)
        degerler.append(sum(p for _, p in islemler[i:j]))
    if not degerler:
        return {"n": 0}
    return {"n": len(degerler), "toplam_usd": round(sum(degerler), 2),
            "ort_usd": round(sum(degerler) / len(degerler), 3),
            "medyan_usd": round(median(degerler), 3),
            "negatif_pay": round(sum(1 for v in degerler if v < 0)
                                 / len(degerler), 3)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saat", type=float, default=24.0)
    ap.add_argument("--veri", default=os.getenv("MOMENTUM_DATA_DIR", "data"))
    a = ap.parse_args()
    veri = Path(a.veri)

    salter_ts, aktif_ts = [], []
    aday_sayimi: Counter = Counter()
    aile_sayimi: Counter = Counter()
    for ts, pl in golge_olaylari(veri, a.saat):
        g = pl.get("golge_aday")
        if g is None:
            salter_ts.append(ts)
        else:
            aktif_ts.append(ts)
            aday_sayimi[g] += 1
            aile_sayimi[aile(g)] += 1
    islemler = filo_islemleri(veri, a.saat)
    rapor = {
        "uretim_ts": time.time(), "pencere_saat": a.saat,
        "pencere_sn": PENCERE_SN,
        "sinir_notu": ("pencereler ortusur; filo pnl tam karsi-olgusal "
                       "degil; anlamlilik testi yok"),
        "salter": pencere_ozeti(salter_ts, islemler),
        "aktif": pencere_ozeti(aktif_ts, islemler),
        "aday_dagilimi": dict(aday_sayimi.most_common()),
        "aile_dagilimi": dict(aile_sayimi.most_common()),
    }
    s, k = rapor["salter"], rapor["aktif"]
    if s.get("n") and k.get("n"):
        rapor["ayrisma_usd_pencere"] = round(
            k["ort_usd"] - s["ort_usd"], 3)
    (veri / "veto_degeri.json").write_text(json.dumps(rapor, indent=1))
    print(json.dumps(rapor, indent=1))


if __name__ == "__main__":
    main()
