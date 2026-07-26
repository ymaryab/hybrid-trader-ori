#!/usr/bin/env python3
"""Cok-kapili Alpha Factory koscusu (26 Tem protokol muhru).

6 dogrulayici kapi TEK gecise yakin kosulur; BH (q=0.10) duzeltmesi;
hukum verilen kapi kilitlenir (alpha_kapilari.json), INCONCLUSIVE'ler
sonraki kosuda yeniden denenir. Esikler docs muhrundedir; burada
YALNIZ uygulanir.

Kullanim: python scripts/alpha_kapilari.py
"""

from __future__ import annotations

import glob
import io
import json
import math
import os
import subprocess
import sys
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path
from statistics import median, pstdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hibrit_trader.edge.yol_arsivi import GozlemYolArsivi  # noqa: E402

VERI = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
Q_FDR = 0.10


def _norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def _mh_rr(tabakalar):
    """Mantel-Haenszel RR + log-CI. tabakalar: [(a,b,c,d), ...]."""
    pay = payda = 0.0
    var_pay = 0.0
    for a, b, c, d in tabakalar:
        n1, n0 = a + b, c + d
        t = n1 + n0
        if t == 0 or n1 == 0 or n0 == 0:
            continue
        pay += a * n0 / t
        payda += c * n1 / t
        var_pay += (n1 * n0 * (a + c) - a * c * t) / (t * t)
    if pay <= 0 or payda <= 0 or var_pay <= 0:
        return None
    rr = pay / payda
    se = math.sqrt(var_pay) / math.sqrt(pay * payda)
    return {"rr": rr, "lo": rr * math.exp(-1.96 * se),
            "hi": rr * math.exp(1.96 * se),
            "p": _norm_sf(math.log(rr) / se) if rr > 1 else 1.0}


