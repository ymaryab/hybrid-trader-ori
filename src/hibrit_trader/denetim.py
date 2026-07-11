"""Denetim katmani: aylik kapali islem defteri (CSV).

- defter_yaz("YYYY-MM"): tum *_trades.jsonl + trades.jsonl dosyalarindan
  o ay kapanan islemleri data/denetim/YYYY-MM_defter.csv'ye yazar.
- run_forever(): panel.py daemon-thread deseni; ay devrinde onceki ayin
  defteri eksikse otomatik yazar (idempotent, restart guvenli).
- Manuel: .venv/bin/python -m hibrit_trader.denetim [YYYY-MM]

Kolonlar: tarih, motor, cift, giris_fiyati, cikis_fiyati, miktar,
pnl_usd, tx_imzasi. Paper islemlerde tx imzasi bos kalir; canli
islemlerde satirdaki "signature" alani okunur.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CHECK_SEC = float(os.getenv("DENETIM_CHECK_SEC", "21600"))  # 6 saatte bir ay devri kontrolu

KOLONLAR = [
    "tarih", "motor", "cift", "giris_fiyati", "cikis_fiyati",
    "miktar", "pnl_usd", "tx_imzasi",
]


def _data_dir() -> Path:
    return Path(os.getenv("MOMENTUM_DATA_DIR", "data"))


def _denetim_dir() -> Path:
    d = _data_dir() / "denetim"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trade_dosyalari(data_dir: Path) -> list[Path]:
    dosyalar = sorted(data_dir.glob("*_trades.jsonl"))
    ana = data_dir / "trades.jsonl"
    if ana.exists():
        dosyalar.append(ana)
    return dosyalar


def _motor_adi(p: Path) -> str:
    if p.name == "trades.jsonl":
        return "ana"
    return p.name[: -len("_trades.jsonl")]


def _satir(motor: str, row: dict) -> dict:
    tarih = row.get("closed_at") or row.get("ts_iso") or ""
    miktar: float | str = ""
    try:
        cost = float(row.get("cost_usd") or 0)
        giris = float(row.get("entry_price") or 0)
        if cost > 0 and giris > 0:
            miktar = round(cost / giris, 6)  # token miktari (satirda alan yok, turetilir)
    except (TypeError, ValueError):
        miktar = ""
    return {
        "tarih": tarih,
        "motor": motor,
        "cift": row.get("pair") or row.get("pair_name") or "",
        "giris_fiyati": row.get("entry_price", ""),
        "cikis_fiyati": row.get("exit_price", ""),
        "miktar": miktar,
        "pnl_usd": row.get("pnl_usd", ""),
        "tx_imzasi": row.get("signature") or "",
    }


def defter_yaz(ay: str) -> Path:
    """Ay (YYYY-MM) icinde kapanan tum islemleri CSV'ye yazar, yolu doner."""
    if not re.fullmatch(r"\d{4}-\d{2}", ay):
        raise ValueError(f"ay formati YYYY-MM olmali: {ay!r}")
    data_dir = _data_dir()
    satirlar: list[dict] = []
    for dosya in _trade_dosyalari(data_dir):
        motor = _motor_adi(dosya)
        for ham in dosya.read_text().splitlines():
            ham = ham.strip()
            if not ham:
                continue
            try:
                row = json.loads(ham)
            except json.JSONDecodeError:
                continue
            s = _satir(motor, row)
            if str(s["tarih"]).startswith(ay):
                satirlar.append(s)
    satirlar.sort(key=lambda s: str(s["tarih"]))
    hedef = _denetim_dir() / f"{ay}_defter.csv"
    with hedef.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=KOLONLAR)
        w.writeheader()
        w.writerows(satirlar)
    return hedef


def _onceki_ay(now: datetime) -> str:
    return (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def ay_devri_kontrol(now: datetime | None = None) -> Path | None:
    """Onceki ayin defteri eksikse yazar; varsa None doner (idempotent)."""
    now = now or datetime.now(timezone.utc)
    ay = _onceki_ay(now)
    if (_denetim_dir() / f"{ay}_defter.csv").exists():
        return None
    return defter_yaz(ay)


def run_forever() -> None:
    log.info("DENETIM: aylik defter dongusu basladi (kontrol %.0fs)", CHECK_SEC)
    while True:
        try:
            yol = ay_devri_kontrol()
            if yol is not None:
                log.info("DENETIM: aylik defter yazildi: %s", yol)
        except Exception:
            log.exception("DENETIM: defter yazimi hatasi")
        time.sleep(CHECK_SEC)


if __name__ == "__main__":
    import sys

    _ay = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m")
    print(defter_yaz(_ay))
