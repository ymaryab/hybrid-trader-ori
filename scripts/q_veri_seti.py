#!/usr/bin/env python3
"""q veri seti birlestirici (HAT 2, 25 Tem). TUREV, yeniden uretilebilir.

Sensor HAM olaylarini (HolderKonsantrasyon, LPKilit, ErkenAlici) +
yaratici sicilini token bazinda EKG yol sonuclariyla birlestirir:

    data/q_veri_seti.jsonl : satir = {token, q: {...}, yol: {...}}

Ilkeler:
- OGRENME YOK, skor yok: yalniz beyan edilen turetimler (pay oranlari).
- Ham-veri ilkesi: turetim her calismada bastan; omurga degismez.
- Token basina ILK (terfi ani) sensor olcumu kullanilir: karar-ani
  bilgisi sorusu ancak boyle durust sorulur (gelecek sizintisi yok).
- Kapsam durustlugu: her q alani icin eksikler sayilir ve raporlanir.

Kullanim: python scripts/q_veri_seti.py [--veri data]
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hibrit_trader.edge.yol_arsivi import YolArsivi   # noqa: E402


def _satirlar(yolad: str):
    if yolad.endswith(".zst"):
        p = subprocess.run(["zstd", "-dc", yolad],
                           capture_output=True, check=True)
        fh = io.BytesIO(p.stdout)
    else:
        fh = open(yolad, "rb")
    with fh:
        for ln in fh:
            if not ln.strip():
                continue
            try:
                yield json.loads(ln)
            except ValueError:
                continue


def sensor_ilk_olcumler(veri: Path) -> dict[str, dict]:
    """token -> {holder: ilk olay, lp: ilk olay, erken: ilk olay}"""
    ilk: dict[str, dict] = {}
    for yolad in sorted(glob.glob(
            str(veri / "gozlem/events/*/*.sensor.jsonl*"))):
        for e in _satirlar(yolad):
            tok = e.get("token")
            k = e.get("kind")
            alan = {"HolderKonsantrasyon": "holder", "LPKilit": "lp",
                    "ErkenAlici": "erken"}.get(k)
            if not tok or alan is None:
                continue
            kayit = ilk.setdefault(tok, {})
            if alan not in kayit:                  # ILK olcum = terfi ani
                kayit[alan] = e
    return ilk


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def holder_q(ev: dict) -> dict:
    pl = ev.get("payload") or {}
    arz = _f((pl.get("arz") or {}).get("uiAmount"))
    miktarlar = sorted((m for m in (_f(h.get("miktar"))
                                    for h in pl.get("hesaplar") or [])
                        if m is not None), reverse=True)
    q = {"ts_ms": ev.get("ts_ms"), "hesap_n": len(miktarlar)}
    if arz and arz > 0 and miktarlar:
        q["top1_pay"] = round(miktarlar[0] / arz, 4)
        q["top5_pay"] = round(sum(miktarlar[:5]) / arz, 4)
        q["top10_pay"] = round(sum(miktarlar[:10]) / arz, 4)
    return q


def lp_q(ev: dict) -> dict:
    pl = ev.get("payload") or {}
    q = {"ts_ms": ev.get("ts_ms"), "amm": pl.get("amm"),
         "parse_guvenli": pl.get("parse_guvenli")}
    arz = _f((pl.get("lp_arz") or {}).get("uiAmount"))
    hesaplar = [(_f(h.get("miktar")) or 0.0)
                for h in pl.get("lp_hesaplar") or []]
    if arz and arz > 0 and hesaplar:
        q["lp_top1_pay"] = round(max(hesaplar) / arz, 4)
    return q


def erken_q(ev: dict) -> dict:
    pl = ev.get("payload") or {}
    return {"ts_ms": ev.get("ts_ms"), "alici_n": pl.get("alici_n"),
            "yeni_n": pl.get("yeni_n"), "eski_n": pl.get("eski_n"),
            "yeni_eski_orani": pl.get("yeni_eski_orani"),
            "ort_yas_gun": pl.get("ort_yas_gun"),
            "medyan_yas_gun": pl.get("medyan_yas_gun"),
            "dagilim": pl.get("dagilim"),
            "kapsam": pl.get("kapsam")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--veri", default="data")
    a = ap.parse_args()
    veri = Path(a.veri)

    try:
        yaratici_map = json.loads((veri / "yaratici_map.json").read_text())
        sicil = json.loads(
            (veri / "yaratici_sicili.json").read_text()).get("sicil") or {}
    except (OSError, ValueError):
        yaratici_map, sicil = {}, {}

    ilk = sensor_ilk_olcumler(veri)
    yollar = {y.token: y for y in YolArsivi(veri).yollar()}

    kapsam = Counter()
    satir_n = 0
    with open(veri / "q_veri_seti.jsonl", "w") as out:
        for tok in sorted(set(ilk) | set(yollar)):
            olc = ilk.get(tok) or {}
            yar = sicil.get(yaratici_map.get(tok))
            q = {
                "holder": holder_q(olc["holder"]) if "holder" in olc else None,
                "lp": lp_q(olc["lp"]) if "lp" in olc else None,
                "erken": erken_q(olc["erken"]) if "erken" in olc else None,
                "yaratici": yar,
            }
            yol = yollar.get(tok)
            yol_oz = None
            if yol is not None:
                yol_oz = {"ath_pct": round(yol.ath_pct, 2),
                          "yasam_dk": round(yol.yasam_dk, 1),
                          "tick_n": len(yol.ticks),
                          "dogum_ts": yol.ticks[0][0]}
            for ad, deger in (("holder", q["holder"]), ("lp", q["lp"]),
                              ("erken", q["erken"]),
                              ("yaratici", q["yaratici"]), ("yol", yol_oz)):
                kapsam[ad + ("_var" if deger is not None else "_yok")] += 1
            out.write(json.dumps({"token": tok, "q": q, "yol": yol_oz})
                      + "\n")
            satir_n += 1
    ozet = {"uretim_ts": time.time(), "satir_n": satir_n,
            "kapsam": dict(kapsam),
            "tam_q_ve_yol": sum(
                1 for tok in set(ilk) & set(yollar)
                if len(ilk[tok]) == 3 and yaratici_map.get(tok) in sicil)}
    print(json.dumps(ozet, indent=1))


if __name__ == "__main__":
    main()
