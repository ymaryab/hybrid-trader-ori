"""Motor muslugu: motor DOSYA ciktilarindan olay uretir, motor koduna
dokunmaz (dondurma taahhudu).

Sadakat siniri (rapor edilir): SignalGenerated motor icinden ANLIK
yayilamiyor; girisler state dosyasi farkindan (<=3 sn gecikme),
cikislar defter satirindan, canli dolumlar WAL'dan, reddedilen
sinyaller reject gunlugunden turetilir. Motor ici kanca, YZn1 100
islem dondurmasi kalkinca tek satirlik cagriyla eklenecek.

Ofsetler data/gozlem/musluk_ofset.json'da tutulur: yeniden baslatmada
kacirilan defter satirlari geriye donuk okunur.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
from pathlib import Path


class Musluk:
    def __init__(self, bus, veri_dizin: Path, gozlem_dizin: Path):
        self.bus = bus
        self.veri = Path(veri_dizin)
        self.ofset_yol = Path(gozlem_dizin) / "musluk_ofset.json"
        try:
            self.ofset = json.loads(self.ofset_yol.read_text())
        except (OSError, ValueError):
            self.ofset = {}
        self._poz_cache: dict[str, set] = {}
        self._ilk_tur = True

    def _ofset_kaydet(self):
        tmp = self.ofset_yol.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ofset))
        os.replace(tmp, self.ofset_yol)

    async def _dosya_kuyrugu(self, yol: str, isleyici):
        """Tek jsonl dosyasini ofsetten itibaren okur, yeni satir isler."""
        anah = os.path.basename(yol)
        # ilk goruste: gecmisi degil sadece bundan sonrasini izle
        if anah not in self.ofset:
            try:
                self.ofset[anah] = os.path.getsize(yol)
            except OSError:
                self.ofset[anah] = 0
        try:
            boy = os.path.getsize(yol)
        except OSError:
            return
        if boy < self.ofset[anah]:          # kesilme: bastan
            self.ofset[anah] = 0
        if boy == self.ofset[anah]:
            return
        with open(yol, "rb") as f:
            f.seek(self.ofset[anah])
            ham = f.read()
        # yarim satir: son \n'e kadar isle
        kes = ham.rfind(b"\n")
        if kes < 0:
            return
        for ln in ham[:kes].splitlines():
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            await isleyici(t)
        self.ofset[anah] += kes + 1

    async def _defter_satiri(self, motor: str, t: dict):
        if t.get("type"):
            await self.bus.yayinla("motor", "EngineRuleChange",
                                   {"engine": motor, **t},
                                   token=t.get("token_address"),
                                   src=f"defter:{motor}")
            return
        await self.bus.yayinla("motor", "EngineExitFilled",
                               {"engine": motor, **t},
                               token=t.get("token_address"),
                               src=f"defter:{motor}")

    async def _wal_satiri(self, t: dict):
        await self.bus.yayinla("motor", "CanliFill", dict(t),
                               token=t.get("token_address"), src="wal")

    async def _reject_satiri(self, t: dict):
        await self.bus.yayinla("motor", "SignalRejected", dict(t),
                               token=t.get("token_address"), src="rejects")

    async def _state_farki(self):
        for f in glob.glob(str(self.veri / "*_state.json")):
            motor = os.path.basename(f).replace("_state.json", "").upper()
            try:
                s = json.loads(Path(f).read_text())
            except (ValueError, OSError):
                continue
            pozlar = {p.get("trade_id"): p for p in (s.get("positions") or [])
                      if p.get("trade_id")}
            eski = self._poz_cache.get(motor)
            self._poz_cache[motor] = set(pozlar)
            if eski is None:
                continue    # ilk gorus: sessiz tohumla (eski pozlar yeni degil)
            for tid in set(pozlar) - eski:
                p = pozlar[tid]
                await self.bus.yayinla("motor", "EngineEntryFilled",
                                       {"engine": motor, **p},
                                       token=p.get("token_address"),
                                       src=f"state:{motor}")

    async def calis(self):
        n = 0
        while True:
            try:
                for f in glob.glob(str(self.veri / "*_trades.jsonl")):
                    motor = os.path.basename(f).replace(
                        "_trades.jsonl", "").upper()
                    await self._dosya_kuyrugu(
                        f, lambda t, m=motor: self._defter_satiri(m, t))
                wal = self.veri / "canli_fills.jsonl"
                if wal.exists():
                    await self._dosya_kuyrugu(str(wal), self._wal_satiri)
                rej = self.veri / "momentum_rejects.jsonl"
                if rej.exists():
                    await self._dosya_kuyrugu(str(rej), self._reject_satiri)
                await self._state_farki()
                n += 1
                if n % 10 == 0:
                    self._ofset_kaydet()
            except asyncio.CancelledError:
                self._ofset_kaydet()
                raise
            except Exception as e:  # noqa: BLE001
                await self.bus.yayinla("sistem", "GapDetected",
                                       {"src": "musluk",
                                        "neden": str(e)[:200]}, src="musluk")
            await asyncio.sleep(3)
