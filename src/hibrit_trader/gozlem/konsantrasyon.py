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
from pathlib import Path

PERIYOT_SN = float(os.getenv("GOZLEM_KONSANTRASYON_SN", "180"))
# 25 Tem: kamu ana RPC getTokenLargestAccounts'i 429'luyor; rotasyonlu
# kamu uc listesi (rpc_multi ile ayni aday havuzu)
def _env_rpc():
    """.env'deki SOLANA_RPC_URL doluysa (anahtarli uc) onu one koy."""
    try:
        for ln in open(Path(__file__).resolve().parents[3] / ".env"):
            if ln.startswith("SOLANA_RPC_URL=") and ln.strip() != "SOLANA_RPC_URL=":
                return [ln.split("=", 1)[1].strip()]
    except OSError:
        pass
    return []


URLS = _env_rpc() + [u.strip() for u in os.getenv(
    "GOZLEM_KONS_RPC",
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
        if c and time.time() - c[0] < 86400:   # arz nadiren degisir: 24h cache (kredi tasarrufu)
            return c[1]
        r = await self._rpc("getTokenSupply", [mint])
        v = (r.get("result") or {}).get("value")
        if v:
            self._arz[mint] = (time.time(), v)
        return v

    async def calis(self):
        """Az-ve-oz mod (25 Tem, 429/403 gercegi): oncelik TERFI ANI
        olcumu (karar-ani bilgisi icin kritik olan ilk fotograf), sonra
        token basina 15dk'da bir tazeleme. Istekler >=10 sn arayla;
        arka arkaya hata olursa ustel geri cekilme (60->960 sn)."""
        son_olcum: dict[str, float] = {}
        backoff = 0.0
        while True:
            await asyncio.sleep(max(10.0, backoff))
            simdi = time.time()
            tokenler = {m["token"] for m in
                        self.onbellek.izlenen.values() if m.get("token")}
            aday = None
            for mint in sorted(tokenler):
                if mint not in son_olcum:          # terfi ani: oncelik
                    aday = mint
                    break
            if aday is None:
                for mint in sorted(tokenler, key=lambda m: son_olcum.get(m, 0)):
                    if simdi - son_olcum.get(mint, 0) >= PERIYOT_SN * 5:
                        aday = mint
                        break
            if aday is None:
                continue
            try:
                r = await self._rpc("getTokenLargestAccounts", [aday])
                hesaplar = ((r.get("result") or {}).get("value")) or []
                arz = await self._supply(aday)
                self.bus.yayinla_kayipli(
                    "sensor", "HolderKonsantrasyon",
                    {"mint": aday, "arz": arz,
                     "hesaplar": [{"adres": h.get("address"),
                                   "miktar": h.get("uiAmountString")
                                             or h.get("uiAmount")}
                                  for h in hesaplar]},
                    token=aday, src="konsantrasyon")
                son_olcum[aday] = simdi
                backoff = 0.0
            except Exception as e:  # noqa: BLE001
                backoff = min(max(60.0, backoff * 2), 960.0)
                self.bus.yazici.yaz(
                    "sistem", "GapDetected",
                    {"src": "konsantrasyon", "neden": str(e)[:120],
                     "backoff_sn": backoff},
                    token=aday, src="konsantrasyon")
            for mint in list(son_olcum):
                if mint not in tokenler:
                    son_olcum.pop(mint, None)
