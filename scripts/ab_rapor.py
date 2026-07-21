#!/usr/bin/env python3
"""YZ (kontrol) vs YZN1 (deney) gunluk A/B raporu — 22 Tem deney protokolu.

Metrikler: islem, net, expectancy, PF, win, ort kazanan/kaybeden, MaxDD,
sonda teyit %, sonda kes %, olcek-basari % (teyitli islemlerin kazanma orani),
ve orneklem yeterse Welch t-testi (E farki icin).
Kullanim: ab_rapor.py [bas_ts]  (rapor stdout + data/ab_rapor_gunluk.txt)
"""
import json, math, os, sys, time
from pathlib import Path

DATA = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
BAS = float(sys.argv[1]) if len(sys.argv) > 1 else float(
    (DATA / "ab_deney_bas_ts").read_text().strip()) if (DATA / "ab_deney_bas_ts").exists() else 0.0


def yukle(m):
    rows = []
    p = DATA / f"{m}_trades.jsonl"
    if not p.exists():
        return rows
    for ln in p.read_text().splitlines():
        if not ln.strip():
            continue
        t = json.loads(ln)
        if t.get("type") or float(t.get("ts") or 0) < BAS:
            continue
        rows.append(t)
    return rows


def metrik(rows):
    n = len(rows)
    if not n:
        return None
    pn = [float(t.get("pnl_usd") or 0) for t in rows]
    kaz = [x for x in pn if x > 0]; kay = [x for x in pn if x <= 0]
    kum = tepe = dd = 0.0
    for x in pn:
        kum += x; tepe = max(tepe, kum); dd = min(dd, kum - tepe)
    durum = [t.get("sonda_durum") for t in rows]
    teyitli = [t for t in rows if t.get("sonda_durum") == "teyitli"]
    tk = sum(1 for t in teyitli if float(t.get("pnl_usd") or 0) > 0)
    sd = math.sqrt(sum((x - sum(pn)/n) ** 2 for x in pn) / (n - 1)) if n > 1 else 0
    return dict(n=n, net=sum(pn), E=sum(pn)/n, sd=sd,
                pf=(sum(kaz)/abs(sum(kay))) if kay and sum(kay) < 0 else float("inf"),
                win=100*len(kaz)/n,
                ortk=sum(kaz)/len(kaz) if kaz else 0,
                ortz=sum(kay)/len(kay) if kay else 0, dd=dd,
                teyit=100*durum.count("teyitli")/n,
                kes=100*durum.count("kesildi")/n,
                olcek_basari=100*tk/len(teyitli) if teyitli else None)


cikti = ["=== A/B RAPORU %s (bas_ts=%.0f) ===" % (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), BAS)]
M = {}
for m in ("yz", "yzn1"):
    M[m] = metrik(yukle(m))
    if M[m] is None:
        cikti.append("%s: islem yok" % m); continue
    x = M[m]
    cikti.append("%-5s n=%-3d net %+8.1f  E %+6.3f  PF %5.2f  win %%%4.1f  "
                 "ortK %+6.2f  ortZ %+6.2f  MaxDD %+7.1f  teyit %%%2.0f  kes %%%2.0f  olcekBasari %s"
                 % (m, x["n"], x["net"], x["E"], x["pf"], x["win"], x["ortk"],
                    x["ortz"], x["dd"], x["teyit"], x["kes"],
                    ("%%%d" % x["olcek_basari"]) if x["olcek_basari"] is not None else "-"))
a, b = M.get("yz"), M.get("yzn1")
if a and b and a["n"] >= 20 and b["n"] >= 20:
    se = math.sqrt(a["sd"]**2/a["n"] + b["sd"]**2/b["n"])
    t = (b["E"] - a["E"]) / se if se > 0 else 0
    cikti.append("E farki (deney-kontrol): %+.3f$/islem | Welch t=%.2f (|t|>1.65 ~ %%90 anlamli)"
                 % (b["E"] - a["E"], t))
else:
    cikti.append("anlamlilik: orneklem henuz yetersiz (her iki kolda >=20 gerekli)")
rapor = "\n".join(cikti)
print(rapor)
with open(DATA / "ab_rapor_gunluk.txt", "a") as f:
    f.write(rapor + "\n\n")
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from hibrit_trader.killswitch import notify
    notify("[A/B] " + cikti[1][:180] + ("\n[A/B] " + cikti[2][:180] if len(cikti) > 2 else ""))
except Exception:
    pass
