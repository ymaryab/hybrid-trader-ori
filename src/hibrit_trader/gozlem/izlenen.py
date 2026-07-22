"""R0 izlenen kume yoneticisi.

Kaynaklar: (1) tum motor state dosyalarindaki acik pozisyonlar,
(2) son 6 saatte EKG tetiklemis tokenler. Oncelik pozisyonlarda;
tavan GOZLEM_R0_MAX (varsayilan 25, public RPC abonelik limiti).
Kume degisiklikleri TrackingPromoted/Demoted olaylari uretir.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
from pathlib import Path


class IzlenenKume:
    def __init__(self, bus, onbellek, veri_dizin: Path,
                 swap_abone=None, tavan: int | None = None):
        self.bus = bus
        self.onbellek = onbellek
        self.veri = Path(veri_dizin)
        self.swap_abone = swap_abone
        self.tavan = tavan or int(os.getenv("GOZLEM_R0_MAX", "25"))

    def _pozisyon_havuzlari(self) -> dict[str, dict]:
        cikti = {}
        for f in glob.glob(str(self.veri / "*_state.json")):
            try:
                s = json.loads(Path(f).read_text())
            except (ValueError, OSError):
                continue
            for p in (s.get("positions") or []):
                pa, ta = p.get("pool_address"), p.get("token_address")
                if pa and ta:
                    cikti[pa] = {"token": ta, "neden": "pozisyon",
                                 "pair": p.get("pair")}
        return cikti

    def _ekg_havuzlari(self) -> dict[str, dict]:
        """EKG dosyasinin son ~200KB'indan 6 saatlik tetikler."""
        import time
        yol = self.veri / "kosucu_ekg.jsonl"
        cikti = {}
        try:
            with open(yol, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 200_000))
                ham = f.read().decode(errors="replace").splitlines()[1:]
        except OSError:
            return cikti
        esik = time.time() - 6 * 3600
        for ln in ham:
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            if float(t.get("ts") or 0) < esik:
                continue
            pa, ta = t.get("pool_address"), t.get("token_address")
            if pa and ta:
                cikti[pa] = {"token": ta, "neden": "ekg",
                             "pair": t.get("pair")}
        return cikti

    async def calis(self):
        while True:
            try:
                yeni = self._pozisyon_havuzlari()
                for pa, meta in self._ekg_havuzlari().items():
                    if len(yeni) >= self.tavan:
                        break
                    yeni.setdefault(pa, meta)
                if len(yeni) > self.tavan:
                    # pozisyonlar tavani asarsa hepsi kalir (kayip yok),
                    # sadece durum raporlanir
                    await self.bus.yayinla(
                        "sistem", "Throttled",
                        {"neden": "r0_tavan", "istek": len(yeni),
                         "tavan": self.tavan}, src="izlenen")
                eski = self.onbellek.izlenen
                for pa in set(yeni) - set(eski):
                    await self.bus.yayinla(
                        "sistem", "TrackingPromoted",
                        {"pool": pa, **yeni[pa]},
                        token=yeni[pa]["token"], src="izlenen")
                for pa in set(eski) - set(yeni):
                    await self.bus.yayinla(
                        "sistem", "TrackingDemoted",
                        {"pool": pa, **eski[pa]},
                        token=eski[pa]["token"], src="izlenen")
                self.onbellek.izlenen = yeni
                if self.swap_abone is not None:
                    self.swap_abone.abonelik_ayarla(
                        {pa: "r0" for pa in yeni})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                await self.bus.yayinla(
                    "sistem", "GapDetected",
                    {"src": "izlenen", "neden": str(e)[:200]}, src="izlenen")
            await asyncio.sleep(30)
