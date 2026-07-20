"""Aday paylastirma (portfoy diversifikasyon) — 18 Tem.

Motorlar arasi ayni token'a coklu giris engeli. Motor bir token aliyorsa
data/motor_alinan.json'a kayit yazar. Diger motorlar _enter'de bu dosyayi
okuyup ayni token'i (COOLDOWN_SEC boyunca) atlar.

Ortak dosya format:
  {"MINT_ADDRESS": {"motor": "v7c", "ts": 1784500000.0, "pair": "febu / SOL"}}

Env:
  PAYLASTIR_ENABLED  (default "1"; "0" ise devre disi - eski davranis)
  PAYLASTIR_COOLDOWN_SEC (default 900 = 15dk)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK = threading.Lock()

def _path() -> Path:
    return Path(os.getenv("MOMENTUM_DATA_DIR", "data")) / "motor_alinan.json"


def _load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save(d: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def _cooldown() -> float:
    return float(os.getenv("PAYLASTIR_COOLDOWN_SEC", "900"))


def _aktif() -> bool:
    return os.getenv("PAYLASTIR_ENABLED", "1") != "0"


def diger_motor_aldi_mi(token_address: str, benim_motor: str) -> tuple[bool, str]:
    """DEPRECATED (18 Tem): race var - iddia_et() kullanin."""
    if not _aktif():
        return False, ""
    now = time.time()
    with _LOCK:
        d = _load()
        rec = d.get(token_address)
        if not rec:
            return False, ""
        motor = str(rec.get("motor", "")).lower()
        ts = float(rec.get("ts", 0))
        if now - ts > _cooldown():
            return False, ""
        if motor == benim_motor.lower():
            return False, ""
        return True, f"paylastir_red:{motor}"


def iddia_et(token_address: str, benim_motor: str, pair: str) -> tuple[bool, str]:
    """ATOMIC check+reservation (18 Tem race condition fix).

    Return: (izin_var_mi, red_nedeni).
    - izin_var=True: BASARILI - motor bu token'i aldi, hemen kaydedildi.
      Motor asagi devam etmeli (open_position). Fail olursa iddiayi_bosalt() cagir.
    - izin_var=False: RED - baska motor bu token'i kilitledi (izin_yok = red_nedeni).
    """
    if not _aktif():
        return True, ""
    now = time.time()
    with _LOCK:
        d = _load()
        rec = d.get(token_address)
        if rec:
            motor = str(rec.get("motor", "")).lower()
            ts = float(rec.get("ts", 0))
            if now - ts <= _cooldown() and motor != benim_motor.lower():
                return False, f"paylastir_red:{motor}"
        # Iddia et - hemen kaydet (baska motor race'de yakalasin)
        cutoff = now - _cooldown() * 2
        d = {k: v for k, v in d.items() if float(v.get("ts", 0)) >= cutoff}
        d[token_address] = {"motor": benim_motor.lower(), "ts": now, "pair": pair}
        _save(d)
        return True, ""


def iddiayi_bosalt(token_address: str, benim_motor: str) -> None:
    """Basarisiz alim sonrasi iddiayi kaldir (baska motor alsin diye)."""
    if not _aktif():
        return
    with _LOCK:
        d = _load()
        rec = d.get(token_address)
        if rec and str(rec.get("motor", "")).lower() == benim_motor.lower():
            del d[token_address]
            _save(d)


def kaydet(token_address: str, motor: str, pair: str) -> None:
    """DEPRECATED (18 Tem): iddia_et() zaten kayit yapar. Ancak eski cagrilar
    icin no-op olarak korunur (idempotent)."""
    # Eskiden burada kayit yapiliyordu. Simdi iddia_et zaten kaydetti.
    # Bu fonksiyon eski _open_position son satirlarindaki cagrilar icin duruyor.
    pass


def durum_ozet() -> dict:
    """Panel/log icin: son N dakika icinde alinmis tokenler."""
    now = time.time()
    with _LOCK:
        d = _load()
    aktif = {k: v for k, v in d.items() if now - float(v.get("ts", 0)) < _cooldown()}
    return {
        "aktif": _aktif(),
        "cooldown_sec": _cooldown(),
        "aktif_kayit_sayisi": len(aktif),
        "kayitlar": aktif,
    }
