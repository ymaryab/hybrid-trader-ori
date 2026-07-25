"""Tier A sensor 4: erken alici profili (Sprint 2, 25 Tem). HAM VERI.

Butce satiri docs/sprint2_rpc_butcesi.md'de ONCE guncellendi (politika).
Tetik: YALNIZ terfi ani (token R0'a girince), token basina TEK olcum.
Maliyet: token basina <=22 kredi (1 largest + 1 multipleAccounts +
<=20 cuzdan-yasi); cuzdan yasi KALICI cache (diskte), gunluk TAVAN
GOZLEM_ALICI_TAVAN=2000 istek; asim Throttled ile SAYILIR. Tembel
kuyruk + 429'da ustel geri cekilme.

TANIM DURUSTLUGU (payload'a da yazilir):
- "erken alici" v0 proxy'si: terfi anindaki en buyuk N token-hesabinin
  SAHIP cuzdanlari (havuz/AMM kasalari dahil olabilir; ayiklama
  cevrimdisi yapilir, ham liste amounts ile kaydedilir).
- Cuzdan yasi: getSignaturesForAddress(limit=1000). <1000 imza ->
  en eski imza = ilk islem (kesin yas). =1000 -> "tavan_1000" bayragi
  (yas >= o partinin en eskisi; kesin yas bilinmiyor, bilinmiyor yazilir).
- SKOR URETILMEZ: yalniz ham alanlar + beyan edilen esikli agregalar
  (yeni_esik_gun sabiti payload'da; turev analiz Faz 0 bataryasinin isi).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

TAVAN_GUN = int(os.getenv("GOZLEM_ALICI_TAVAN", "2000"))
YENI_ESIK_GUN = float(os.getenv("GOZLEM_ALICI_YENI_GUN", "7"))
N_HOLDER = 20


class ErkenAlici:
    def __init__(self, bus, onbellek, veri: Path):
        self.bus = bus
        self.onbellek = onbellek
        self._olculen: set[str] = set()
        self._cache_yol = Path(veri) / "gozlem" / "cuzdan_yas_cache.json"
        try:
            self._cache = json.loads(self._cache_yol.read_text())
        except (OSError, ValueError):
            self._cache = {}
        self._negatif: dict[str, float] = {}
        self._gun = ""
        self._gun_istek = 0
        self._url_ix = 0

    # ---- rpc (rotasyon + gunluk tavan) --------------------------------
    async def _rpc(self, method, params):
        from .konsantrasyon import URLS
        from .kaynak_rpc import http_rpc
        gun = time.strftime("%Y-%m-%d", time.gmtime())
        if gun != self._gun:
            self._gun, self._gun_istek = gun, 0
        if self._gun_istek >= TAVAN_GUN:
            raise RuntimeError("gunluk_tavan")
        self._gun_istek += 1
        for _ in range(len(URLS)):
            url = URLS[self._url_ix % len(URLS)]
            try:
                r = await http_rpc(url, method, params, timeout=10)
                if r.get("result") is not None:
                    return r
                raise RuntimeError(str(r.get("error"))[:80])
            except Exception:
                self._url_ix += 1
        raise RuntimeError("rpc_basarisiz")

    def _cache_kaydet(self):
        tmp = self._cache_yol.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache))
        os.replace(tmp, self._cache_yol)

    async def _cuzdan_yasi(self, cuzdan: str) -> dict:
        """{yas_gun|None, tavan_1000, kaynak}. Kalici cache; hata: negatif 1h."""
        c = self._cache.get(cuzdan)
        if c is not None:
            return {**c, "kaynak": "cache"}
        if time.time() - self._negatif.get(cuzdan, 0) < 3600:
            raise RuntimeError("negatif_cache")
        try:
            r = await self._rpc("getSignaturesForAddress",
                                [cuzdan, {"limit": 1000}])
        except Exception:
            self._negatif[cuzdan] = time.time()
            raise
        imzalar = r.get("result") or []
        if not imzalar:
            kayit = {"yas_gun": 0.0, "tavan_1000": False}
        elif len(imzalar) >= 1000:
            kayit = {"yas_gun": None, "tavan_1000": True}
        else:
            ilk = min(float(s.get("blockTime") or time.time())
                      for s in imzalar)
            kayit = {"yas_gun": round((time.time() - ilk) / 86400, 2),
                     "tavan_1000": False}
        self._cache[cuzdan] = kayit
        return {**kayit, "kaynak": "rpc"}

    # ---- olcum --------------------------------------------------------
    async def _olc(self, pool: str, mint: str):
        r = await self._rpc("getTokenLargestAccounts", [mint])
        hesaplar = ((r.get("result") or {}).get("value") or [])[:N_HOLDER]
        adresler = [h.get("address") for h in hesaplar if h.get("address")]
        sahipler = {}
        if adresler:
            r2 = await self._rpc("getMultipleAccounts",
                                 [adresler, {"encoding": "jsonParsed"}])
            for adr, acc in zip(adresler, (r2.get("result") or {})
                                .get("value") or []):
                try:
                    sahipler[adr] = acc["data"]["parsed"]["info"]["owner"]
                except (TypeError, KeyError):
                    continue
        cuzdanlar = []
        yas_olculen = 0
        cap_atlanan = 0
        for h in hesaplar:
            owner = sahipler.get(h.get("address"))
            kayit = {"owner": owner, "miktar": h.get("uiAmountString")
                     or h.get("uiAmount"), "yas_gun": None,
                     "tavan_1000": None, "kaynak": None}
            if owner:
                try:
                    y = await self._cuzdan_yasi(owner)
                    kayit.update(y)
                    yas_olculen += 1
                except RuntimeError as e:
                    if str(e) == "gunluk_tavan":
                        cap_atlanan += 1
                    kayit["kaynak"] = "olculemedi"
                await asyncio.sleep(2.0)
            cuzdanlar.append(kayit)
        yaslar = [c["yas_gun"] for c in cuzdanlar
                  if c["yas_gun"] is not None]
        yeni = sum(1 for c in cuzdanlar
                   if c["yas_gun"] is not None
                   and c["yas_gun"] <= YENI_ESIK_GUN)
        eski = sum(1 for c in cuzdanlar
                   if c.get("tavan_1000") or (c["yas_gun"] is not None
                                              and c["yas_gun"] > YENI_ESIK_GUN))
        yaslar_s = sorted(yaslar)
        dagilim = {"lt1g": sum(1 for y in yaslar if y < 1),
                   "g1_7": sum(1 for y in yaslar if 1 <= y <= 7),
                   "g7_30": sum(1 for y in yaslar if 7 < y <= 30),
                   "gt30": sum(1 for y in yaslar if y > 30),
                   "tavan_1000": sum(1 for c in cuzdanlar
                                     if c.get("tavan_1000"))}
        self.bus.yayinla_kayipli(
            "sensor", "ErkenAlici",
            {"pool": pool, "mint": mint,
             "tanim": "terfi_ani_ilk%d_holder_sahipleri" % N_HOLDER,
             "yeni_esik_gun": YENI_ESIK_GUN,
             "cuzdanlar": cuzdanlar,
             "alici_n": len(cuzdanlar),
             "yeni_n": yeni, "eski_n": eski,
             "yeni_eski_orani": round(yeni / eski, 3) if eski else None,
             "ort_yas_gun": round(sum(yaslar) / len(yaslar), 2)
                            if yaslar else None,
             "medyan_yas_gun": yaslar_s[len(yaslar_s) // 2]
                               if yaslar_s else None,
             "dagilim": dagilim,
             "kapsam": {"holder_n": len(hesaplar),
                        "owner_cozulen": len(sahipler),
                        "yas_olculen": yas_olculen,
                        "cap_atlanan": cap_atlanan}},
            token=mint, src="erken_alici")

    async def calis(self):
        backoff = 0.0
        sayac = 0
        while True:
            await asyncio.sleep(max(5.0, backoff))
            aday = None
            for pool, meta in sorted(self.onbellek.izlenen.items()):
                if pool not in self._olculen and meta.get("token"):
                    aday = (pool, meta["token"])
                    break
            if aday is None:
                continue
            pool, mint = aday
            try:
                await self._olc(pool, mint)
                self._olculen.add(pool)
                backoff = 0.0
                sayac += 1
                if sayac % 5 == 0:
                    self._cache_kaydet()
            except Exception as e:  # noqa: BLE001
                neden = str(e)[:120]
                if neden == "gunluk_tavan":
                    self.bus.yazici.yaz(
                        "sistem", "Throttled",
                        {"src": "erken_alici", "neden": "gunluk_tavan",
                         "tavan": TAVAN_GUN}, src="erken_alici")
                    backoff = 3600.0
                else:
                    backoff = min(max(60.0, backoff * 2), 960.0)
                    self.bus.yazici.yaz(
                        "sistem", "GapDetected",
                        {"src": "erken_alici", "neden": neden,
                         "backoff_sn": backoff},
                        token=mint, src="erken_alici")