def _tabaka(ts: float) -> int:
    return int(ts // 21600) % 4


def yollar_yukle():
    return {y.token: y.ticks for y in GozlemYolArsivi(VERI).yollar()}


def filo_pnl_serisi():
    islemler = []
    for yolad in glob.glob(str(VERI / "*_trades.jsonl")):
        for ln in open(yolad):
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            if t.get("type"):
                continue
            islemler.append((float(t.get("ts") or 0),
                             float(t.get("pnl_usd") or 0)))
    islemler.sort()
    return islemler


def pencere_pnl(islemler, ts_list, t0, t1):
    i = bisect_left(ts_list, t0)
    j = bisect_right(ts_list, t1)
    return sum(p for _, p in islemler[i:j])


# ---- G1 DIP-CLAIM ---------------------------------------------------------
def g1_dip(yollar):
    getiriler = []
    for tok, ticks in yollar.items():
        t0 = ticks[0][0]
        f0 = ticks[0][1]
        tepe = f0
        tepe_i = 0
        for i, (ts, f) in enumerate(ticks):
            if ts - t0 > 1800:
                break
            if f > tepe:
                tepe, tepe_i = f, i
        if f0 <= 0 or 100 * (tepe / f0 - 1) < 15:
            continue
        giris = None
        for i in range(tepe_i + 1, len(ticks)):
            ts, f = ticks[i]
            if ts - t0 > 1800 and giris is None:
                break
            d = 100 * (f / tepe - 1)
            if -8 <= d <= -3:
                giris = (i, ts, f)
                break
            if d < -8:
                break
        if giris is None:
            continue
        gi, gts, gf = giris
        sonuc = None
        for ts, f in ticks[gi + 1:]:
            r = 100 * (f / gf - 1)
            if r >= 6:
                sonuc = 6.0
                break
            if r <= -6:
                sonuc = r
                break
            if ts - gts >= 1800:
                sonuc = r
                break
        if sonuc is None:
            sonuc = 100 * (ticks[-1][1] / gf - 1)
        getiriler.append(max(min(sonuc, 6.0), -30.0))
    n = len(getiriler)
    if n < 40:
        return {"kapi": "G1_dip", "n": n, "hukum": "INCONCLUSIVE",
                "neden": "min_n"}
    ort = sum(getiriler) / n
    se = (pstdev(getiriler) / math.sqrt(n)) or 1e-9
    isabet = sum(1 for g in getiriler if g > 0) / n
    p = _norm_sf(ort / se)
    return {"kapi": "G1_dip", "n": n, "ort": round(ort, 2),
            "medyan": round(median(getiriler), 2),
            "isabet": round(isabet, 3),
            "ci": [round(ort - 1.96 * se, 2), round(ort + 1.96 * se, 2)],
            "p": p,
            "fail_kosulu": ort + 1.96 * se < 0,
            "etki_ok": ort > 0 and isabet >= 0.50}


# ---- G2 ABSORB-v2 ---------------------------------------------------------
def g2_absorb(yollar):
    akis = defaultdict(dict)
    yol_akis = VERI / "gozlem" / "k3_akis.jsonl"
    if not yol_akis.exists():
        return {"kapi": "G2_absorb2", "hukum": "INCONCLUSIVE",
                "neden": "k3_akis yok"}
    for ln in open(yol_akis):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        akis[r["mint"]][int(r["ts_dk"])] = r
    tabakalar = defaultdict(lambda: [0, 0, 0, 0])
    for tok, dkler in akis.items():
        ticks = yollar.get(tok)
        if not ticks:
            continue
        ts_list = [t for t, _ in ticks]
        for dk0 in sorted(dkler):
            sol_al = sol_sat = 0
            for k in range(15):
                r = dkler.get(dk0 + 60 * k)
                if r:
                    sol_al += r["sol_al"]
                    sol_sat += r["sol_sat"]
            top = sol_al + sol_sat
            if top < 5 * 10**8:          # <0.5 SOL: gurultu
                continue
            pay = sol_al / top
            t_bas, t_son = dk0, dk0 + 900
            i1 = bisect_left(ts_list, t_bas)
            i2 = bisect_left(ts_list, t_son)
            if i2 >= len(ticks) or i1 >= len(ticks):
                continue
            f1, f2 = ticks[i1][1], ticks[i2][1]
            if f1 <= 0 or abs(100 * (f2 / f1 - 1)) > 2:
                continue
            j = bisect_left(ts_list, t_son + 3600)
            ileri = ticks[i2:j] or [ticks[i2]]
            guclu = 100 * (max(f for _, f in ileri) / f2 - 1) >= 20
            tb = _tabaka(t_bas)
            if pay >= 0.65:
                tabakalar[tb][0 if guclu else 1] += 1
            elif 0.35 <= pay <= 0.65:
                tabakalar[tb][2 if guclu else 3] += 1
    maruz_n = sum(v[0] + v[1] for v in tabakalar.values())
    mh = _mh_rr(list(tabakalar.values()))
    if maruz_n < 30 or mh is None:
        return {"kapi": "G2_absorb2", "maruz_n": maruz_n,
                "hukum": "INCONCLUSIVE", "neden": "min_n/hucre"}
    return {"kapi": "G2_absorb2", "maruz_n": maruz_n,
            "rr": round(mh["rr"], 2),
            "ci": [round(mh["lo"], 2), round(mh["hi"], 2)],
            "p": mh["p"], "fail_kosulu": mh["hi"] < 1.2,
            "etki_ok": mh["rr"] >= 1.5}


# ---- G3 SNIPER-YOGUNLUK ---------------------------------------------------
def g3_sniper():
    seri = defaultdict(dict)
    for yolad in sorted(glob.glob(
            str(VERI / "gozlem/events/*/*.islem.jsonl*"))):
        if yolad.endswith(".zst"):
            p = subprocess.run(["zstd", "-dc", yolad],
                               capture_output=True, check=True)
            fh = io.BytesIO(p.stdout)
        else:
            fh = open(yolad, "rb")
        with fh:
            for ln in fh:
                if b"TradeAggregate" not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("kind") != "TradeAggregate":
                    continue
                pl = e["payload"]
                seri[e.get("token")][pl["ts_dk"]] = pl
    kayitlar = []
    for tok, dkler in seri.items():
        if len(dkler) < 3:
            continue
        sirali = sorted(dkler)
        d0 = sirali[0]
        ilk2_al = sum(dkler[d].get("n_al", 0) for d in sirali
                      if d < d0 + 120)
        ilk_f = dkler[d0].get("o") or dkler[d0].get("c")
        son_dk = [d for d in sirali if d <= d0 + 3600][-1]
        son_f = dkler[son_dk].get("c")
        if not ilk_f or not son_f:
            continue
        kayitlar.append((d0, ilk2_al, son_f / ilk_f <= 0.5))
    if len(kayitlar) < 120:
        return {"kapi": "G3_sniper", "n": len(kayitlar),
                "hukum": "INCONCLUSIVE", "neden": "min_n"}
    yog = sorted(k[1] for k in kayitlar)
    p90 = yog[int(0.9 * len(yog))]
    p25, p75 = yog[len(yog) // 4], yog[3 * len(yog) // 4]
    tabakalar = defaultdict(lambda: [0, 0, 0, 0])
    maruz_n = 0
    for d0, il, cok in kayitlar:
        tb = _tabaka(d0)
        if il >= p90:
            tabakalar[tb][0 if cok else 1] += 1
            maruz_n += 1
        elif p25 <= il <= p75:
            tabakalar[tb][2 if cok else 3] += 1
    mh = _mh_rr(list(tabakalar.values()))
    if maruz_n < 30 or mh is None:
        return {"kapi": "G3_sniper", "maruz_n": maruz_n,
                "hukum": "INCONCLUSIVE", "neden": "min_n/hucre"}
    return {"kapi": "G3_sniper", "maruz_n": maruz_n,
            "rr": round(mh["rr"], 2),
            "ci": [round(mh["lo"], 2), round(mh["hi"], 2)],
            "p": mh["p"], "fail_kosulu": mh["hi"] < 1.2,
            "etki_ok": mh["rr"] >= 1.5}


# ---- G4-6 rejim gostergeleri ---------------------------------------------
def _gosterge_kapi(ad, pencere_deger, islemler, ts_list,
                   alt_mi, orta):
    """pencere_deger: [(t0, deger)]; alt_mi True -> maruz=alt dilim."""
    if len(pencere_deger) < 120:
        return {"kapi": ad, "n": len(pencere_deger),
                "hukum": "INCONCLUSIVE", "neden": "min_n"}
    degerler = sorted(v for _, v in pencere_deger)
    n = len(degerler)
    if alt_mi:
        esik = degerler[n // 4]
        maruz = [(t, v) for t, v in pencere_deger if v <= esik]
    else:
        esik = degerler[int(0.9 * n)]
        maruz = [(t, v) for t, v in pencere_deger if v >= esik]
    o1, o2 = degerler[int(orta[0] * n)], degerler[int(orta[1] * n) - 1]
    kontrol = [(t, v) for t, v in pencere_deger if o1 <= v <= o2]
    if len(maruz) < 40 or len(kontrol) < 40:
        return {"kapi": ad, "hukum": "INCONCLUSIVE", "neden": "min_grup"}
    mp = [pencere_pnl(islemler, ts_list, t, t + 1800) for t, _ in maruz]
    kp = [pencere_pnl(islemler, ts_list, t, t + 1800) for t, _ in kontrol]
    fark = sum(mp) / len(mp) - sum(kp) / len(kp)
    se = math.sqrt((pstdev(mp) ** 2) / len(mp)
                   + (pstdev(kp) ** 2) / len(kp)) or 1e-9
    p = _norm_sf(-fark / se)          # yon: maruzda daha KOTU (fark<0)
    return {"kapi": ad, "maruz_n": len(mp), "kontrol_n": len(kp),
            "maruz_ort": round(sum(mp) / len(mp), 2),
            "kontrol_ort": round(sum(kp) / len(kp), 2),
            "fark": round(fark, 2), "p": p,
            "fail_kosulu": fark - 1.96 * se > 0,
            "etki_ok": fark < 0}


def g456(yollar, islemler, ts_list):
    # pencere baslangiclari: son 5 gunun 30dk gridi
    simdi = time.time()
    bas = simdi - 5 * 86400
    grid = [bas + k * 1800 for k in range(int((simdi - bas) // 1800) - 1)]
    # G4 dispersiyon
    disp = []
    tick_map = {t: ([x for x, _ in v], v) for t, v in yollar.items()}
    for t0 in grid:
        rets = []
        for tok, (ts_l, v) in tick_map.items():
            i = bisect_left(ts_l, t0)
            j = bisect_left(ts_l, t0 + 1800)
            if i < len(v) and j < len(v) and v[i][1] > 0:
                rets.append(100 * (v[j][1] / v[i][1] - 1))
        if len(rets) >= 8:
            disp.append((t0, pstdev(rets)))
    # G5 lansman-z (CensusPulse)
    pulslar = []
    for yolad in sorted(glob.glob(
            str(VERI / "gozlem/events/*/*.sayim.jsonl*"))):
        if yolad.endswith(".zst"):
            pz = subprocess.run(["zstd", "-dc", yolad],
                                capture_output=True, check=True)
            fh = io.BytesIO(pz.stdout)
        else:
            fh = open(yolad, "rb")
        with fh:
            for ln in fh:
                if b"CensusPulse" not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("kind") == "CensusPulse":
                    pulslar.append((e["ts_ms"] / 1000,
                                    (e["payload"] or {}).get(
                                        "lansman_1h") or 0))
    pulslar.sort()
    pl_ts = [t for t, _ in pulslar]
    lz = []
    if len(pulslar) > 100:
        vals = [v for _, v in pulslar]
        mu = sum(vals) / len(vals)
        sd = pstdev(vals) or 1e-9
        for t0 in grid:
            i = bisect_left(pl_ts, t0)
            if 0 <= i < len(pulslar) and abs(pulslar[i][0] - t0) < 900:
                lz.append((t0, (pulslar[i][1] - mu) / sd))
    # G6 fee (MarketContext prioritization_fees medyani)
    fees = []
    for yolad in sorted(glob.glob(
            str(VERI / "gozlem/events/*/*.anlik.jsonl*"))):
        if yolad.endswith(".zst"):
            pz = subprocess.run(["zstd", "-dc", yolad],
                                capture_output=True, check=True)
            fh = io.BytesIO(pz.stdout)
        else:
            fh = open(yolad, "rb")
        with fh:
            for ln in fh:
                if b"MarketContext" not in ln or b"prioritization" not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                f = (e.get("payload") or {}).get("prioritization_fees")
                if isinstance(f, list) and f:
                    med = median([x.get("prioritizationFee", 0)
                                  for x in f if isinstance(x, dict)] or [0])
                    fees.append((e["ts_ms"] / 1000, med))
    fees.sort()
    fe_ts = [t for t, _ in fees]
    fz = []
    for t0 in grid:
        i = bisect_left(fe_ts, t0)
        if 0 <= i < len(fees) and abs(fees[i][0] - t0) < 900:
            fz.append((t0, fees[i][1]))
    return [
        _gosterge_kapi("G4_dispersiyon", disp, islemler, ts_list,
                       alt_mi=True, orta=(0.375, 0.625)),
        _gosterge_kapi("G5_lansman_z",
                       [(t, v) for t, v in lz], islemler, ts_list,
                       alt_mi=True, orta=(0.375, 0.625)),
        _gosterge_kapi("G6_fee", fz, islemler, ts_list,
                       alt_mi=False, orta=(0.25, 0.75)),
    ]


def main() -> None:
    kilit_yol = VERI / "alpha_kapilari.json"
    try:
        kilit = json.loads(kilit_yol.read_text())
    except (OSError, ValueError):
        kilit = {"hukumler": {}}
    yollar = yollar_yukle()
    islemler = filo_pnl_serisi()
    ts_list = [t for t, _ in islemler]
    sonuclar = [g1_dip(yollar), g2_absorb(yollar), g3_sniper(),
                *g456(yollar, islemler, ts_list)]
    # onceden hukum verilenler kilitli
    acik = [s for s in sonuclar
            if kilit["hukumler"].get(s["kapi"]) in (None, "INCONCLUSIVE")]
    pli = sorted([s for s in acik if "p" in s], key=lambda s: s["p"])
    m = len(pli)
    for i, s in enumerate(pli, 1):
        s["bh_esik"] = round(Q_FDR * i / m, 4) if m else None
        s["bh_anlamli"] = s["p"] <= Q_FDR * i / m if m else False
    for s in sonuclar:
        if kilit["hukumler"].get(s["kapi"]) not in (None, "INCONCLUSIVE"):
            s["hukum"] = kilit["hukumler"][s["kapi"]] + " (kilitli)"
            continue
        if s.get("hukum") == "INCONCLUSIVE":
            continue
        if s.get("fail_kosulu"):
            s["hukum"] = "FAIL"
        elif s.get("bh_anlamli") and s.get("etki_ok"):
            s["hukum"] = "PASS"
        else:
            s["hukum"] = "INCONCLUSIVE"
        kilit["hukumler"][s["kapi"]] = s["hukum"]
    kilit["son_kosu"] = time.time()
    kilit_yol.write_text(json.dumps(kilit, indent=1))
    (VERI / "alpha_kapilari_son.json").write_text(
        json.dumps({"ts": time.time(), "sonuclar": sonuclar}, indent=1))
    for s in sonuclar:
        print(json.dumps(s))


if __name__ == "__main__":
    main()
