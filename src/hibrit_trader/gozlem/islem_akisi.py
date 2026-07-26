"""K1: pump.fun TradeEvent CANLI agregat akisi (Sprint 6, blueprint).

Firehose'un sayim imzalarina uymayan "Program data:" mesajlari sinirli
kuyruga alinir; worker decode eder (surumlu anchor_kayit.json, 60 sn'de
sicak-yenilenir), mint x dakika agregatlari "islem" akisina yazilir.

BACKPRESSURE (3 kademe, hepsi sayili/beyanli):
  1) kuyruk dolu -> ham dusurulur (dusen_kuyruk)
  2) kuyruk doluluk >%50 -> ORNEKLEME (her N'inci; N adaptif 2..8)
  3) aktif mint tavani -> en eski dakika erken flush + Throttled

KESIF MODU: bilinmeyen discriminator'lar sayilir; 'pump' sonekli 32B
pencere histogrami tutulur (mint_ofs adayi) ve Buy/Sell log korelasyonu
kaydedilir. Kayit dosyasina PIN insan onayiyla yapilir; kod tahmin
ETMEZ. Tum satirlar sv (schema_version) tasir; eski arsiv asla
donusturulmez (Madde 11).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path

from .anchor_decode import (SCHEMA_VERSION, AnchorDecoder, kayit_yukle,
                            veri_payloadlari)
from .lp_kilit import _b58

KUYRUK_MAX = int(os.getenv("ISLEM_KUYRUK", "20000"))
AKTIF_MINT_TAVAN = int(os.getenv("ISLEM_MINT_TAVAN", "4000"))
KAYIT_YENILE_SN = 60.0
KESIF_DISC_TAVAN = 30
KESIF_ORNEK = 300


class IslemAkisi:
    def __init__(self, bus, veri: Path):
        self.bus = bus
        self.veri = Path(veri)
        self.q: asyncio.Queue = asyncio.Queue(maxsize=KUYRUK_MAX)
        self.dec = AnchorDecoder(kayit_yukle(self.veri))
        self._kayit_kontrol_ts = 0.0
        self._kayit_mtime = 0.0
        # sayaclar (ObserverHealth)
        self.dusen_kuyruk = 0
        self.dusen_ornekleme = 0
        self.islenen = 0
        self.cozulen = 0
        self.ornekleme_n = 1
        self._giris_sayac = 0
        # agregatlar
        self.agreg: dict = {}
        # kesif
        self.kesif: dict = {}

    # ---- on-filtre yolundan cagrilir (sayim eslesmeyenler) -------------
    def on_ham(self, ham: str) -> None:
        if "Program data:" not in ham:
            return
        self._giris_sayac += 1
        if self.ornekleme_n > 1 and \
                self._giris_sayac % self.ornekleme_n:
            self.dusen_ornekleme += 1
            return
        try:
            self.q.put_nowait(ham)
        except asyncio.QueueFull:
            self.dusen_kuyruk += 1

    # ---- kayit sicak-yenileme ------------------------------------------
    def _kayit_yenile(self) -> None:
        simdi = time.time()
        if simdi - self._kayit_kontrol_ts < KAYIT_YENILE_SN:
            return
        self._kayit_kontrol_ts = simdi
        yol = self.veri / "gozlem" / "anchor_kayit.json"
        try:
            mt = yol.stat().st_mtime
        except OSError:
            return
        if mt != self._kayit_mtime:
            self._kayit_mtime = mt
            self.dec = AnchorDecoder(kayit_yukle(self.veri))

    # ---- kesif ----------------------------------------------------------
    def _kesfe_yaz(self, ham_b: bytes, log_yon: int) -> None:
        disc = ham_b[:8].hex()
        if disc not in self.kesif:
            if len(self.kesif) >= KESIF_DISC_TAVAN:
                return
            self.kesif[disc] = {"n": 0, "boy": Counter(),
                                "mint_ofs": Counter(),
                                "buy_n": 0, "sell_n": 0}
        g = self.kesif[disc]
        g["n"] += 1
        if log_yon > 0:
            g["buy_n"] += 1
        elif log_yon < 0:
            g["sell_n"] += 1
        if g["n"] <= KESIF_ORNEK:
            g["boy"][len(ham_b)] += 1
            for o in range(8, min(len(ham_b) - 31, 240), 8):
                if _b58(ham_b[o:o + 32]).endswith("pump"):
                    g["mint_ofs"][o] += 1

    # ---- isleme ---------------------------------------------------------
    def _isle_metin(self, metin: str) -> None:
        self._kayit_yenile()
        try:
            y = json.loads(metin)
        except ValueError:
            return
        val = ((((y.get("params") or {}).get("result") or {})
                .get("value")) or {})
        logs = val.get("logs") or []
        if val.get("err") is not None:
            return
        log_yon = (1 if any("Instruction: Buy" in l for l in logs)
                   else -1 if any("Instruction: Sell" in l for l in logs)
                   else 0)
        self.islenen += 1
        ts = time.time()
        for ham_b in veri_payloadlari(logs):
            if len(ham_b) < 8:
                continue
            r = self.dec.coz(ham_b)
            if r is None:
                self._kesfe_yaz(ham_b, log_yon)
                continue
            self.cozulen += 1
            self._agregata(r, ts)

    def _agregata(self, r: dict, ts: float) -> None:
        dk = int(ts // 60)
        anah = (r["mint"], dk)
        g = self.agreg.get(anah)
        fiyat = None
        if r.get("sol_lamport") and r.get("token_miktar"):
            try:
                fiyat = (r["sol_lamport"] / 1e9) / (r["token_miktar"] / 1e6)
            except ZeroDivisionError:
                fiyat = None
        if g is None:
            if len(self.agreg) >= AKTIF_MINT_TAVAN:
                self._flush(erken=True)
            g = self.agreg[anah] = {
                "n_al": 0, "n_sat": 0, "sol_al": 0, "sol_sat": 0,
                "o": fiyat, "h": fiyat, "l": fiyat, "c": fiyat}
        if r.get("is_buy"):
            g["n_al"] += 1
            g["sol_al"] += r.get("sol_lamport") or 0
        else:
            g["n_sat"] += 1
            g["sol_sat"] += r.get("sol_lamport") or 0
        if fiyat is not None:
            g["c"] = fiyat
            g["h"] = fiyat if g["h"] is None else max(g["h"], fiyat)
            g["l"] = fiyat if g["l"] is None else min(g["l"], fiyat)
            if g["o"] is None:
                g["o"] = fiyat

    # ---- flush ----------------------------------------------------------
    def _flush(self, erken: bool = False) -> int:
        su_dk = int(time.time() // 60)
        yaz = [(a, g) for a, g in self.agreg.items()
               if erken or a[1] < su_dk]
        if erken and yaz:
            en_eski_dk = min(a[1] for a, _ in yaz)
            yaz = [(a, g) for a, g in yaz if a[1] == en_eski_dk]
            self.bus.yazici.yaz("sistem", "Throttled",
                                {"src": "islem_akisi",
                                 "neden": "aktif_mint_tavan",
                                 "tavan": AKTIF_MINT_TAVAN},
                                src="islem_akisi")
        mod = ("ornekleme" if self.ornekleme_n > 1 else "tam")
        for (mint, dk), g in yaz:
            self.bus.yayinla_kayipli(
                "islem", "TradeAggregate",
                {"sv": SCHEMA_VERSION, "ts_dk": dk * 60, "mod": mod,
                 **{k: (round(v, 12) if isinstance(v, float) else v)
                    for k, v in g.items()}},
                token=mint, src="islem_akisi")
            del self.agreg[(mint, dk)]
        return len(yaz)

    def _ornekleme_ayarla(self) -> None:
        doluluk = self.q.qsize() / max(KUYRUK_MAX, 1)
        if doluluk > 0.5:
            self.ornekleme_n = min(8, max(2, self.ornekleme_n * 2))
        elif doluluk < 0.1 and self.ornekleme_n > 1:
            self.ornekleme_n = max(1, self.ornekleme_n // 2)

    def nesneler(self) -> dict:
        return {"islem_kuyruk": self.q.qsize(),
                "islem_aktif_mint": len(self.agreg),
                "islem_dusen_kuyruk": self.dusen_kuyruk,
                "islem_dusen_ornekleme": self.dusen_ornekleme,
                "islem_ornekleme_n": self.ornekleme_n,
                "islem_islenen": self.islenen,
                "islem_cozulen": self.cozulen,
                "islem_kesif_disc": len(self.kesif)}

    # ---- gorevler --------------------------------------------------------
    async def worker(self):
        while True:
            metin = await self.q.get()
            try:
                self._isle_metin(metin)
            except Exception:  # noqa: BLE001  (akis asla olmez)
                pass
            self.q.task_done()

    async def flush_dongusu(self):
        while True:
            await asyncio.sleep(60)
            try:
                self._ornekleme_ayarla()
                n = self._flush()
                kesif_ozet = {d: {"n": g["n"], "buy": g["buy_n"],
                                  "sell": g["sell_n"],
                                  "boy": g["boy"].most_common(1),
                                  "mint_ofs": g["mint_ofs"].most_common(2)}
                              for d, g in sorted(
                                  self.kesif.items(),
                                  key=lambda x: -x[1]["n"])[:5]}
                self.bus.yayinla_kayipli(
                    "islem", "IslemPulse",
                    {"sv": SCHEMA_VERSION, "yazilan": n,
                     **self.nesneler(), "kesif_top5": kesif_ozet},
                    src="islem_akisi")
            except Exception:  # noqa: BLE001
                pass
