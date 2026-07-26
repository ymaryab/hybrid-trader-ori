#!/usr/bin/env python3
"""Edge zinciri raporu (HAT 2, 25 Tem): arsiv edge tablosu + golge uyumu.

Bolum 1: son N saatte DOGAN yollar uzerinde politika ailesi edge'leri
         (EdgeMotoru: arsiv x simulator, ogrenme yok).
Bolum 2: EdgeShadowEvaluated olaylarindan uyum orani + sapma dagilimi.

Kullanim: python scripts/edge_rapor.py [--veri data] [--saat 24]
Cikti: stdout ozet + data/edge_rapor.json (turev, yeniden uretilebilir).
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

from hibrit_trader.edge.edge_motoru import EdgeMotoru          # noqa: E402
from hibrit_trader.edge.simulator import (runner_politikasi,   # noqa: E402
                                          tp_politikasi)
from hibrit_trader.edge.yol_arsivi import (GozlemYolArsivi,    # noqa: E402
                                           YolArsivi)

POLITIKALAR = {
    "TP2": tp_politikasi(2, 60),
    "TP5": tp_politikasi(5, 60),
    "RUN": runner_politikasi(25, 10, 360),
}


TAMAMLANMIS_SN = 1800.0   # censoring on-kaydi: son tick >=30dk eski


class PencereliArsiv:
    """Yalniz son N saatte dogan ve TAMAMLANMIS yollari sunar.

    CENSORING KURALI (26 Tem on-kayit, HIGH-7): kosusu suren yol
    (son tick'i 30 dk'dan taze) edge tablolarina GIRMEZ; tepe aninda
    kesilmis yol runner edge'ini sistematik abartirdi. Dislanan adet
    raporda beyan edilir (sessiz kirpma yasak)."""

    def __init__(self, arsiv: YolArsivi, saat: float):
        self.arsiv = arsiv
        self.esik = time.time() - saat * 3600
        self.dislanan_suren = 0

    def yollar(self):
        simdi = time.time()
        for yol in self.arsiv.yollar():
            if yol.ticks[0][0] < self.esik:
                continue
            if simdi - yol.ticks[-1][0] < TAMAMLANMIS_SN:
                self.dislanan_suren += 1
                continue
            yield yol


def golge_olaylari(veri: Path, saat: float):
    esik_ms = (time.time() - saat * 3600) * 1000
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
                        and e.get("ts_ms", 0) >= esik_ms):
                    yield e


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--veri", default="data")
    ap.add_argument("--saat", type=float, default=24.0)
    ap.add_argument("--kaynak", choices=("ekg", "anlik"), default="ekg")
    a = ap.parse_args()
    veri = Path(a.veri)

    if a.kaynak == "anlik":
        gunler = sorted({time.strftime("%Y%m%d", time.gmtime(
            time.time() - g * 3600)) for g in range(int(a.saat) + 2)})
        kaynak = GozlemYolArsivi(veri, gun_onek=gunler)
    else:
        kaynak = YolArsivi(veri)
    em = EdgeMotoru(PencereliArsiv(kaynak, a.saat))
    tablo = {ad: em.edge(pol) for ad, pol in POLITIKALAR.items()}

    uyum = Counter()
    sapmalar = Counter()
    son_sapmalar = []
    for e in golge_olaylari(veri, a.saat):
        pl = e.get("payload") or {}
        uyum["uyum" if pl.get("uyum") else "sapma"] += 1
        if not pl.get("uyum"):
            sapmalar[pl.get("sapma_nedeni") or "?"] += 1
            son_sapmalar.append({
                "ts_ms": e.get("ts_ms"),
                "golge": pl.get("golge_aday"),
                "legacy": pl.get("legacy_hedef"),
                "karar": pl.get("legacy_karar"),
                "neden": pl.get("sapma_nedeni")})
    rapor = {
        "uretim_ts": time.time(), "pencere_saat": a.saat,
        "kaynak": a.kaynak,
        "censoring": {"kural": "tamamlanmis_30dk",
                      "dislanan_suren_yol": em.arsiv.dislanan_suren},
        "sadakat_notu": ("EKG dakika-cozunurluklu, tetik-kosullu; kayma/"
                         "ucret yok. Edge hatasi = kosullama + simulator "
                         "sadakati; ayri degerlendirin."),
        "arsiv_edge": tablo,
        "golge": {"olay_n": sum(uyum.values()),
                  "uyum_n": uyum.get("uyum", 0),
                  "sapma_n": uyum.get("sapma", 0),
                  "uyum_orani": (round(uyum["uyum"] / sum(uyum.values()), 3)
                                 if sum(uyum.values()) else None),
                  "sapma_nedenleri": dict(sapmalar),
                  "son_sapmalar": son_sapmalar[-5:]},
    }
    ad = "edge_rapor_anlik.json" if a.kaynak == "anlik" else "edge_rapor.json"
    (veri / ad).write_text(json.dumps(rapor, indent=1))
    print(json.dumps(rapor, indent=1))


if __name__ == "__main__":
    main()
