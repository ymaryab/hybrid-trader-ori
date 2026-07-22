"""R0 swap akisi: izlenen havuzlarin islemleri, ham log payload'uyla.

Alis/satis siniflandirmasi yapilmaz (turev, cevrimdisi). Basarisiz
tx'ler de saklanir (err alaniyla): MEV/bot baskisi da bilgidir.

DEBI VALFI (22 Tem, olcum sonrasi tasarim sapmasi): hiper-aktif
havuzlar (orn. trending mainstay'ler, 150+ tx/sn) tam sadakatle
saklanirsa disk gunde ~27GB dolar. Havuz basina 60 sn'de
GOZLEM_HAVUZ_TX_ESIK (vars. 120) mesaji asan havuz PULS moduna gecer:
10 sn'lik pencere basina SwapPulse (adet, hata adedi, ilk/son imza,
slot araligi). Mod gecisleri ThrottleModeChanged olayi uretir: bilgi
kaybi ACIK ve olculebilir, sessiz degil. Sakinlesince tam moda doner.
"""

from __future__ import annotations

import asyncio
import collections
import os
import time


class SwapR0:
    def __init__(self, bus, onbellek):
        self.bus = bus
        self.onbellek = onbellek
        self.esik = int(os.getenv("GOZLEM_HAVUZ_TX_ESIK", "120"))
        self._pencere: dict[str, collections.deque] = {}
        self._mod: dict[str, str] = {}          # pool -> tam|puls
        self._puls: dict[str, dict] = {}        # pool -> birikim

    def _oran(self, pool: str) -> int:
        dq = self._pencere.setdefault(pool, collections.deque())
        simdi = time.time()
        dq.append(simdi)
        esik = simdi - 60
        while dq and dq[0] < esik:
            dq.popleft()
        return len(dq)

    async def _mod_degistir(self, pool: str, yeni: str, oran: int) -> None:
        self._mod[pool] = yeni
        meta = self.onbellek.izlenen.get(pool) or {}
        await self.bus.yayinla(
            "sistem", "ThrottleModeChanged",
            {"pool": pool, "mod": yeni, "tx_60s": oran,
             "esik": self.esik}, token=meta.get("token"), src="ws:r0")

    async def isle(self, addr: str, etiket: str, params: dict) -> None:
        res = params.get("result") or {}
        val = res.get("value") or {}
        slot = (res.get("context") or {}).get("slot")
        oran = self._oran(addr)
        mod = self._mod.get(addr, "tam")
        if mod == "tam" and oran > self.esik:
            mod = "puls"
            await self._mod_degistir(addr, mod, oran)
        elif mod == "puls" and oran < self.esik // 2:
            mod = "tam"
            await self._mod_degistir(addr, mod, oran)
        if mod == "puls":
            b = self._puls.setdefault(addr, {
                "adet": 0, "err_adet": 0, "ilk_sig": val.get("signature"),
                "ilk_slot": slot, "bas_ts_ms": int(time.time() * 1000)})
            b["adet"] += 1
            if val.get("err") is not None:
                b["err_adet"] += 1
            if b.get("ilk_sig") is None:
                b["ilk_sig"] = val.get("signature")
                b["ilk_slot"] = slot
            b["son_sig"] = val.get("signature")
            b["son_slot"] = slot
            return
        meta = self.onbellek.izlenen.get(addr) or {}
        await self.bus.yayinla(
            "swap", "SwapObserved",
            {"pool": addr, "err": val.get("err"),
             "logs": val.get("logs") or []},
            token=meta.get("token"), sig=val.get("signature"),
            slot=slot, src="ws:r0")

    async def puls_dongusu(self):
        """10 sn'de bir puls birikimlerini bosalt; izlenmeyen havuzlarin
        pencerelerini temizle."""
        while True:
            await asyncio.sleep(10)
            for pool, b in list(self._puls.items()):
                if b["adet"] == 0:
                    continue
                meta = self.onbellek.izlenen.get(pool) or {}
                await self.bus.yayinla(
                    "swap", "SwapPulse", {"pool": pool, **b},
                    token=meta.get("token"), src="ws:r0")
                self._puls[pool] = {
                    "adet": 0, "err_adet": 0, "ilk_sig": None,
                    "ilk_slot": None,
                    "bas_ts_ms": int(time.time() * 1000)}
            izlenen = self.onbellek.izlenen
            for pool in list(self._pencere):
                if pool not in izlenen:
                    self._pencere.pop(pool, None)
                    self._mod.pop(pool, None)
                    self._puls.pop(pool, None)
