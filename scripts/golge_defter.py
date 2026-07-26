#!/usr/bin/env python3
"""Golge-defter: Edge GO on-kaydinin (docs/edge_go_onkayit.md) birincil
KPI olcum araci. Eslestirilmis net PnL farki + siralama-IC.

Her degerlendirme turu (ardisik EdgeShadowEvaluated arasi pencere):
  fark = pnl(golge_aday) - pnl(legacy_hedef) - gecis_maliyeti
  golge CASH ise golge tarafi 0; karar degisimi = -1.50 USD (on-kayit).
Siralama-IC: edgeler vektoru vs motorlarin izleyen-pencere PnL'i
(Spearman, bagli siralar ortalama-sira).

Cikti: stdout ozet + data/golge_defter.json (gunluk seri, birikimli).
On-kayit geregi degerlendirme donemi baslangici parametrelidir;
oncesi yalniz boru-hatti testi (DRY-RUN etiketi).
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
from collections import defaultdict
from pathlib import Path
from statistics import median

GECIS_MALIYETI_USD = 1.50          # on-kayit sabiti; turetimi dokumanda
DONEM_BASLANGIC_TS = 1785058502.0   # on-kayit commit ani (26 Tem)


def _spearman(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3:
        return None

    def siralar(v):
        s = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[s[j + 1]] == v[s[i]]:
                j += 1
            ort = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[s[k]] = ort
            i = j + 1
        return r
    ra, rb = siralar(a), siralar(b)
    ma = sum(ra) / n
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((x - ma) ** 2 for x in rb)
    if va == 0 or vb == 0:
        return None
    kov = sum((x - ma) * (y - ma) for x, y in zip(ra, rb))
    return kov / (va ** 0.5 * vb ** 0.5)


def golge_turlari(veri: Path, saat: float):
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
                    pl = e.get("payload") or {}
                    yield (e["ts_ms"] / 1000, pl.get("golge_aday"),
                           pl.get("legacy_hedef"), pl.get("edgeler") or {})


def motor_islemleri(veri: Path, saat: float) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for yolad in glob.glob(str(veri / "*_trades.jsonl")):
        motor = os.path.basename(yolad).replace("_trades.jsonl", "")
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
            if ts >= time.time() - (saat + 0.5) * 3600:
                out[motor].append((ts, float(t.get("pnl_usd") or 0)))
    for m in out:
        out[m].sort()
    return out


def pencere_pnl(islemler: dict, motor: str | None,
                t0: float, t1: float) -> float:
    if motor is None:                      # CASH: getirisiz nakit
        return 0.0
    seri = islemler.get(motor) or []
    ts_list = [x[0] for x in seri]
    i = bisect.bisect_left(ts_list, t0)
    j = bisect.bisect_left(ts_list, t1)
    return sum(p for _, p in seri[i:j])


def hesapla(turlar: list, islemler: dict) -> dict:
    farklar = []
    icler = []
    onceki_karar = "__ilk__"
    gecis_n = 0
    for k in range(len(turlar) - 1):
        ts, golge, legacy, edgeler = turlar[k]
        t1 = turlar[k + 1][0]
        g = pencere_pnl(islemler, golge, ts, t1)
        l = pencere_pnl(islemler, legacy, ts, t1)
        maliyet = 0.0
        if onceki_karar != "__ilk__" and golge != onceki_karar:
            maliyet = GECIS_MALIYETI_USD
            gecis_n += 1
        onceki_karar = golge
        farklar.append(g - l - maliyet)
        if edgeler:
            motorlar = sorted(edgeler)
            ger = [pencere_pnl(islemler, m, ts, t1) for m in motorlar]
            ic = _spearman([edgeler[m] for m in motorlar], ger)
            if ic is not None:
                icler.append(ic)
    poz = sum(1 for f in farklar if f > 0)
    neg = sum(1 for f in farklar if f < 0)
    return {"tur_n": len(farklar), "gecis_n": gecis_n,
            "toplam_fark_usd": round(sum(farklar), 2),
            "medyan_fark_usd": round(median(farklar), 4) if farklar else None,
            "pozitif_pencere": poz, "negatif_pencere": neg,
            "pozitif_pay_bagsiz": round(poz / (poz + neg), 3)
                                  if poz + neg else None,
            "ic_n": len(icler),
            "ic_medyan": round(median(icler), 3) if icler else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saat", type=float, default=24.0)
    ap.add_argument("--veri", default=os.getenv("MOMENTUM_DATA_DIR", "data"))
    a = ap.parse_args()
    veri = Path(a.veri)
    turlar = sorted(golge_turlari(veri, a.saat))
    islemler = motor_islemleri(veri, a.saat)
    donem_turlari = [t for t in turlar if t[0] >= DONEM_BASLANGIC_TS]
    dry_turlari = [t for t in turlar if t[0] < DONEM_BASLANGIC_TS]
    gun = {"gun": time.strftime("%Y-%m-%d"), "uretim_ts": time.time(),
           "donem": hesapla(donem_turlari, islemler),
           "dry_run_donem_oncesi": hesapla(dry_turlari, islemler)}
    yol = veri / "golge_defter.json"
    try:
        seri = json.loads(yol.read_text())
    except (OSError, ValueError):
        seri = {"gunler": []}
    seri["gunler"] = [g for g in seri["gunler"]
                      if g.get("gun") != gun["gun"]] + [gun]
    seri["on_kayit"] = "docs/edge_go_onkayit.md"
    yol.write_text(json.dumps(seri, indent=1))
    print(json.dumps(gun, indent=1))


if __name__ == "__main__":
    main()
