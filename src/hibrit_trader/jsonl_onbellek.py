"""Append-only jsonl dosyalari icin artimli okuma onbellegi (24 Tem).

Panel ve otonom secici ayni dosyalari her cagrida bastan ayristiriyordu
(v7_equity 6.5MB vb.): /api/filo soguk 4.6sn. Dosyalar yalniz sona ekleme
aldigindan bir kez ayristirip sonraki cagrilarda yalniz yeni baytlari
okumak yeterli. Kesilme/reset (boyut kuculmesi) bastan okumaya doner.

Iki hazir okuyucu: equity_satirlari (ts, eq) ve islem_satirlari
(ts, pnl_usd, gecerli, trade_id). Degerler degistirilmez: ayni girdi
ayni cikti (determinizm korunur), yalniz ayrisitirma maliyeti duser.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

_KILIT = threading.Lock()
_ONBELLEK: dict[tuple[str, str], dict] = {}


def _artimli(yol: Path, tur: str, ayristir) -> list:
    try:
        boy = yol.stat().st_size
    except OSError:
        return []
    anah = (str(yol), tur)
    with _KILIT:
        c = _ONBELLEK.get(anah)
        if c is None or boy < c["ofs"]:
            c = {"ofs": 0, "rows": []}
            _ONBELLEK[anah] = c
        if boy > c["ofs"]:
            with open(yol, "rb") as f:
                f.seek(c["ofs"])
                ham = f.read()
            kes = ham.rfind(b"\n")
            if kes >= 0:
                for ln in ham[:kes].splitlines():
                    if not ln.strip():
                        continue
                    try:
                        r = ayristir(json.loads(ln))
                    except Exception:
                        continue
                    if r is not None:
                        c["rows"].append(r)
                c["ofs"] += kes + 1
        return c["rows"]


def equity_satirlari(yol: Path) -> list[tuple[float, float]]:
    """[(ts, eq)] sirali (dosya sirasi = zaman sirasi)."""
    return _artimli(yol, "eq", lambda d: (float(d["ts"]), float(d["eq"])))


def islem_satirlari(yol: Path) -> list[tuple[float, float, bool, str]]:
    """[(ts, pnl_usd, gecerli, trade_id)]; gecerli=False: tip satiri veya
    manuel kapanis (kayan hesaba girmez)."""
    def ay(d):
        gecerli = not d.get("type") and d.get("exit_reason") != "manuel_kapanis"
        return (float(d.get("ts") or 0), float(d.get("pnl_usd") or 0),
                gecerli, str(d.get("trade_id") or ""))
    return _artimli(yol, "tr", ay)
