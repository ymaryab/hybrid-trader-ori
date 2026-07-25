#!/usr/bin/env python3
"""3-gunluk motor karar raporu — revize/kaldir/canliya al onerisi.

9 motor son 3 gunluk performansini karsilastir (winrate, PnL, MFE ort,
exit dagilimi). En iyi motoru "canliya al" one, en kotu 2 motoru
"revize veya kaldir" oner. Systemd timer: her 3 gun 05:00 UTC.

Kullanim:
    python scripts/motor_karar_raporu.py [--gun 3] [--no-telegram]
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

DATA = Path("/home/bot/yz/hybrid-trader-ori/data")
MOTORLAR = ("v7", "v7c", "v7cd", "v7d", "v7hizli", "r1", "v7y", "v7t", "v7m")


def yukle(motor: str) -> list[dict]:
    tp = DATA / f"{motor}_trades.jsonl"
    if not tp.exists():
        return []
    return [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]


def motor_metrik(motor: str, cutoff: float) -> dict:
    trades = [t for t in yukle(motor) if float(t.get("ts") or 0) >= cutoff]
    n = len(trades)
    if n == 0:
        return {"n": 0, "aktif": False}
    pnl = sum(t.get("pnl_usd") or 0 for t in trades)
    w = sum(1 for t in trades if (t.get("pnl_usd") or 0) > 0)
    mfeler = [t.get("mfe_pct") or 0 for t in trades]
    reasons = Counter(t.get("exit_reason", "?") for t in trades)
    # skor: n × win% × ort_pnl (basit karma)
    skor = (n * (w / n) * (pnl / n)) if n > 0 else -999
    return {
        "n": n, "aktif": True,
        "pnl": pnl, "ort_pnl": pnl / n,
        "wr": w * 100 / n,
        "mfe_ort": sum(mfeler) / n,
        "mfe_max": max(mfeler),
        "reasons": dict(reasons),
        "skor": skor,
    }


def rapor(gun: int) -> str:
    cutoff = time.time() - gun * 86400
    veri = {m: motor_metrik(m, cutoff) for m in MOTORLAR}

    aktif_motorlar = [(m, d) for m, d in veri.items() if d.get("aktif")]
    aktif_motorlar.sort(key=lambda x: x[1]["skor"], reverse=True)

    md = [f"📊 *Motor Karar Raporu ({gun}g)*"]
    md.append(f"{'motor':<9} {'n':>3} {'wr%':>5} {'pnl':>8} {'ort':>7} {'mfe_ort':>7}")
    md.append("─" * 50)
    for m, d in aktif_motorlar:
        md.append(f"{m.upper():<9} {d['n']:>3} {d['wr']:>4.0f}% {d['pnl']:>+8.2f} "
                  f"{d['ort_pnl']:>+7.2f} {d['mfe_ort']:>+7.2f}")

    inaktif = [m for m, d in veri.items() if not d.get("aktif")]
    if inaktif:
        md.append(f"\n_Inaktif ({gun}g'de 0 islem):_ {', '.join(m.upper() for m in inaktif)}")

    if aktif_motorlar:
        en_iyi_m, en_iyi_d = aktif_motorlar[0]
        md.append(f"\n🏆 *En iyi ({gun}g):* {en_iyi_m.upper()} "
                  f"(wr {en_iyi_d['wr']:.0f}%, pnl ${en_iyi_d['pnl']:+.2f})")
        md.append(f"→ Aday: **canliya al** (`canli-swap {en_iyi_m}`)")

        # En kotu 2 (skoru negatif olanlar)
        kotu = [(m, d) for m, d in aktif_motorlar if d["skor"] < 0]
        if kotu:
            md.append(f"\n⚠️ *Revize/Kaldir oneri:*")
            for m, d in kotu[-2:]:
                sebep = "yuksek zarar" if d["pnl"] < 0 else "dusuk winrate"
                md.append(f"  {m.upper()}: wr {d['wr']:.0f}% pnl ${d['pnl']:+.2f} ({sebep})")
    return "\n".join(md)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gun", type=int, default=3)
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args()

    r = rapor(a.gun)
    print(r)

    # Diske yaz
    out = DATA / f"motor_karar_{time.strftime('%Y-%m-%d')}.md"
    out.write_text(r, encoding="utf-8")

    if not a.no_telegram:
        try:
            sys.path.insert(0, "/home/bot/yz/hybrid-trader-ori/src")
            from hibrit_trader import config  # load_dotenv
            from hibrit_trader.killswitch import notify
            notify(r)
            print("\nTelegram gonderildi.")
        except Exception as e:
            print(f"\nTelegram HATA: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
