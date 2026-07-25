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

# etiket -> (aranan log imzasi, uretilecek olay tipi, eslesme modu)
# mod "son": satir SONU tam eslesme (25 Tem fixi: "Instruction: Create"
# alt-dizesi "Instruction: CreateTokenAccount" iceren ALIM tx'lerini de
# yakaliyordu -> sahte LaunchObserved, sayim ~%60 sisikti; tx-fallback
# teshisiyle bulundu). mod "icinde": alt-dize (raydium log formati argumanli).
IMZALAR = {
    "pumpfun": [("Instruction: Create", "LaunchObserved", "son"),
                ("Instruction: Migrate", "GraduationObserved", "son")],
    "pumpswap": [("Instruction: CreatePool", "PoolCreated", "son")],
    "raydium": [("initialize2", "PoolCreated", "icinde"),
                ("Instruction: Initialize2", "PoolCreated", "son")],
}


_HAM_IMZALAR = ("Instruction: Create", "Instruction: Migrate",
                "initialize2", "Initialize2", "CreatePool")


class SayimR2:
    def __init__(self, bus, sayac):
        self.bus = bus
        self.sayac = sayac
        self._puls_gorev = None

    def on_ham(self, ham: str) -> bool:
        """json.loads oncesi ucuz metin filtresi: firehose'un tamamini
        ayristirmadan sayar, sadece dogum/mezuniyet adaylarini gecirir.
        Kisa mesajlar (abonelik onaylari) her zaman gecer."""
        if len(ham) < 300:
            return True
        self.sayac.r2_ham_mesaj += 1
        return any(imza in ham for imza in _HAM_IMZALAR)

    async def isle(self, addr: str, etiket: str, params: dict) -> None:
        val = (params.get("result") or {}).get("value") or {}
        logs = val.get("logs") or []
        if val.get("err") is not None:
            return
        metin = "\n".join(logs)
        for imza, kind, mod in IMZALAR.get(etiket, []):
            eslesme = (any(ln.rstrip().endswith(imza) for ln in logs)
                       if mod == "son" else imza in metin)
            if eslesme:
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
