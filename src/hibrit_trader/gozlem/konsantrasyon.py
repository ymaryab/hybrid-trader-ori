"""Tier A sensor 1: holder konsantrasyonu (Sprint 2, 24 Tem).

Amac ALGORITMA degil BILGI DENEYI: karar aninda "bu tokenin sahiplik
yapisi koşucuyu tuzaktan ayiriyor mu" sorusunun verisini biriktirmek.
Izlenen (R0) her token icin 60 sn'de bir:
  getTokenLargestAccounts (ilk 20 hesap, ham miktarlar)
  + getTokenSupply (mint basina onbellekli)
-> HolderKonsantrasyon olayi (ham veri; pay hesaplari cevrimdisi yapilir,
   turev diske yazilmaz: omurga ilkesi).

RPC butcesi: ~izlenen_token istek/dk (supply onbellekli) — kamu RPC
icin kabul edilebilir. Hata halinde GapDetected(src=konsantrasyon).
"""

from __future__ import annotations

import asyncio
import os
import time

from .kaynak_rpc import http_rpc

PERIYOT_SN = float(os.getenv("GOZLEM_KONSANTRASYON_SN", "60"))


class Konsantrasyon:
    def __init__(self, bus, onbellek):
        self.bus = bus
        self.onbellek = onbellek
        self.rpc_url = os.getenv("GOZLEM_HTTP",
                                 "https://api.mainnet-beta.solana.com")
        self._arz: dict[str, tuple[float, dict]] = {}   # mint -> (ts, supply)

    async def _supply(self, mint: str):
        c = self._arz.get(mint)
        if c and time.time() - c[0] < 3600:
            return c[1]
        r = await http_rpc(self.rpc_url, "getTokenSupply", [mint], timeout=6)
        v = (r.get("result") or {}).get("value")
        if v:
            self._arz[mint] = (time.time(), v)
        return v

    async def calis(self):
        while True:
            bas = time.monotonic()
            tokenler = sorted({m["token"] for m in
                               self.onbellek.izlenen.values() if m.get("token")})
            for mint in tokenler:
                try:
                    r = await http_rpc(self.rpc_url,
                                       "getTokenLargestAccounts",
                                       [mint], timeout=6)
                    hesaplar = ((r.get("result") or {}).get("value")) or []
                    arz = await self._supply(mint)
                    self.bus.yayinla_kayipli(
                        "sensor", "HolderKonsantrasyon",
                        {"mint": mint,
                         "arz": arz,
                         "hesaplar": [
                             {"adres": h.get("address"),
                              "miktar": h.get("uiAmountString")
                                        or h.get("uiAmount")}
                             for h in hesaplar]},
                        token=mint, src="konsantrasyon")
                except Exception as e:  # noqa: BLE001
                    self.bus.yazici.yaz(
                        "sistem", "GapDetected",
                        {"src": "konsantrasyon", "neden": str(e)[:150]},
                        token=mint, src="konsantrasyon")
                await asyncio.sleep(1.0)   # RPC nezaketi: istekler yayilir
            gecen = time.monotonic() - bas
            await asyncio.sleep(max(2.0, PERIYOT_SN - gecen))
