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
from .kaynak_rpc import WssAbone, http_get_json
from .musluk import Musluk
from .ortak import Bus, DurumOnbellek, Sayaclar
from .sayim_r2 import PROGRAMLAR, SayimR2
from .swap_r0 import SwapR0
from .yazici import OlayYazici


async def saglik(bus, sayac, nesne_sayimi=None):
    """5 dakikada bir oz-olcum: RSS, CPU, kuyruk, sayaclar.

    nesne_sayimi (25 Tem P0): RAM'de buyuyebilen yapilarin adetleri;
    RSS egimi teshisi bu satirlardan kanitla yapilir (tahmin degil)."""
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
        payload = {"rss_kb": rss, "cpu_user_s": round(t.user, 1),
                   "cpu_sys_s": round(t.system, 1),
                   "kuyruk": bus.q.qsize(),
                   "dusen_snapshot": sayac.dusen_snapshot,
                   "kind_sayi": dict(sayac.kind_sayi)}
        if nesne_sayimi is not None:
            try:
                payload["nesneler"] = nesne_sayimi()
            except Exception:  # noqa: BLE001
                pass
        await bus.yayinla("sistem", "ObserverHealth", payload, src="saglik")


async def calistir():
    veri = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
    kok = Path(os.getenv("GOZLEM_DATA_DIR", str(veri / "gozlem")))
    kok.mkdir(parents=True, exist_ok=True)
    wss = os.getenv("GOZLEM_WSS", "wss://api.mainnet-beta.solana.com")

    yazici = OlayYazici(kok)
    onbellek = DurumOnbellek()
    sayac = Sayaclar()
    bus = Bus(yazici, onbellek, sayac)

    async def snap_getir(tok: str):
        """Terfi yarisi acil cekimi: token bazli DexScreener sorgusu,
        en likit solana cifti ham haliyle doner."""
        y = await http_get_json(
            "https://api.dexscreener.com/latest/dex/tokens/" + tok,
            timeout=3)
        pl = [p for p in (y.get("pairs") or [])
              if p.get("chainId") == "solana"]
        if not pl:
            return None
        return max(pl, key=lambda x: float(
            (x.get("liquidity") or {}).get("usd") or 0))

    bus.karar = KararUretici(bus, onbellek, snap_getir=snap_getir)

    from .islem_akisi import IslemAkisi
    islem = IslemAkisi(bus, veri)
    sayim = SayimR2(bus, sayac, islem_kanca=islem.on_ham)
    sayim_ws = WssAbone(wss, bus, "ws-sayim", sayim.isle,
                        on_ham=sayim.on_ham)
    sayim_ws.abonelik_ayarla({p: et for p, et in PROGRAMLAR.items()})

    swap = SwapR0(bus, onbellek)
    swap_ws = WssAbone(wss, bus, "ws-swap", swap.isle)

    izlenen = IzlenenKume(bus, onbellek, veri, swap_abone=swap_ws)
    anlik = Anlik(bus, onbellek, sayac)
    musluk = Musluk(bus, veri, kok)
    from .konsantrasyon import Konsantrasyon
    konsantrasyon = Konsantrasyon(bus, onbellek)
    from .lp_kilit import LpKilit
    lp_kilit = LpKilit(bus, onbellek)
    from .erken_alici import ErkenAlici
    erken_alici = ErkenAlici(bus, onbellek, veri)

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
        asyncio.create_task(konsantrasyon.calis(), name="konsantrasyon"),
        asyncio.create_task(lp_kilit.calis(), name="lp_kilit"),
        asyncio.create_task(erken_alici.calis(), name="erken_alici"),
        asyncio.create_task(islem.worker(), name="islem_worker"),
        asyncio.create_task(islem.flush_dongusu(), name="islem_flush"),
        asyncio.create_task(saglik(bus, sayac, nesne_sayimi=lambda: {
            **islem.nesneler(),
            "son_snapshot": len(onbellek.son_snapshot),
            "izlenen": len(onbellek.izlenen),
            "ctx_uretilen": len(bus.karar._uretilen),
            "musluk_girisler": len(musluk.girisler),
            "musluk_ofset": len(musluk.ofset),
            "swap_pencere": len(swap._pencere),
            "kons_arz": len(konsantrasyon._arz),
            "alici_cache": len(erken_alici._cache),
            "alici_negatif": len(erken_alici._negatif),
            "alici_olculen": len(erken_alici._olculen),
            "lp_olculen": len(lp_kilit._olculen)}), name="saglik"),
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
