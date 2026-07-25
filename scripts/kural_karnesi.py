#!/usr/bin/env python3
"""Kural karnesi (25 Tem): cikis etiketi bazinda VAAD vs GERCEK.

Her cikis kurali bir vaatle kondu; karne o vaadi olculebilir bicimde
sinar. Vaad turleri:
  seviye        : "X seviyesinde cik" -> kayma = medyan(pnl) - hedef
                  (esik-delme gercegi: stoplar hedeften derin dolar)
  pozitif_cikis : "artiyi gorunce sat" -> pnl>0 payi
  kucuk_zarar   : "zayifi kucuk zararla kes" -> pnl > -3 payi + p90 zarar
  sifir_civari  : "basabas cik" -> |pnl-hedef| <= 1.5 payi
  mfe_yakalama  : "tepenin cogunu getir" -> medyan(pnl/mfe), mfe>0'da

SINIR (durustluk): hedef seviyeler motor bazinda farklilasabilir;
karne genel hedef tablosuyla calisir ve bunu ciktida beyan eder.
Karsi-olgusal ("kesilmeseydi ne olurdu") bu araca girmez: o soru yol
arsivi + simulatorun isidir (edge_rapor), buraya vekil sokulmaz.

Kullanim: python scripts/kural_karnesi.py [--gun 7] [--veri data]
Cikti: stdout tablo + data/kural_karnesi.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from statistics import median

VAATLER = {
    "timeout_karla": {"tur": "pozitif_cikis",
                      "vaad": "cooldown yolunda artiyi gorunce sat"},
    "erken_zayif": {"tur": "kucuk_zarar",
                    "vaad": "zayif kagidi buyumeden kes"},
    "erken_breakeven": {"tur": "sifir_civari", "hedef": 0.0,
                        "vaad": "guc kaybedeni basabasta birak"},
    "sonda_kes": {"tur": "kucuk_zarar",
                  "vaad": "donmeyen sondayi erken kapat"},
    "otonom_tasfiye": {"tur": "kucuk_zarar",
                       "vaad": "gecis tasfiyesi kontrollu kapansin"},
    "stop_6": {"tur": "seviye", "hedef": -6.0, "vaad": "-6'da kes"},
    "stop_gec": {"tur": "seviye", "hedef": -5.0, "vaad": "gec stop -5"},
    "stop_felaket": {"tur": "seviye", "hedef": -15.0,
                     "vaad": "felaket freni -15"},
    "breakeven_stop": {"tur": "sifir_civari", "hedef": 1.0,
                       "vaad": "mfe gormus kagit +1 tabaninda"},
    "tp_2": {"tur": "seviye", "hedef": 2.0, "vaad": "+2 hedef dolumu"},
    "tp_5": {"tur": "seviye", "hedef": 5.0, "vaad": "+5 hedef dolumu"},
    "tp_kilit_25": {"tur": "seviye", "hedef": 25.0,
                    "vaad": "+25 kilit dolumu"},
    "tp_kilit_40": {"tur": "seviye", "hedef": 40.0,
                    "vaad": "+40 kilit dolumu"},
    "runner_trail": {"tur": "mfe_yakalama",
                     "vaad": "kosucunun tepesinin cogunu getir"},
}


def _p(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def hucre_ozeti(satirlar: list[dict], tanim: dict) -> dict:
    pnl = [float(t.get("pnl_pct") or 0) for t in satirlar]
    mfe = [float(t.get("mfe_pct") or 0) for t in satirlar]
    hold = [float(t.get("hold_sec") or 0) for t in satirlar]
    usd = [float(t.get("pnl_usd") or 0) for t in satirlar]
    n = len(pnl)
    oz = {"n": n, "vaad": tanim.get("vaad"), "tur": tanim["tur"],
          "toplam_usd": round(sum(usd), 2),
          "medyan_pnl_pct": round(median(pnl), 2),
          "p10_pnl_pct": round(_p(pnl, 0.10), 2),
          "p90_pnl_pct": round(_p(pnl, 0.90), 2),
          "win_orani": round(sum(1 for p in pnl if p > 0) / n, 3),
          "medyan_hold_sn": round(median(hold), 1),
          "medyan_mfe": round(median(mfe), 2),
          "guven": round(n / (n + 10.0), 2)}
    tur = tanim["tur"]
    if tur == "seviye":
        hedef = tanim["hedef"]
        oz["hedef"] = hedef
        oz["kayma_puan"] = round(median(pnl) - hedef, 2)
        oz["uyum"] = round(sum(1 for p in pnl
                               if abs(p - hedef) <= 2.0) / n, 3)
    elif tur == "pozitif_cikis":
        oz["uyum"] = oz["win_orani"]
    elif tur == "kucuk_zarar":
        oz["uyum"] = round(sum(1 for p in pnl if p > -3.0) / n, 3)
        oz["p90_zarar"] = round(_p([p for p in pnl if p <= 0] or [0.0],
                                   0.10), 2)
    elif tur == "sifir_civari":
        hedef = tanim.get("hedef", 0.0)
        oz["hedef"] = hedef
        oz["uyum"] = round(sum(1 for p in pnl
                               if abs(p - hedef) <= 1.5) / n, 3)
    elif tur == "mfe_yakalama":
        oranlar = [p / m for p, m in zip(pnl, mfe) if m > 1.0]
        oz["mfe_yakalama_medyan"] = (round(median(oranlar), 3)
                                     if oranlar else None)
        oz["uyum"] = oz["mfe_yakalama_medyan"]
    return oz


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gun", type=float, default=7.0)
    ap.add_argument("--veri", default=os.getenv("MOMENTUM_DATA_DIR", "data"))
    a = ap.parse_args()
    veri = Path(a.veri)
    esik_ts = time.time() - a.gun * 86400

    hucreler: dict[tuple[str, str], list[dict]] = defaultdict(list)
    bilinmeyen: dict[str, int] = defaultdict(int)
    for yolad in sorted(glob.glob(str(veri / "*_trades.jsonl"))):
        motor = os.path.basename(yolad).replace("_trades.jsonl", "")
        for ln in open(yolad):
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            if t.get("type") or float(t.get("ts") or 0) < esik_ts:
                continue                     # kural_degisim satiri / eski
            et = t.get("exit_reason") or "?"
            kok = next((k for k in VAATLER
                        if et == k or et.startswith(k)), None)
            if kok is None:
                bilinmeyen[et] += 1
                continue
            hucreler[(motor, kok)].append(t)

    karne = {}
    for (motor, et), satirlar in sorted(hucreler.items()):
        karne.setdefault(et, {})[motor] = hucre_ozeti(satirlar, VAATLER[et])
    for et, motorlar in karne.items():           # etiket toplami
        hepsi = [t for (m, e), ss in hucreler.items() if e == et
                 for t in ss]
        motorlar["TOPLAM"] = hucre_ozeti(hepsi, VAATLER[et])

    cikti = {"uretim_ts": time.time(), "pencere_gun": a.gun,
             "karne": karne,
             "kapsanmayan_etiketler": dict(bilinmeyen),
             "not": ("hedef seviyeler genel tablodandir; motor-ozel "
                     "sapmalar kayma_puan icinde gorunur. karsi-olgusal "
                     "yok: o edge_rapor/simulatorun isi.")}
    (veri / "kural_karnesi.json").write_text(json.dumps(cikti, indent=1))

    print(f"KURAL KARNESI  son {a.gun:g} gun\n")
    for et in sorted(karne):
        t = karne[et]["TOPLAM"]
        print(f"[{et}]  vaad: {t['vaad']}")
        for motor in sorted(karne[et]):
            h = karne[et][motor]
            ek = ""
            if h["tur"] == "seviye":
                ek = f" hedef {h['hedef']:+.0f} kayma {h['kayma_puan']:+.2f}"
            if h.get("mfe_yakalama_medyan") is not None:
                ek = f" mfe-yakalama {h['mfe_yakalama_medyan']:.2f}"
            print(f"  {motor:>8} n={h['n']:<4} medyan {h['medyan_pnl_pct']:+6.2f}%"
                  f" [p10 {h['p10_pnl_pct']:+.1f} p90 {h['p90_pnl_pct']:+.1f}]"
                  f" uyum {h.get('uyum')} guven {h['guven']}{ek}")
        print()
    if bilinmeyen:
        print("kapsanmayan etiketler:",
              dict(sorted(bilinmeyen.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
