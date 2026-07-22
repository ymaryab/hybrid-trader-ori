"""Gozlemci ana sureci.

Calistirma: gozlem-venv python -m hibrit_trader.gozlem.ana
Env: MOMENTUM_DATA_DIR (motor verisi, varsayilan data),
     GOZLEM_DATA_DIR (olay deposu, varsayilan data/gozlem),
     GOZLEM_WSS, GOZLEM_HTTP, GOZLEM_R0_MAX.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from .anlik import Anlik
from .izlenen import IzlenenKume
from .karar import KararUretici
from .kaynak_rpc import WssAbone
from .musluk import Musluk
from .ortak import Bus, DurumOnbellek, Sayaclar
from .sayim_r2 import PROGRAMLAR, SayimR2
from .swap_r0 import SwapR0
from .yazici import OlayYazici


async def saglik(bus, sayac):
    """5 dakikada bir oz-olcum: RSS, CPU, kuyruk, sayaclar."""
    while True:
        await asyncio.sleep(300)
        rss = 0
        try:
            for ln in open("/proc/self/status"):
                if ln.startswith("VmRSS"):
                    rss = int(ln.split()[1])
                    break
        except OSError:
            pass
        t = os.times()
        await bus.yayinla(
            "sistem", "ObserverHealth",
            {"rss_kb": rss, "cpu_user_s": round(t.user, 1),
             "cpu_sys_s": round(t.system, 1),
             "kuyruk": bus.q.qsize(),
             "dusen_snapshot": sayac.dusen_snapshot,
             "kind_sayi": dict(sayac.kind_sayi)}, src="saglik")


async def calistir():
    veri = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    kok = Path(os.getenv("GOZLEM_DATA_DIR", str(veri / "gozlem")))
    kok.mkdir(parents=True, exist_ok=True)
    wss = os.getenv("GOZLEM_WSS", "wss://api.mainnet-beta.solana.com")

    yazici = OlayYazici(kok)
    onbellek = DurumOnbellek()
    sayac = Sayaclar()
    bus = Bus(yazici, onbellek, sayac)
    bus.karar = KararUretici(bus, onbellek)

    sayim = SayimR2(bus, sayac)
    sayim_ws = WssAbone(wss, bus, "ws-sayim", sayim.isle)
    sayim_ws.abonelik_ayarla({p: et for p, et in PROGRAMLAR.items()})

    swap = SwapR0(bus, onbellek)
    swap_ws = WssAbone(wss, bus, "ws-swap", swap.isle)

    izlenen = IzlenenKume(bus, onbellek, veri, swap_abone=swap_ws)
    anlik = Anlik(bus, onbellek, sayac)
    musluk = Musluk(bus, veri, kok)

    await bus.yayinla("sistem", "ObserverStarted",
                      {"pid": os.getpid(), "wss": wss,
                       "r0_tavan": izlenen.tavan,
                       "baslama_ts": time.time()}, src="ana")

    gorevler = [
        asyncio.create_task(bus.dagitici(), name="dagitici"),
        asyncio.create_task(sayim_ws.calis(), name="sayim_ws"),
        asyncio.create_task(sayim.puls(), name="sayim_puls"),
        asyncio.create_task(swap_ws.calis(), name="swap_ws"),
        asyncio.create_task(swap.puls_dongusu(), name="swap_puls"),
        asyncio.create_task(izlenen.calis(), name="izlenen"),
        asyncio.create_task(anlik.snapshot_dongusu(), name="snapshot"),
        asyncio.create_task(anlik.mctx_dongusu(), name="mctx"),
        asyncio.create_task(musluk.calis(), name="musluk"),
        asyncio.create_task(saglik(bus, sayac), name="saglik"),
    ]

    dur = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, dur.set)
    await dur.wait()
    for g in gorevler:
        g.cancel()
    await asyncio.gather(*gorevler, return_exceptions=True)
    # kuyrukta kalanlari bosalt
    while not bus.q.empty():
        akis, kind, payload, kw = bus.q.get_nowait()
        yazici.yaz(akis, kind, payload, **kw)
    yazici.kapat()


if __name__ == "__main__":
    asyncio.run(calistir())
