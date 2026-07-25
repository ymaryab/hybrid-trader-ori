"""Bus, durum onbellegi ve sayaclar.

Bus: tek kuyruk, tek dagitici. Dagitici sirasi: onbellek guncelle ->
karar uretici -> diske yaz. R0/R2/musluk olaylari kuyruk doluysa BEKLER
(kayip yok); snapshot uretici doluysa cevrim atlar ve Throttled sayar.
"""

from __future__ import annotations

import asyncio
import collections
import time


class DurumOnbellek:
    """Karar ani baglami icin RAM'deki son bilinen durum."""

    def __init__(self):
        self.son_snapshot: dict[str, dict] = {}   # token -> Snapshot olayi
        self.son_mctx: dict | None = None          # son MarketContext olayi
        self.izlenen: dict[str, dict] = {}         # pool -> {token, neden}

    def guncelle(self, ev: dict) -> None:
        k = ev.get("kind")
        if k == "Snapshot" and ev.get("token"):
            self.son_snapshot[ev["token"]] = ev
        elif k == "MarketContext":
            self.son_mctx = ev

    def buda(self, tokenler: set[str]) -> int:
        """RSS budamasi (25 Tem P0): izlenmeyen tokenlerin snapshot'lari
        atilir. Snapshot yalniz izlenenler icin uretilir; demote sonrasi
        kayit zaten bayatlar, ctx bayat/acil_cekim bayraklari gercegi
        soyler. Atilan adet doner (ObserverHealth'e raporlanir)."""
        atil = [t for t in self.son_snapshot if t not in tokenler]
        for t in atil:
            self.son_snapshot.pop(t, None)
        return len(atil)


class Sayaclar:
    """Sansursuz sayim: saklanmayan ham mesajlar bile SAYILIR."""

    def __init__(self):
        self.kind_sayi: collections.Counter = collections.Counter()
        self.dusen_snapshot = 0
        # kayan 1 saatlik pencereler (ts listeleri)
        self.lansman_ts: collections.deque = collections.deque()
        self.havuz_ts: collections.deque = collections.deque()
        self.r2_ham_mesaj = 0   # filtre oncesi gorulen tum mesajlar

    def pencere_say(self, dq: collections.deque) -> int:
        esik = time.time() - 3600
        while dq and dq[0] < esik:
            dq.popleft()
        return len(dq)


class Bus:
    def __init__(self, yazici, onbellek: DurumOnbellek, sayac: Sayaclar,
                 karar=None, maxsize: int = 20000):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.yazici = yazici
        self.onbellek = onbellek
        self.sayac = sayac
        self.karar = karar  # KararUretici, ana.py'de baglanir

    async def yayinla(self, akis: str, kind: str, payload: dict, **kw):
        """Kayip kabul etmeyen yol: kuyruk doluysa bekler."""
        await self.q.put((akis, kind, payload, kw))

    def yayinla_kayipli(self, akis: str, kind: str, payload: dict, **kw) -> bool:
        """Snapshot yolu: doluysa dusurur ve sayar (Throttled)."""
        try:
            self.q.put_nowait((akis, kind, payload, kw))
            return True
        except asyncio.QueueFull:
            self.sayac.dusen_snapshot += 1
            return False

    async def dagitici(self):
        while True:
            akis, kind, payload, kw = await self.q.get()
            ev = self.yazici.yaz(akis, kind, payload, **kw)
            self.sayac.kind_sayi[kind] += 1
            self.onbellek.guncelle(ev)
            if self.karar is not None:
                await self.karar.olay_isle(ev, akis)
            self.q.task_done()
