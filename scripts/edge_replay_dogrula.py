#!/usr/bin/env python3
"""Edge v2 karar replay dogrulamasi (HIGH-8, DecisionContext disiplini).

Ardisik EdgeShadowEvaluated(v2) ciftleri icin: k olayinin loglanmis
DURUMUNDAN baslayip k+1 olayinin loglanmis GIRDILERIYLE cekirdegi
yeniden kosar; uretilen karar loglanandan farkliysa UYUSMAZLIK sayar.
Ayrica loglanan parametrelerle mevcut kod sabitleri farkliysa
(parametre drift) raporlar. Cikti: stdout + data/edge_replay_dogrula.json

Kullanim: python scripts/edge_replay_dogrula.py [--saat 24]
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import hibrit_trader.edge.cekirdek as ck  # noqa: E402


def olaylar(veri: Path, saat: float):
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
                if b'"surum": "v2"' not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if (e.get("kind") == "EdgeShadowEvaluated"
                        and e.get("ts_ms", 0) >= esik):
                    pl = e.get("payload") or {}
                    v2 = pl.get("v2") or {}
                    if v2.get("katman") == "cekirdek":
                        yield e["ts_ms"], pl, v2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saat", type=float, default=24.0)
    ap.add_argument("--veri", default=os.getenv("MOMENTUM_DATA_DIR", "data"))
    a = ap.parse_args()
    veri = Path(a.veri)
    seri = sorted(olaylar(veri, a.saat))
    esles = uyusmaz = drift = 0
    ornekler = []
    for k in range(len(seri) - 1):
        _, pl0, v0 = seri[k]
        _, pl1, v1 = seri[k + 1]
        if v1.get("tur") != v0.get("tur", 0) + 1:
            continue                      # restart/tatbikat arasi: atla
        prm = v1.get("parametreler") or {}
        if (prm.get("lcb_k") != ck.LCB_K or prm.get("marj") != ck.AILE_MARJ
                or prm.get("teyit") != ck.TEYIT_TUR
                or prm.get("cooldown") != ck.COOLDOWN_TUR):
            drift += 1
            continue                      # farkli parametreyle replay yaniltir
        c = ck.Cekirdek()
        c.durum_yukle(v0["aile"], v0.get("bekleyen_aday"),
                      v0.get("tur", 0), v0.get("son_gecis_turu", -10**9))
        skorlar = {m: {"pct": p, "islem":
                       (pl1.get("girdi_islem") or {}).get(m, 0)}
                   for m, p in (pl1.get("edgeler") or {}).items()}
        try:
            yeni = c.karar(skorlar)
        except Exception as e:  # noqa: BLE001
            uyusmaz += 1
            ornekler.append({"tur": v1.get("tur"), "hata": str(e)[:80]})
            continue
        ayni = (yeni["aile"] == v1["aile"]
                and yeni["bekleyen_aday"] == v1.get("bekleyen_aday"))
        if ayni:
            esles += 1
        else:
            uyusmaz += 1
            if len(ornekler) < 5:
                ornekler.append({"tur": v1.get("tur"),
                                 "log": {"aile": v1["aile"],
                                         "bekleyen": v1.get("bekleyen_aday")},
                                 "replay": {"aile": yeni["aile"],
                                            "bekleyen": yeni["bekleyen_aday"]}})
    rapor = {"uretim_ts": time.time(), "cift_n": esles + uyusmaz,
             "eslesen": esles, "uyusmayan": uyusmaz,
             "parametre_drift": drift,
             "eslesme_orani": round(esles / (esles + uyusmaz), 4)
                              if esles + uyusmaz else None,
             "ornekler": ornekler}
    (veri / "edge_replay_dogrula.json").write_text(json.dumps(rapor, indent=1))
    print(json.dumps(rapor, indent=1))


if __name__ == "__main__":
    main()
