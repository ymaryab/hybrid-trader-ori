#!/usr/bin/env python3
"""Forensic Factory CLI (salt-okur).

Ornekler:
  python3 scripts/forensic_fabrika.py --kohort gunluk_en_kotu_n --n 4
  python3 scripts/forensic_fabrika.py --kohort pareto_zarar --pay 0.5
  python3 scripts/forensic_fabrika.py --liste
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hibrit_trader.forensic import karsilastir, kohort, ozellik, rapor, veri


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kohort", default="gunluk_en_kotu_n")
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--pay", type=float, default=0.5)
    ap.add_argument("--esik", type=float, default=-15.0)
    ap.add_argument("--motorlar", default=",".join(veri.VARSAYILAN_MOTORLAR))
    ap.add_argument("--baslangic", default=None,
                    help="ISO (or. 2026-07-23T00:00:00Z); varsayilan guvenilir baslangic")
    ap.add_argument("--kirli", action="store_true",
                    help="guvenilir baslangictan onceki veriyi de al (damgalanir)")
    ap.add_argument("--cikti", default="data/forensic_son.json")
    ap.add_argument("--liste", action="store_true")
    a = ap.parse_args()

    if a.liste:
        print("KOHORTLAR")
        for k, v in kohort.liste().items():
            print("  %-18s %s" % (k, v))
        print("\nOZELLIKLER (giris aninda bilinen)")
        for k in ozellik.giris_anI():
            m = ozellik.meta(k)
            print("  %-22s %s%s" % (k, m["aciklama"],
                                    " [KISMI]" if m["kismi_alanlar"] else ""))
        print("\nOZELLIKLER (giristen sonra; yalniz teshis)")
        for k in ozellik.sonrasi():
            print("  %-22s %s" % (k, ozellik.meta(k)["aciklama"]))
        return 0

    try:
        ev = veri.yukle(tuple(a.motorlar.split(",")), baslangic=a.baslangic,
                        kirli_pencereye_izin=a.kirli)
    except veri.GuvenHatasi as e:
        print("GUVEN KAPISI:", e)
        return 2

    kw = {}
    if a.kohort == "gunluk_en_kotu_n":
        kw = {"n": a.n}
    elif a.kohort in ("pareto_zarar", "katki_kuyrugu"):
        kw = {"pay": a.pay}
    elif a.kohort == "esik_alti_pct":
        kw = {"esik": a.esik}
    hedef, kontrol = kohort.uygula(a.kohort, ev.islemler, **kw)
    if not hedef:
        print("kohort bos: secici hicbir islem dondurmedi")
        return 1

    imz = karsilastir.imza(hedef, kontrol)
    mal = karsilastir.maliyet_ozeti(hedef, ev.islemler)
    ornek = sorted(hedef, key=lambda t: t.get("pnl_usd") or 0)
    print(rapor.metin(ev.ozet(), a.kohort, mal, imz, ornek))
    rapor.json_yaz(Path(a.cikti), ev.ozet(), a.kohort, mal, imz)
    print("\njson: %s" % a.cikti)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
