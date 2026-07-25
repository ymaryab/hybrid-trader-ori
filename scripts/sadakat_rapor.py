#!/usr/bin/env python3
"""Simulator sadakat raporu: GERCEK paper islemleri vs ARSIV REPLAY.

Amac (22 Tem sonda-olcekle dogrulamasi + edge zinciri sadakat terimi):
ayni giris anindan itibaren, ayni politika arsiv yolu uzerinde kosulunca
gercek defter sonucuna ne kadar yaklasiyor? Edge tablolarina guvenin
tavani bu rapordur.

Yontem: islem satirindan giris ani (ts - hold_sec) ve GERCEK giris
fiyati alinir; token yolunun giris sonrasi tick'leri gercek giris
fiyatina baglanarak replay edilir. Farklar uc kaynaktan gelir ve rapor
bunlari ayirt etmeye calisir: (1) kapsam (arsivde yol yok), (2) yol
cozunurlugu (dakika tick'leri arasi hareket), (3) yurutme (kayma/ucret;
gercek satirdaki friction_pct ile kiyaslanir).

Kullanim: python scripts/sadakat_rapor.py [--motor yz] [--gun 3]
Cikti: stdout + data/sadakat_rapor.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hibrit_trader.edge.simulator import degerlendir, tp_politikasi  # noqa: E402
from hibrit_trader.edge.yol_arsivi import Yol, YolArsivi             # noqa: E402

# Motor -> replay politikasi. yz: kural seti kesin (TP+2, felaket -20,
# timeout 60). v7hizli: felaket -20 VARSAYIM (kod tabani paylasik).
MOTOR_POLITIKALARI = {
    "yz": tp_politikasi(2.0, 60.0, stop_pct=-20.0),
    "v7hizli": tp_politikasi(2.0, 60.0, stop_pct=-20.0),
}

ETIKET_GRUBU = {
    "tp": "tp", "tp_2": "tp", "tp_5": "tp",
    "stop": "stop", "stop_felaket": "stop", "stop_gec": "stop",
    "timeout": "timeout", "timeout_60": "timeout",
    "timeout_karla": "timeout", "timeout_cuval": "timeout",
    "seri_sonu": "seri_sonu",
}


def grup(etiket: str) -> str:
    return ETIKET_GRUBU.get(etiket, "diger")


def islem_replay(t: dict, arsiv_yol, politika: dict) -> dict | None:
    """Tek islemi gercek giris fiyatindan arsiv uzerinde yeniden oynat."""
    giris_ts = float(t["ts"]) - float(t.get("hold_sec") or 0)
    giris_fiyat = float(t.get("entry_price") or 0)
    if giris_fiyat <= 0 or arsiv_yol is None:
        return None
    sonraki = [(ts, p) for ts, p in arsiv_yol.ticks if ts > giris_ts]
    if len(sonraki) < 2:
        return None
    r = degerlendir(Yol(t.get("token_address") or "?",
                        [(giris_ts, giris_fiyat)] + sonraki), politika)
    # COZUNURLUK RISKI: cikis karari, giristen uzun bosluk sonrasi ILK
    # tick'te dustuyse tick arasi TP/stop dokunusu GORULEMEZ (kotumser
    # yanlilik kaynagi). Bayrakla sayilir, duzeltilmeye CALISILMAZ.
    ilk_tick_sn = sonraki[0][0] - giris_ts
    riskli = (r["sure_dk"] <= (ilk_tick_sn / 60) + 0.01
              and ilk_tick_sn > 120)
    return {"replay_pnl": r["pnl_pct"], "replay_cikis": r["cikis"],
            "replay_sure_dk": r["sure_dk"], "tick_n": len(sonraki),
            "ilk_tick_sn": round(ilk_tick_sn, 1),
            "cozunurluk_riskli": riskli}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--motor", default="yz")
    ap.add_argument("--gun", type=float, default=3.0)
    ap.add_argument("--veri", default=os.getenv("MOMENTUM_DATA_DIR", "data"))
    a = ap.parse_args()
    veri = Path(a.veri)
    politika = MOTOR_POLITIKALARI.get(a.motor)
    if politika is None:
        raise SystemExit(f"politika tanimsiz: {a.motor} "
                         f"(mevcut: {sorted(MOTOR_POLITIKALARI)})")

    esik = time.time() - a.gun * 86400
    islemler = []
    for ln in open(veri / f"{a.motor}_trades.jsonl"):
        if not ln.strip():
            continue
        try:
            t = json.loads(ln)
        except ValueError:
            continue
        if t.get("type") or float(t.get("ts") or 0) < esik:
            continue
        islemler.append(t)

    arsiv = {y.token: y for y in YolArsivi(veri).yollar()}
    ciftler = []
    kapsam_yok = 0
    for t in islemler:
        r = islem_replay(t, arsiv.get(t.get("token_address")), politika)
        if r is None:
            kapsam_yok += 1
            continue
        ciftler.append({
            "token": (t.get("token_address") or "")[:8],
            "gercek_pnl": float(t.get("pnl_pct") or 0),
            "gercek_cikis": t.get("exit_reason"),
            "friction_pct": t.get("friction_pct"),
            **r,
            "fark": round(r["replay_pnl"] - float(t.get("pnl_pct") or 0), 3),
            "etiket_uyum": grup(r["replay_cikis"])
                           == grup(t.get("exit_reason") or "")})

    rapor = {"uretim_ts": time.time(), "motor": a.motor,
             "politika": politika, "pencere_gun": a.gun,
             "islem_n": len(islemler), "kapsam_yok_n": kapsam_yok,
             "replay_n": len(ciftler)}
    if ciftler:
        farklar = [c["fark"] for c in ciftler]
        temiz = [c for c in ciftler if not c.get("cozunurluk_riskli")]
        rapor.update({
            "cozunurluk_riskli_n": sum(
                1 for c in ciftler if c.get("cozunurluk_riskli")),
            "medyan_ilk_tick_sn": round(median(
                c["ilk_tick_sn"] for c in ciftler), 1),
            "temiz_n": len(temiz),
            "temiz_medyan_mutlak_fark": round(median(
                abs(c["fark"]) for c in temiz), 3) if temiz else None,
            "temiz_ort_fark": round(sum(c["fark"] for c in temiz)
                                    / len(temiz), 3) if temiz else None,
            "temiz_etiket_uyum": round(sum(
                1 for c in temiz if c["etiket_uyum"]) / len(temiz), 3)
                if temiz else None,
            "kapsam_orani": round(len(ciftler) / len(islemler), 3),
            "etiket_uyum_orani": round(
                sum(1 for c in ciftler if c["etiket_uyum"]) / len(ciftler), 3),
            "medyan_mutlak_fark": round(median(abs(f) for f in farklar), 3),
            "ort_fark": round(sum(farklar) / len(farklar), 3),
            "medyan_friction_pct": round(median(
                float(c["friction_pct"]) for c in ciftler
                if c["friction_pct"] is not None), 3) if any(
                c["friction_pct"] is not None for c in ciftler) else None,
            "gercek_toplam_pnl_pct": round(
                sum(c["gercek_pnl"] for c in ciftler), 2),
            "replay_toplam_pnl_pct": round(
                sum(c["replay_pnl"] for c in ciftler), 2),
            "en_kotu_5": sorted(ciftler, key=lambda c: -abs(c["fark"]))[:5]})
    (veri / "sadakat_rapor.json").write_text(json.dumps(rapor, indent=1))
    print(json.dumps({k: v for k, v in rapor.items() if k != "en_kotu_5"},
                     indent=1))
    for c in rapor.get("en_kotu_5", []):
        print("  en_kotu:", c["token"], "gercek", c["gercek_pnl"],
              c["gercek_cikis"], "| replay", c["replay_pnl"],
              c["replay_cikis"], "| fark", c["fark"])


if __name__ == "__main__":
    main()
