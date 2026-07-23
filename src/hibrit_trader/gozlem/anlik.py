"""5 sn R0 snapshot uretici + 60 sn MarketContext.

Snapshot payload'u DexScreener pair yanitinin HAM halidir (turev alan
eklenmez, alan atilmaz). Kuyruk doluysa cevrim atlanir ve Throttled
sayilir: R0/R2 olay yolu asla sikismaz.
"""

from __future__ import annotations

import asyncio
import os
import time

from .kaynak_rpc import http_get_json, http_rpc

DEXS = "https://api.dexscreener.com/latest/dex/pairs/solana/"
SOL_MINT = "So11111111111111111111111111111111111111112"
# 24 Tem (429 baskisi karari): kadans 5s -> 15s; DexScreener kotasi
# tarayici/fast_price ile paylasildigi icin feed hizina oncelik verildi
SNAPSHOT_SN = float(os.getenv("GOZLEM_SNAPSHOT_SN", "15"))


class Anlik:
    def __init__(self, bus, onbellek, sayac):
        self.bus = bus
        self.onbellek = onbellek
        self.sayac = sayac
        self.http_rpc_url = os.getenv("GOZLEM_HTTP",
                                      "https://api.mainnet-beta.solana.com")

    async def snapshot_dongusu(self):
        while True:
            bas = time.monotonic()
            havuzlar = list(self.onbellek.izlenen)
            for i in range(0, len(havuzlar), 30):
                grup = havuzlar[i:i + 30]
                try:
                    y = await http_get_json(DEXS + ",".join(grup), timeout=4)
                except Exception as e:  # noqa: BLE001
                    await self.bus.yayinla(
                        "sistem", "GapDetected",
                        {"src": "snapshot", "neden": str(e)[:200]},
                        src="snapshot")
                    continue
                for pair in (y.get("pairs") or []):
                    tok = ((pair.get("baseToken") or {}).get("address"))
                    self.bus.yayinla_kayipli(
                        "anlik", "Snapshot", pair, token=tok, src="dexs")
            gecen = time.monotonic() - bas
            await asyncio.sleep(max(0.5, SNAPSHOT_SN - gecen))

    async def mctx_dongusu(self):
        while True:
            payload = {}
            try:
                y = await http_get_json(
                    "https://api.dexscreener.com/latest/dex/tokens/"
                    + SOL_MINT, timeout=6)
                pl = [p for p in (y.get("pairs") or [])
                      if p.get("priceUsd")]
                if pl:
                    p = max(pl, key=lambda x: float(
                        (x.get("liquidity") or {}).get("usd") or 0))
                    payload["sol_pair"] = p   # ham alanlar aynen
            except Exception as e:  # noqa: BLE001
                payload["sol_hata"] = str(e)[:120]
            try:
                f = await http_rpc(self.http_rpc_url,
                                   "getRecentPrioritizationFees", [])
                payload["prioritization_fees"] = f.get("result")
            except Exception as e:  # noqa: BLE001
                payload["fee_hata"] = str(e)[:120]
            payload["lansman_1h"] = self.sayac.pencere_say(
                self.sayac.lansman_ts)
            payload["havuz_1h"] = self.sayac.pencere_say(
                self.sayac.havuz_ts)
            payload["izlenen_sayi"] = len(self.onbellek.izlenen)
            await self.bus.yayinla("anlik", "MarketContext", payload,
                                   src="mctx")
            await asyncio.sleep(60)
