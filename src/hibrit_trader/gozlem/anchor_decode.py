"""Ortak anchor decode cekirdegi (K1+K3, blueprint bolum 2 + 11).

TEK cekirdek, iki tuketici: cevrimici trade gorevi (K1) ve cevrimdisi
retro decoder (K3). Layout bilgisi KODA GOMULMEZ; kesifle uretilen
surumlu kayit dosyasindan okunur (data/gozlem/anchor_kayit.json):
layout degisirse yeni kayit YENI surumle eklenir, eski arsiv asla
donusturulmez (Madde 11).

Kayit formati (sv=1):
  {"sv": 1, "kesif_ts": ..., "kayitlar": {
     "<disc_hex>": {"tur": "pumpfun_trade"|"pumpswap_buy"|...,
                     "mint_ofs": int, "sol_ofs": int|null,
                     "token_ofs": int|null, "user_ofs": int|null,
                     "is_buy": true|false|null, "boy_min": int}}}

Dogrulama (yapisal oz-denetim, lp_kilit deseni): mint 'pump' soneki
VEYA beklenen mint ile eslesme; sol_amount makulluk bandi. Gecemeyen
None doner; SAYMAK cagiranin isidir (sessiz atlama yasak).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from .lp_kilit import _b58

SCHEMA_VERSION = 1
SOL_MIN_LAMPORT = 1_000            # 0.000001 SOL
SOL_MAX_LAMPORT = 2_000 * 10**9    # 2000 SOL/islem ustu makul degil


def _u64(ham: bytes, ofs: int) -> int:
    return int.from_bytes(ham[ofs:ofs + 8], "little")


def kayit_yukle(veri: Path) -> dict:
    try:
        k = json.loads((Path(veri) / "gozlem" /
                        "anchor_kayit.json").read_text())
    except (OSError, ValueError):
        return {"sv": SCHEMA_VERSION, "kayitlar": {}}
    return k


class AnchorDecoder:
    def __init__(self, kayit: dict):
        self.kayitlar = kayit.get("kayitlar") or {}
        self.kayit_sv = kayit.get("sv")

    def coz(self, ham: bytes, beklenen_mint: str | None = None,
            beklenen_pool: str | None = None) -> dict | None:
        """Tek 'Program data' payload'unu coz. None = taninmadi/gecersiz.

        Kimlik iki turlu olabilir (kayit belirler): mint_ofs (pump.fun
        TradeEvent: mint gomulu) veya pool_ofs (PumpSwap eventleri:
        havuz gomulu; mint zarf/harita uzerinden gelir)."""
        if len(ham) < 8:
            return None
        disc = ham[:8].hex()
        k = self.kayitlar.get(disc)
        if k is None or len(ham) < k.get("boy_min", 0):
            return None
        mint = None
        if k.get("pool_ofs") is not None:
            pool = _b58(ham[k["pool_ofs"]:k["pool_ofs"] + 32])
            if beklenen_pool is not None and pool != beklenen_pool:
                return None
            mint = beklenen_mint          # zarf kimligi devralinir
        else:
            mint = _b58(ham[k["mint_ofs"]:k["mint_ofs"] + 32])
            if beklenen_mint is not None:
                if mint != beklenen_mint:
                    return None
            elif not mint.endswith("pump"):
                return None
        sol = None
        if k.get("sol_ofs") is not None:
            sol = _u64(ham, k["sol_ofs"])
            if not (SOL_MIN_LAMPORT <= sol <= SOL_MAX_LAMPORT):
                return None
        cikti = {"sv": SCHEMA_VERSION, "tur": k["tur"], "disc": disc,
                 "mint": mint, "sol_lamport": sol,
                 "token_miktar": (_u64(ham, k["token_ofs"])
                                  if k.get("token_ofs") is not None
                                  else None),
                 "is_buy": k.get("is_buy"),
                 "user": (_b58(ham[k["user_ofs"]:k["user_ofs"] + 32])
                          if k.get("user_ofs") is not None else None)}
        return cikti


def veri_payloadlari(logs: list[str]):
    """Log satirlarindan 'Program data:' payload'larini uret (bytes)."""
    for ln in logs:
        if not ln.startswith("Program data: "):
            continue
        try:
            yield base64.b64decode(ln[14:])
        except Exception:  # noqa: BLE001
            continue
