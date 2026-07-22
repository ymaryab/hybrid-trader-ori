"""R2 piyasa sayimi: lansman ve havuz dogumlari.

Program firehose'unun tamami diske SIGMAZ (tasarim: R2 = sadece dogum
olaylari). Ham mesajlar ucuz substring on-filtresinden gecer; eslesen
olaylar TAM payload ile saklanir, eslesmeyenler SAYILIR (CensusPulse),
yani hicbir bilgi izsiz kaybolmaz: akis hacmi her dakika rapor edilir.
"""

from __future__ import annotations

import asyncio
import time

PROGRAMLAR = {
    # pump.fun bonding curve programi: token dogumu
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pumpfun",
    # PumpSwap AMM: mezuniyet havuzlari
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    # Raydium AMM v4: klasik havuz dogumu
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium",
}

# etiket -> (aranan log imzasi, uretilecek olay tipi)
IMZALAR = {
    "pumpfun": [("Instruction: Create", "LaunchObserved"),
                ("Instruction: Migrate", "GraduationObserved")],
    "pumpswap": [("Instruction: CreatePool", "PoolCreated")],
    "raydium": [("initialize2", "PoolCreated"),
                ("Instruction: Initialize2", "PoolCreated")],
}


class SayimR2:
    def __init__(self, bus, sayac):
        self.bus = bus
        self.sayac = sayac
        self._puls_gorev = None

    async def isle(self, addr: str, etiket: str, params: dict) -> None:
        self.sayac.r2_ham_mesaj += 1
        val = (params.get("result") or {}).get("value") or {}
        logs = val.get("logs") or []
        if val.get("err") is not None:
            return
        metin = "\n".join(logs)
        for imza, kind in IMZALAR.get(etiket, []):
            if imza in metin:
                slot = ((params.get("result") or {}).get("context")
                        or {}).get("slot")
                await self.bus.yayinla(
                    "sayim", kind,
                    {"program": etiket, "logs": logs},
                    sig=val.get("signature"), slot=slot, src=f"ws:{etiket}")
                if kind == "LaunchObserved":
                    self.sayac.lansman_ts.append(time.time())
                elif kind == "PoolCreated":
                    self.sayac.havuz_ts.append(time.time())
                break

    async def puls(self):
        """Dakikalik sayim nabzi: filtrelenen hacim dahil hicbir sey
        izsiz kalmaz."""
        onceki_ham = 0
        while True:
            await asyncio.sleep(60)
            ham = self.sayac.r2_ham_mesaj
            await self.bus.yayinla(
                "sayim", "CensusPulse",
                {"ham_mesaj_60s": ham - onceki_ham,
                 "lansman_1h": self.sayac.pencere_say(self.sayac.lansman_ts),
                 "havuz_1h": self.sayac.pencere_say(self.sayac.havuz_ts)},
                src="sayim")
            onceki_ham = ham
