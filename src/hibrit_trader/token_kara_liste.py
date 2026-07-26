"""Rug-imza token kara listesi (26 Tem, kullanici karari: secenek B).

Herhangi bir defterde <= -%25 dolumla kapanan token TUM motorlara
(paper + CANLI) kalici olarak yasaklanir. Kapsam bilerek dar: rug
olmus (LP cekilmis) tokene yeniden giris kesilir; normal felaket
bandi (-15..-25) serbest kalir, karantina yok.

Iki kanca:
  yazma: paper.enrich_trade_from_position (tum modlar kapanista gecer)
  okuma: scanner.scan_all / scan_all_cached (tum motorlar adaylari
         buradan alir; yasakli token aday listesine hic girmez)

Liste data/token_kara_liste.json'da kalici; surec ici cache mtime ile
sicak yenilenir. Yazim atomik (tmp + rename). Acik pozisyonlarin
CIKISINA etkisi yok; yalniz yeni giris engellenir.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("hibrit.kara_liste")

RUG_ESIK_PCT = float(os.getenv("RUG_KARA_ESIK_PCT", "-25"))
DOSYA = Path("data/token_kara_liste.json")

_cache: dict = {"mtime": None, "tokenler": {}, "yol": None}


def _yukle(dosya: Path) -> dict:
    """{token: kayit} dondurur; mtime degismediyse cache'ten."""
    try:
        mt = dosya.stat().st_mtime
    except OSError:
        _cache.update(mtime=None, tokenler={}, yol=dosya)
        return {}
    if _cache["yol"] == dosya and _cache["mtime"] == mt:
        return _cache["tokenler"]
    try:
        veri = json.loads(dosya.read_text())
        tokenler = veri.get("tokenler") or {}
    except (OSError, ValueError):
        tokenler = {}
    _cache.update(mtime=mt, tokenler=tokenler, yol=dosya)
    return tokenler


def yasakli(token: str | None, dosya: Path | None = None) -> bool:
    if not token:
        return False
    return token in _yukle(dosya or DOSYA)


def ekle(token: str, *, pnl_pct: float = 0.0, pair: str = "",
         kaynak: str = "", dosya: Path | None = None) -> bool:
    """Tokeni kalici listeye ekler; zaten varsa dokunmaz (idempotent)."""
    if not token:
        return False
    dosya = dosya or DOSYA
    tokenler = dict(_yukle(dosya))
    if token in tokenler:
        return False
    tokenler[token] = {"ts": round(time.time(), 1), "pair": pair,
                       "pnl_pct": round(pnl_pct, 2), "kaynak": kaynak}
    dosya.parent.mkdir(parents=True, exist_ok=True)
    tmp = dosya.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sv": 1, "tokenler": tokenler},
                              ensure_ascii=False, indent=1))
    tmp.replace(dosya)
    _cache.update(mtime=None)          # bir sonraki okuma diskten
    log.warning("KARA LISTE: %s (%s) eklendi pnl=%.1f%% kaynak=%s",
                token[:12], pair, pnl_pct, kaynak)
    return True


def islem_kontrol(token: str | None, pnl_pct: float | None,
                  pair: str = "", kaynak: str = "") -> bool:
    """Kapanis kancasi: rug-imza esigini asan kayip tokeni yasaklar."""
    if not token or pnl_pct is None or pnl_pct > RUG_ESIK_PCT:
        return False
    try:
        return ekle(token, pnl_pct=pnl_pct, pair=pair, kaynak=kaynak)
    except Exception as e:  # noqa: BLE001 — kapanis yolunu asla dusurme
        log.warning("kara liste ekleme hatasi: %s", e)
        return False


def filtrele(pairs: list, kaynak: str = "tarama") -> list:
    """Giris kancasi: aday listesinden yasakli tokenleri eler."""
    try:
        tokenler = _yukle(DOSYA)
        if not tokenler:
            return pairs
        temiz = [p for p in pairs
                 if getattr(p, "token_address", None) not in tokenler]
        n = len(pairs) - len(temiz)
        if n:
            log.warning("KARA LISTE: %d aday elendi (%s)", n, kaynak)
        return temiz
    except Exception as e:  # noqa: BLE001 — tarama yolunu asla dusurme
        log.warning("kara liste filtre hatasi: %s", e)
        return pairs
