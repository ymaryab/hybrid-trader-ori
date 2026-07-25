"""Tier A sensor 2: LP kilit/burn fotografi (Sprint 2, 25 Tem).

Butce satiri docs/sprint2_rpc_butcesi.md'de ONCE eklendi (politika).
Havuz basina TEK olcum (kalici cache), gunluk TAVAN 600 istek; asim
Throttled olayiyla SAYILARAK birakilir.

v0 kapsami (durust):
- Havuz sahibi PumpSwap programi ise: "pumpswap" etiketi (protokol
  havuzu; LP kullanici elinde degil) — ek cagri yok (1 kredi).
- Raydium AMM v4 ise: hesap verisinden base/quote/lp mint ofsetle
  cikarilir (400/432/464). OZ-DENETIM: cikan base/quote, izlenen token
  veya WSOL ile eslesmiyorsa parse GUVENSIZ sayilir, supply cagrilari
  atlanir ve olay guvensiz bayragiyla yazilir (yanlis veri yazilmaz).
  Eslesirse lp arzi + lp en buyuk hesaplar alinir (yakma/kilit analizi
  cevrimdisi yapilir: ham veri ilkesi).
- Diger sahipler: "bilinmeyen_amm" etiketi, ham owner kaydi.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time

from .konsantrasyon import URLS

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAYDIUM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
WSOL = "So11111111111111111111111111111111111111112"
GUNLUK_TAVAN = int(os.getenv("GOZLEM_LP_TAVAN", "600"))

_ABC = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _ABC[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\\0"))) + s


def v4_mintler(ham: bytes) -> dict:
    """Raydium v4 layout ofsetleri: base 400, quote 432, lp 464."""
    if len(ham) < 496:
        return {}
    return {"base": _b58(ham[400:432]), "quote": _b58(ham[432:464]),
            "lp": _b58(ham[464:496])}


class LpKilit:
    def __init__(self, bus, onbellek):
        self.bus = bus
        self.onbellek = onbellek
        self._olculen: set[str] = set()      # pool -> kalici cache
        self._gun = ""
        self._gun_istek = 0
        self._url_ix = 0

    async def _rpc(self, method, params):
        from .kaynak_rpc import http_rpc
        gun = time.strftime("%Y-%m-%d", time.gmtime())
        if gun != self._gun:
            self._gun, self._gun_istek = gun, 0
        if self._gun_istek >= GUNLUK_TAVAN:
            raise RuntimeError("gunluk_tavan")
        self._gun_istek += 1
        for _ in range(len(URLS)):
            url = URLS[self._url_ix % len(URLS)]
            try:
                r = await http_rpc(url, method, params, timeout=8)
                if r.get("result") is not None:
                    return r
                raise RuntimeError(str(r.get("error"))[:80])
            except Exception:
                self._url_ix += 1
        raise RuntimeError("rpc_basarisiz")

    async def calis(self):
        backoff = 0.0
        while True:
            await asyncio.sleep(max(5.0, backoff))
            aday = None
            for pool, meta in sorted(self.onbellek.izlenen.items()):
                if pool not in self._olculen:
                    aday = (pool, meta)
                    break
            if aday is None:
                continue
            pool, meta = aday
            try:
                r = await self._rpc("getAccountInfo",
                                    [pool, {"encoding": "base64"}])
                v = (r.get("result") or {}).get("value") or {}
                owner = v.get("owner")
                payload = {"pool": pool, "owner": owner}
                if owner == PUMPSWAP:
                    payload["amm"] = "pumpswap"
                elif owner == RAYDIUM_V4:
                    payload["amm"] = "raydium_v4"
                    ham = base64.b64decode((v.get("data") or ["", ""])[0])
                    m = v4_mintler(ham)
                    beklenen = meta.get("token")
                    guvenli = bool(m) and (
                        beklenen in (m.get("base"), m.get("quote"))
                        or WSOL in (m.get("base"), m.get("quote")))
                    payload["mintler"] = m
                    payload["parse_guvenli"] = guvenli
                    if guvenli and m.get("lp"):
                        arz = await self._rpc("getTokenSupply", [m["lp"]])
                        buyuk = await self._rpc("getTokenLargestAccounts",
                                                [m["lp"]])
                        payload["lp_arz"] = (arz.get("result") or {}).get("value")
                        payload["lp_hesaplar"] = [
                            {"adres": h.get("address"),
                             "miktar": h.get("uiAmountString")
                                       or h.get("uiAmount")}
                            for h in ((buyuk.get("result") or {}).get("value")
                                      or [])]
                else:
                    payload["amm"] = "bilinmeyen_amm"
                self.bus.yayinla_kayipli(
                    "sensor", "LPKilit", payload,
                    token=meta.get("token"), src="lp_kilit")
                self._olculen.add(pool)
                backoff = 0.0
            except Exception as e:  # noqa: BLE001
                neden = str(e)[:120]
                if neden == "gunluk_tavan":
                    self.bus.yazici.yaz(
                        "sistem", "Throttled",
                        {"src": "lp_kilit", "neden": "gunluk_tavan",
                         "tavan": GUNLUK_TAVAN}, src="lp_kilit")
                    backoff = 3600.0
                else:
                    backoff = min(max(60.0, backoff * 2), 960.0)
                    self.bus.yazici.yaz(
                        "sistem", "GapDetected",
                        {"src": "lp_kilit", "neden": neden,
                         "backoff_sn": backoff},
                        token=meta.get("token"), src="lp_kilit")
