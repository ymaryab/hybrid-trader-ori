"""Solana WSS logsSubscribe istemcisi: dinamik abonelik + kopus durustlugu.

Her kopus GapDetected olayi uretir; surekliligin kanitlanamadigi araliklar
asla gizlenmez. Tek baglantida coklu abonelik (adres basina bir
logsSubscribe) yonetilir.
"""

from __future__ import annotations

import asyncio
import json
import time

try:
    import websockets
except ImportError:  # testler icin: ws gerektirmeyen moduller etkilenmez
    websockets = None


class WssAbone:
    def __init__(self, url: str, bus, src: str, isleyici, on_ham=None):
        """isleyici(addr, etiket, result_dict) -> coroutine

        on_ham(ham_str) -> bool: ayristirma ONCESI ucuz metin filtresi.
        False donerse mesaj json.loads'a girmeden atlanir (sayimi
        on_ham'in kendisi yapar). Yuksek hacimli firehose baglantilarinda
        CPU ve bellek churn'unu bir buyukluk sirasi dusurur.
        """
        self.url = url
        self.bus = bus
        self.src = src
        self.isleyici = isleyici
        self.on_ham = on_ham
        self.istekler: dict[str, str] = {}   # addr -> etiket
        self._degisti = asyncio.Event()
        self._req_id = 0

    def abonelik_ayarla(self, istekler: dict[str, str]) -> None:
        if istekler != self.istekler:
            self.istekler = dict(istekler)
            self._degisti.set()

    async def _gap(self, neden: str):
        await self.bus.yayinla(
            "sistem", "GapDetected",
            {"src": self.src, "neden": neden, "abonelik": len(self.istekler)},
            src=self.src)

    async def calis(self):
        if websockets is None:
            await self._gap("websockets kutuphanesi yok")
            return
        bekleme = 1.0
        while True:
            try:
                async with websockets.connect(
                        self.url, ping_interval=20, ping_timeout=20,
                        max_size=2 ** 22) as ws:
                    bekleme = 1.0
                    await self._dongu(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - her kopus ayni yolda
                await self._gap(f"{type(e).__name__}: {e}"[:200])
                await asyncio.sleep(bekleme)
                bekleme = min(bekleme * 2, 60.0)

    async def _abone_ol(self, ws, addr: str, et: str, bekleyen) -> None:
        self._req_id += 1
        bekleyen[self._req_id] = (addr, et)
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": self._req_id,
            "method": "logsSubscribe",
            "params": [{"mentions": [addr]},
                       {"commitment": "processed"}]}))

    async def _dongu(self, ws):
        aktif: dict[int, tuple[str, str]] = {}    # subid -> (addr, etiket)
        bekleyen: dict[int, tuple[str, str]] = {} # reqid -> (addr, etiket)
        hedef = dict(self.istekler)
        for addr, et in hedef.items():
            await self._abone_ol(ws, addr, et, bekleyen)
        self._degisti.clear()
        while True:
            if self._degisti.is_set():
                yeni = dict(self.istekler)
                self._degisti.clear()
                for addr, et in yeni.items():
                    if addr not in hedef:
                        await self._abone_ol(ws, addr, et, bekleyen)
                for sid, (addr, _et) in list(aktif.items()):
                    if addr not in yeni:
                        self._req_id += 1
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": self._req_id,
                            "method": "logsUnsubscribe", "params": [sid]}))
                        del aktif[sid]
                hedef = yeni
            try:
                async with asyncio.timeout(3):
                    ham = await ws.recv()
            except TimeoutError:
                continue    # sessiz baglanti: abonelik degisimini isle
            # ucuz on-filtre: firehose'da json.loads'a girmeden ele
            if self.on_ham is not None and not self.on_ham(ham):
                continue
            try:
                m = json.loads(ham)
            except ValueError:
                continue
            if m.get("method") == "logsNotification":
                sid = m["params"]["subscription"]
                addr, et = aktif.get(sid, (None, None))
                if addr is not None:
                    await self.isleyici(addr, et, m["params"])
            elif "id" in m and m["id"] in bekleyen:
                addr_et = bekleyen.pop(m["id"])
                if isinstance(m.get("result"), int):
                    aktif[m["result"]] = addr_et


async def http_rpc(url: str, method: str, params: list, timeout: float = 6.0):
    """Basit stdlib HTTP RPC (thread'de)."""
    import urllib.request

    def _cagir():
        req = urllib.request.Request(
            url, data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                  "method": method,
                                  "params": params}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    return await asyncio.to_thread(_cagir)


async def http_get_json(url: str, timeout: float = 6.0):
    import urllib.request

    def _cagir():
        req = urllib.request.Request(url, headers={"User-Agent": "gozlemci/1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    return await asyncio.to_thread(_cagir)


def simdi_ms() -> int:
    return int(time.time() * 1000)
