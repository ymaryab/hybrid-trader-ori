#!/usr/bin/env python3
"""Nightly retro analiz — 4 motor trades.jsonl'i is le, oruntu cikar,
gunluk rapor + telegram ozet.

Uretilen dosya: data/retro_analiz_YYYY-MM-DD.md
Telegram: kisa ozet (5-6 satir), gunluk PnL/winrate + en dikkat cekici bulgular.

Kullanim: python scripts/retro_analiz.py [--gun N] (default 1 = dun)
Systemd timer ile her gece 00:15 UTC tetiklenir (Turkey time 03:15).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median

MOTORLAR = ("v7", "v7c", "v7d", "v7hizli")
DATA = Path("/home/bot/yz/hybrid-trader-ori/data")


def yukle_trades(motor: str) -> list[dict]:
    p = DATA / f"{motor}_trades.jsonl"
    if not p.exists():
        return []
    out = []
    for l in p.read_text().splitlines():
        if not l.strip():
            continue
        try:
            out.append(json.loads(l))
        except Exception:
            pass
    return out


def h1_bandi(h1: float) -> str:
    if h1 < 10: return "<10"
    if h1 < 20: return "10-20"
    if h1 < 30: return "20-30"
    if h1 < 40: return "30-40"
    if h1 < 50: return "40-50"
    return "50+"


def liq_bandi(liq: float) -> str:
    if liq < 100_000: return "<100k"
    if liq < 200_000: return "100-200k"
    if liq < 500_000: return "200-500k"
    if liq < 1_000_000: return "500k-1M"
    return "1M+"


def saat_bandi(ts: float) -> int:
    return datetime.fromtimestamp(ts, tz=timezone.utc).hour


def motor_ozet(trades: list[dict]) -> dict:
    """Bir motorun genel istatistikleri."""
    if not trades:
        return {"n": 0, "pnl": 0.0, "winrate": None, "ort": 0.0}
    n = len(trades)
    pnl = sum(t.get("pnl_usd") or 0 for t in trades)
    w = sum(1 for t in trades if (t.get("pnl_usd") or 0) > 0)
    return {
        "n": n, "pnl": pnl, "winrate": w * 100 / n,
        "ort": pnl / n,
        "en_iyi": max(trades, key=lambda t: t.get("pnl_usd") or 0),
        "en_kotu": min(trades, key=lambda t: t.get("pnl_usd") or 0),
        "ort_hold_sec": mean(t.get("hold_sec") or 0 for t in trades),
    }


def band_analiz(trades: list[dict], band_fn, isim: str) -> dict:
    """H1 / liq / saat bazli winrate + ort pnl."""
    grup = defaultdict(list)
    for t in trades:
        key = band_fn(t)
        grup[key].append(t)
    out = {}
    for k, ts in grup.items():
        pnl = sum(t.get("pnl_usd") or 0 for t in ts)
        w = sum(1 for t in ts if (t.get("pnl_usd") or 0) > 0)
        n = len(ts)
        out[k] = {"n": n, "pnl": pnl, "wr": w * 100 / n, "ort": pnl / n}
    return out


def token_analiz(trades: list[dict], top_n: int = 10) -> tuple[list, list]:
    """Token bazli net pnl - en iyi/en kotu."""
    tok = defaultdict(list)
    for t in trades:
        pair = (t.get("pair") or "").split(" /")[0] or "?"
        tok[pair].append(t.get("pnl_usd") or 0)
    net = [(name, sum(pnls), len(pnls)) for name, pnls in tok.items()]
    net.sort(key=lambda x: x[1], reverse=True)
    return net[:top_n], net[-top_n:]


def rejim_korelasyon(trades: list[dict]) -> dict:
    """sol_chg_h1 bandinda winrate + ort pnl."""
    band = defaultdict(list)
    for t in trades:
        sh = t.get("sol_chg_h1")
        if sh is None:
            continue
        if sh < 0: k = "<0"
        elif sh < 0.5: k = "0-0.5"
        elif sh < 1: k = "0.5-1"
        elif sh < 2: k = "1-2"
        else: k = "2+"
        band[k].append(t.get("pnl_usd") or 0)
    out = {}
    for k, ps in band.items():
        n = len(ps)
        pnl = sum(ps)
        w = sum(1 for p in ps if p > 0)
        out[k] = {"n": n, "pnl": pnl, "wr": w * 100 / n if n else 0, "ort": pnl / n if n else 0}
    return out


def rapor_yaz(gun_dt: datetime, out_path: Path, tum_motor_data: dict) -> str:
    """Markdown rapor uret + geri dondur (telegram icin ozet)."""
    tarih = gun_dt.strftime("%Y-%m-%d")
    md = [f"# Retro Analiz — {tarih} (UTC)\n"]

    # Kismi 1: motor bazli ozet
    md.append("## Motor Ozeti\n")
    md.append("| Motor | n | PnL | Ort | Winrate | Ort Hold |")
    md.append("|-------|---|-----|-----|---------|----------|")
    toplam_n = 0
    toplam_pnl = 0.0
    for m in MOTORLAR:
        o = tum_motor_data[m]["ozet"]
        if o["n"] == 0:
            md.append(f"| {m.upper()} | 0 | - | - | - | - |")
            continue
        toplam_n += o["n"]
        toplam_pnl += o["pnl"]
        md.append(f"| {m.upper()} | {o['n']} | ${o['pnl']:+.2f} | ${o['ort']:+.2f} | "
                  f"{o['winrate']:.0f}% | {o['ort_hold_sec']/60:.1f} dk |")
    md.append(f"\n**Toplam**: {toplam_n} islem, PnL **${toplam_pnl:+.2f}**\n")

    # Kismi 2: en iyi motor detay
    en_iyi_m = max(MOTORLAR, key=lambda m: tum_motor_data[m]["ozet"].get("pnl", 0))
    if tum_motor_data[en_iyi_m]["ozet"]["n"] > 0:
        md.append(f"## En Iyi Motor: {en_iyi_m.upper()}\n")

        # H1 bandi
        h1_b = tum_motor_data[en_iyi_m]["h1_band"]
        if h1_b:
            md.append("### H1 momentum bandi")
            md.append("| h1 | n | PnL | Ort | Winrate |")
            md.append("|----|---|-----|-----|---------|")
            for k in ("10-20", "20-30", "30-40", "40-50", "50+"):
                if k in h1_b:
                    b = h1_b[k]
                    md.append(f"| {k} | {b['n']} | ${b['pnl']:+.2f} | ${b['ort']:+.2f} | {b['wr']:.0f}% |")
            md.append("")

        # Liq bandi
        liq_b = tum_motor_data[en_iyi_m]["liq_band"]
        if liq_b:
            md.append("### Likidite bandi")
            md.append("| liq | n | PnL | Ort | Winrate |")
            md.append("|-----|---|-----|-----|---------|")
            for k in ("<100k", "100-200k", "200-500k", "500k-1M", "1M+"):
                if k in liq_b:
                    b = liq_b[k]
                    md.append(f"| {k} | {b['n']} | ${b['pnl']:+.2f} | ${b['ort']:+.2f} | {b['wr']:.0f}% |")
            md.append("")

        # Saat
        s_b = tum_motor_data[en_iyi_m]["saat_band"]
        if s_b:
            md.append("### Saat (UTC)")
            md.append("| saat | n | PnL | Wr |")
            md.append("|------|---|-----|-----|")
            for h in sorted(s_b.keys()):
                b = s_b[h]
                md.append(f"| {h:02d} | {b['n']} | ${b['pnl']:+.2f} | {b['wr']:.0f}% |")
            md.append("")

        # Rejim
        rej = tum_motor_data[en_iyi_m]["rejim"]
        if rej:
            md.append("### Rejim (sol_chg_h1) korelasyon")
            md.append("| sol_h1 | n | PnL | Ort | Wr |")
            md.append("|--------|---|-----|-----|-----|")
            for k in ("<0", "0-0.5", "0.5-1", "1-2", "2+"):
                if k in rej:
                    b = rej[k]
                    md.append(f"| {k} | {b['n']} | ${b['pnl']:+.2f} | ${b['ort']:+.2f} | {b['wr']:.0f}% |")
            md.append("")

        # Token top
        top, kotu = tum_motor_data[en_iyi_m]["token"]
        if top:
            md.append("### En kar getiren tokenler")
            md.append("| token | n | net PnL |")
            md.append("|-------|---|---------|")
            for t, p, n in top[:5]:
                md.append(f"| {t} | {n} | ${p:+.2f} |")
            md.append("")
            md.append("### En kaybettiren tokenler")
            md.append("| token | n | net PnL |")
            md.append("|-------|---|---------|")
            for t, p, n in kotu[:5]:
                md.append(f"| {t} | {n} | ${p:+.2f} |")
            md.append("")

    # Kismi 3: Exit reason (tum motorlar)
    md.append("## Exit Reason Dagilimi (tum motorlar)\n")
    md.append("| motor | tp_2 | stop_gec | stop_felaket | timeout_60 | manuel |")
    md.append("|-------|------|----------|--------------|------------|--------|")
    for m in MOTORLAR:
        r = tum_motor_data[m]["reasons"]
        md.append(f"| {m.upper()} | {r.get('tp_2',0)} | {r.get('stop_gec',0)} | "
                  f"{r.get('stop_felaket',0)} | {r.get('timeout_60',0)} | "
                  f"{r.get('manuel_kapanis',0)} |")
    md.append("")

    out_path.write_text("\n".join(md), encoding="utf-8")

    # Telegram ozet (kisa)
    ozet_satir = []
    ozet_satir.append(f"📊 *Retro {tarih}*")
    ozet_satir.append(f"Toplam: {toplam_n} islem, PnL ${toplam_pnl:+.2f}")
    for m in MOTORLAR:
        o = tum_motor_data[m]["ozet"]
        if o["n"] > 0:
            ozet_satir.append(f"{m.upper()}: n={o['n']} pnl=${o['pnl']:+.2f} wr={o['winrate']:.0f}%")
    ozet_satir.append(f"🏆 En iyi motor: *{en_iyi_m.upper()}* (${tum_motor_data[en_iyi_m]['ozet']['pnl']:+.2f})")
    # En iyi h1 bandi ipucu
    if tum_motor_data[en_iyi_m]["ozet"]["n"] > 0:
        h1_b = tum_motor_data[en_iyi_m]["h1_band"]
        if h1_b:
            en_iyi_h1 = max(h1_b, key=lambda k: h1_b[k]["pnl"])
            ozet_satir.append(f"🎯 En iyi h1 bandi: {en_iyi_h1} (${h1_b[en_iyi_h1]['pnl']:+.2f}, wr {h1_b[en_iyi_h1]['wr']:.0f}%)")
    return "\n".join(ozet_satir)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gun", type=int, default=1, help="Kac gun onceki (1=dun, 0=bugun)")
    ap.add_argument("--all", action="store_true", help="Tum verilerle (gun filtresi yok)")
    ap.add_argument("--no-telegram", action="store_true", help="Telegram gonderme")
    a = ap.parse_args()

    gun_dt = datetime.now(timezone.utc) - timedelta(days=a.gun)
    gun_baslangic = gun_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    gun_bitis = gun_baslangic + timedelta(days=1)

    tum_motor_data = {}
    for m in MOTORLAR:
        trades = yukle_trades(m)
        if not a.all:
            trades = [t for t in trades if
                      gun_baslangic.timestamp() <= (t.get("ts") or 0) < gun_bitis.timestamp()]
        tum_motor_data[m] = {
            "ozet": motor_ozet(trades),
            "h1_band": band_analiz(trades, lambda t: h1_bandi(t.get("chg_h1") or 0), "h1"),
            "liq_band": band_analiz(trades, lambda t: liq_bandi(t.get("liq_entry") or 0), "liq"),
            "saat_band": band_analiz(trades, lambda t: saat_bandi(t.get("ts") or 0), "saat"),
            "token": token_analiz(trades),
            "reasons": Counter(t.get("exit_reason", "?") for t in trades),
            "rejim": rejim_korelasyon(trades),
        }

    tarih = gun_baslangic.strftime("%Y-%m-%d")
    out = DATA / f"retro_analiz_{tarih}.md"
    out.parent.mkdir(exist_ok=True)
    ozet = rapor_yaz(gun_baslangic, out, tum_motor_data)
    print(f"Rapor yazildi: {out}")
    print()
    print(ozet)

    if not a.no_telegram:
        try:
            sys.path.insert(0, "/home/bot/yz/hybrid-trader-ori/src")
            from hibrit_trader import config  # load_dotenv
            from hibrit_trader.killswitch import notify
            notify(ozet)
            print("\nTelegram gonderildi.")
        except Exception as e:
            print(f"\nTelegram HATA: {e}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
