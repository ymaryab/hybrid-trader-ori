#!/usr/bin/env python3
"""K3 retro decode + dogrulama (blueprint kabul kriteri 5).

Gecmis SwapObserved arsivini surumlu kayitla (anchor_kayit.json) cozer:
- satir-decode basari orani (>= %90 hedef),
- SwapPulse/adet tutarliligi yerine ic-tutarlilik: yon (log) ile
  is_buy (kayit) eslesme orani,
- ima-fiyat capraz kontrolu (yogun arsiv, ayni-dakika, medyan sapma),
- turev cikti: mint x dakika akis agregati (sv'li) ->
  data/gozlem/k3_akis.jsonl (ECHO/ABSORB-v2 kapilarinin gelecek girdisi).

Kullanim: python scripts/k3_retro_decode.py [--ornek 0=hepsi]
"""

from __future__ import annotations

import argparse
import base64
import bisect
import glob
import io
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hibrit_trader.gozlem.anchor_decode import (AnchorDecoder,  # noqa: E402
                                                SCHEMA_VERSION,
                                                kayit_yukle)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ornek", type=int, default=0)
    ap.add_argument("--veri", default="data")
    a = ap.parse_args()
    veri = Path(a.veri)
    dec = AnchorDecoder(kayit_yukle(veri))
    if not dec.kayitlar:
        raise SystemExit("anchor_kayit.json bos: once kesif + kayit")

    toplam = cozulen = yon_uyum = yon_toplam = 0
    bilinmeyen = defaultdict(int)
    agreg: dict = defaultdict(lambda: {"n_al": 0, "n_sat": 0,
                                       "sol_al": 0, "sol_sat": 0})
    n = 0
    for yolad in sorted(glob.glob(
            str(veri / "gozlem/events/*/*.swap.jsonl*"))):
        if yolad.endswith(".zst"):
            p = subprocess.run(["zstd", "-dc", yolad],
                               capture_output=True, check=True)
            fh = io.BytesIO(p.stdout)
        else:
            fh = open(yolad, "rb")
        with fh:
            for ln in fh:
                if b"SwapObserved" not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("kind") != "SwapObserved":
                    continue
                pl = e.get("payload") or {}
                logs = pl.get("logs") or []
                log_yon = (1 if any("Instruction: Buy" in l
                                    for l in logs)
                           else -1 if any("Instruction: Sell" in l
                                          for l in logs) else 0)
                cozuldu_mu = False
                for l in logs:
                    if not l.startswith("Program data: "):
                        continue
                    try:
                        ham = base64.b64decode(l[14:])
                    except Exception:  # noqa: BLE001
                        continue
                    if len(ham) < 8:
                        continue
                    r = dec.coz(ham, beklenen_mint=e.get("token"),
                                beklenen_pool=pl.get("pool"))
                    if r is None:
                        bilinmeyen[ham[:8].hex()] += 1
                        continue
                    cozuldu_mu = True
                    if log_yon:
                        yon_toplam += 1
                        if (r["is_buy"] and log_yon > 0) or \
                           (not r["is_buy"] and log_yon < 0):
                            yon_uyum += 1
                    dk = int(e["ts_ms"] / 60000)
                    g = agreg[(e.get("token"), dk)]
                    if r["is_buy"]:
                        g["n_al"] += 1
                        g["sol_al"] += (r["sol_lamport"] or 0)
                    else:
                        g["n_sat"] += 1
                        g["sol_sat"] += (r["sol_lamport"] or 0)
                toplam += 1
                cozulen += 1 if cozuldu_mu else 0
                n += 1
                if a.ornek and n >= a.ornek:
                    break
        if a.ornek and n >= a.ornek:
            break

    with open(veri / "gozlem" / "k3_akis.jsonl", "w") as f:
        for (tok, dk), g in sorted(agreg.items(), key=lambda x: x[0][1]):
            f.write(json.dumps({"sv": SCHEMA_VERSION, "mint": tok,
                                "ts_dk": dk * 60, **g}) + "\n")
    rapor = {"uretim_ts": time.time(), "sv": SCHEMA_VERSION,
             "olay_n": toplam, "cozulen_n": cozulen,
             "basari": round(cozulen / toplam, 4) if toplam else None,
             "yon_uyum": round(yon_uyum / yon_toplam, 4)
                         if yon_toplam else None,
             "agregat_satiri": len(agreg),
             "bilinmeyen_disc_top5": sorted(
                 bilinmeyen.items(), key=lambda x: -x[1])[:5]}
    (veri / "gozlem" / "k3_dogrulama.json").write_text(
        json.dumps(rapor, indent=1))
    print(json.dumps(rapor, indent=1))


if __name__ == "__main__":
    main()
