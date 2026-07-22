"""R0 swap akisi: izlenen havuzlarin TUM islemleri, ham log payload'uyla.

Alis/satis siniflandirmasi yapilmaz (turev, cevrimdisi). Basarisiz
tx'ler de saklanir (err alaniyla): MEV/bot baskisi da bilgidir.
"""

from __future__ import annotations


class SwapR0:
    def __init__(self, bus, onbellek):
        self.bus = bus
        self.onbellek = onbellek

    async def isle(self, addr: str, etiket: str, params: dict) -> None:
        res = params.get("result") or {}
        val = res.get("value") or {}
        meta = self.onbellek.izlenen.get(addr) or {}
        await self.bus.yayinla(
            "swap", "SwapObserved",
            {"pool": addr, "err": val.get("err"),
             "logs": val.get("logs") or []},
            token=meta.get("token"), sig=val.get("signature"),
            slot=(res.get("context") or {}).get("slot"), src="ws:r0")
