#!/usr/bin/env python3
"""Nightly saglik raporu — memory + disk + trend takip.

Her gece 04:30 UTC (log rotate sonrasi) systemd timer ile calisir.
Sunucu metrikleri toplar, data/saglik_gunluk.jsonl'a yazar (kolay JSON),
son 7 gunle trend karsilastir, telegram'a kisa ozet.

Kullanim:
    python scripts/saglik_rapor.py [--no-telegram]
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("/home/bot/yz/hybrid-trader-ori/data")
LOG_TREND = DATA / "saglik_gunluk.jsonl"


def sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _mem_mb(byte_str: str) -> int:
    try:
        return int(byte_str) // 1_048_576
    except (ValueError, TypeError):
        return 0


def olcum() -> dict:
    """Su anki metrikler."""
    now = datetime.now(timezone.utc)
    return {
        "ts": time.time(),
        "gun": now.strftime("%Y-%m-%d"),
        "ram_kullanilan_mb": int(sh("free -m | awk '/^Mem:/ {print $3}'") or 0),
        "ram_toplam_mb": int(sh("free -m | awk '/^Mem:/ {print $2}'") or 0),
        "load_1m": float(sh("cut -d ' ' -f1 /proc/loadavg") or 0),
        "disk_home_kullanilan_gb": float(sh("df -BG /home | awk 'NR==2 {print $3}' | tr -d G") or 0),
        "disk_home_toplam_gb": float(sh("df -BG /home | awk 'NR==2 {print $2}' | tr -d G") or 0),
        "disk_home_yuzde": int(sh("df /home | awk 'NR==2 {print $5}' | tr -d %") or 0),
        "servis_mem_mb": _mem_mb(sh("systemctl --user show momentum-trader --property=MemoryCurrent --value")),
        "data_dizin_mb": int(sh("du -sm /home/bot/yz/hybrid-trader-ori/data | cut -f1") or 0),
        "logs_dizin_mb": int(sh("du -sm /home/bot/yz/hybrid-trader-ori/logs | cut -f1") or 0),
        "trades_toplam": sum(int(sh(f"wc -l < /home/bot/yz/hybrid-trader-ori/data/{m}_trades.jsonl 2>/dev/null") or 0)
                             for m in ("v7", "v7c", "v7cd", "v7d", "v7hizli", "r1")),
        "uptime_gun": float(sh("awk '{print $1/86400}' /proc/uptime") or 0),
    }


def yukle_gecmis() -> list[dict]:
    if not LOG_TREND.exists():
        return []
    out = []
    for l in LOG_TREND.read_text().splitlines():
        if l.strip():
            try:
                out.append(json.loads(l))
            except Exception:
                pass
    return out


def yaz_gecmis(o: dict) -> None:
    LOG_TREND.parent.mkdir(parents=True, exist_ok=True)
    with LOG_TREND.open("a", encoding="utf-8") as f:
        f.write(json.dumps(o, ensure_ascii=False) + "\n")


def trend_ok(seri: list[float], son: float) -> str:
    """Basit trend: son 7 kaydin ortalamasina gore delta."""
    if len(seri) < 2:
        return "-"
    ort = sum(seri) / len(seri)
    delta = son - ort
    if abs(delta) < ort * 0.05:  # %5 icinde stabil
        return f"~stabil ({son:.1f})"
    ok = "↑" if delta > 0 else "↓"
    return f"{ok} {son:.1f} (ort {ort:.1f})"


def rapor(bugun: dict, gecmis: list[dict]) -> str:
    ana = [
        f"🩺 *Saglik {bugun['gun']}*",
        f"RAM     : {bugun['ram_kullanilan_mb']}/{bugun['ram_toplam_mb']} MB",
        f"Load 1m : {bugun['load_1m']:.2f}",
        f"Disk    : {bugun['disk_home_kullanilan_gb']:.0f}/{bugun['disk_home_toplam_gb']:.0f} GB (%{bugun['disk_home_yuzde']})",
        f"Servis  : {bugun['servis_mem_mb']} MB RAM",
        f"data/   : {bugun['data_dizin_mb']} MB",
        f"logs/   : {bugun['logs_dizin_mb']} MB",
        f"Trades  : {bugun['trades_toplam']} toplam",
        f"Uptime  : {bugun['uptime_gun']:.1f} gun",
    ]
    if gecmis:
        son7 = gecmis[-7:]
        ana.append("─ 7g trend ─")
        ana.append(f"servis_mb: {trend_ok([g['servis_mem_mb'] for g in son7], bugun['servis_mem_mb'])}")
        ana.append(f"data_mb  : {trend_ok([g['data_dizin_mb'] for g in son7], bugun['data_dizin_mb'])}")
        ana.append(f"trades   : {trend_ok([g['trades_toplam'] for g in son7], bugun['trades_toplam'])}")
        # Memory leak sinyali: servis RAM > 500MB VE 7g artis > %20 → uyari
        if son7 and bugun['servis_mem_mb'] > 500:
            eski = son7[0].get('servis_mem_mb', 0)
            if eski > 0 and (bugun['servis_mem_mb'] - eski) / eski > 0.2:
                ana.append(f"⚠️ RAM %{int((bugun['servis_mem_mb']-eski)/eski*100)} artis 7g'de (leak sinyali?)")
    return "\n".join(ana)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args()

    bugun = olcum()
    gecmis = yukle_gecmis()
    yaz_gecmis(bugun)
    ozet = rapor(bugun, gecmis)
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
