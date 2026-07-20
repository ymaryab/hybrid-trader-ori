"""Sessiz motor kirilma uyarilari (18 Tem) — telegram + dedup.

5 motora ortak: KOR FIYAT, SATIS ERTELENDI, GIRIS IPTAL (canli alim gerceklesmedi)
gibi kritik ama sessiz kalan olaylari telegram'a bildir. 5dk dedup (spam engeli).

Kullanim:
    from hibrit_trader.uyari_notify import kritik_uyari
    kritik_uyari("KOR FIYAT", f"kor:{motor}:{pair}", f"{motor} {pair}: 120s fiyat yok")
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)
_LOCK = threading.Lock()
_son: dict[str, float] = {}
DEDUP_SEC = float(os.getenv("UYARI_DEDUP_SEC", "300"))


def kritik_uyari(baslik: str, dedup_key: str, mesaj: str) -> None:
    """Log CRITICAL + telegram notify. Dedup: aynı key DEDUP_SEC boyunca tekrar atmaz."""
    now = time.time()
    with _LOCK:
        if now - _son.get(dedup_key, 0.0) < DEDUP_SEC:
            return
        _son[dedup_key] = now
    log.critical("UYARI %s: %s", baslik, mesaj)
    try:
        from hibrit_trader.killswitch import notify
        notify(f"⚠️ {baslik}: {mesaj}")
    except Exception:
        log.warning("uyari_notify telegram gonderilemedi", exc_info=True)
