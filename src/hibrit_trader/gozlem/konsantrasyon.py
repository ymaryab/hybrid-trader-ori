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

PERIYOT_SN = float(os.getenv("GOZLEM_KONSANTRASYON_SN", "180"))
# 25 Tem: kamu ana RPC getTokenLargestAccounts'i 429'luyor; rotasyonlu
# kamu uc listesi (rpc_multi ile ayni aday havuzu)
URLS = [u.strip() for u in os.getenv(
    "GOZLEM_KONS_RPC",
    "https://solana-rpc.publicnode.com,"
    "https://solana.drpc.org,"
    "https://rpc.ankr.com/solana,"
    "https://api.mainnet-beta.solana.com").split(",") if u.strip()]


class Konsantrasyon:
    def __init__(self, bus, onbellek):
        self.bus = bus
        self.onbellek = onbellek
        self._url_ix = 0
        self._arz: dict[str, tuple[float, dict]] = {}   # mint -> (ts, supply)

    async def _rpc(self, method, params):
        from .kaynak_rpc import http_rpc
        for _ in range(len(URLS)):
            url = URLS[self._url_ix % len(URLS)]
            try:
                r = await http_rpc(url, method, params, timeout=8)
                if r.get("result") is not None:
                    return r
                raise RuntimeError(str(r.get("error"))[:80])
            except Exception:
                self._url_ix += 1     # 429/hata: siradaki uca gec
        raise RuntimeError("tum RPC uclari basarisiz")

    async def _supply(self, mint: str):
        c = self._arz.get(mint)
        if c and time.time() - c[0] < 3600:
            return c[1]
        r = await self._rpc("getTokenSupply", [mint])
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
                    r = await self._rpc("getTokenLargestAccounts",
                                        [mint])
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
                await asyncio.sleep(2.0)   # RPC nezaketi: istekler yayilir
            gecen = time.monotonic() - bas
            await asyncio.sleep(max(2.0, PERIYOT_SN - gecen))
