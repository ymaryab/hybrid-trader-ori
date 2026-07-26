"""Edge KARAR CEKIRDEGI v2 (26 Tem, CRITICAL blokerler 1-2-5). GOLGE.

docs/edge_aile_secici_tasarimi.md akisinin kodu: uygunluk -> aile
skoru -> LCB -> CASH tabani -> histerezis/cooldown -> guven ->
fallback merdiveni. YALNIZ golge yolunda kosar; canli karari GO
on-kaydi (docs/edge_go_onkayit.md) saglanmadan BAGLANMAZ.

v2 girdi vekili: uye motorlarin kayan-pencere pct'leri (gerceklesen).
Replay-tabanli aile edge'i (censoring on-kaydi cozulunce) ayni arayuze
takilir; karar akisi degismez.

Fallback katmanlari (hangisinin konustugu her olayda yazilir):
  cekirdek      normal karar
  girdi_yok     skor uretilemedi -> onceki karar (yoksa CASH)
  cekirdek_hata istisna -> "legacy" isareti (golge kiyasinda legacy
                davranisi esas alinir); EDGE_ARIZA_SIM=1 tatbikat kapisi
"""

from __future__ import annotations

import math
import os
from statistics import median, pstdev

KATALOG = {
    "scalp": {"uyeler": ("v7", "v7c", "v7d", "v7hizli", "v7ht",
                         "v7new", "v7t", "yz", "yzn1")},
    "runner": {"uyeler": ("r1", "r2")},
}
CASH = "cash"

LCB_K = float(os.getenv("EDGE_LCB_K", "1.0"))
AILE_MARJ = float(os.getenv("EDGE_AILE_MARJ", "0.75"))     # puan
TEYIT_TUR = int(os.getenv("EDGE_TEYIT_TUR", "2"))          # ardisik tur
COOLDOWN_TUR = int(os.getenv("EDGE_COOLDOWN_TUR", "4"))    # 4x5dk=20dk
GUVEN_ISLEM_DOYMA = 50.0


class Cekirdek:
    def __init__(self):
        self.karar_aile = CASH          # mevcut golge karari
        self._hedef_sayac: dict = {}    # aday aile -> ardisik tur
        self._tur = 0
        self._son_gecis_turu = -10**9

    # ---- saf yardimcilar ------------------------------------------------
    @staticmethod
    def aile_skoru(uyeler: list[dict]) -> dict | None:
        pctler = [float(u.get("pct") or 0.0) for u in uyeler]
        if not pctler:
            return None
        islem = sum(int(u.get("islem") or 0) for u in uyeler)
        med = median(pctler)
        yayilim = pstdev(pctler) if len(pctler) > 1 else abs(med)
        lcb = med - LCB_K * yayilim / math.sqrt(max(islem, 1))
        return {"medyan": round(med, 3), "lcb": round(lcb, 3),
                "islem": islem, "uye_n": len(pctler)}

    # ---- karar ----------------------------------------------------------
    def karar(self, skorlar: dict) -> dict:
        self._tur += 1
        if os.getenv("EDGE_ARIZA_SIM") == "1":
            raise RuntimeError("tatbikat: EDGE_ARIZA_SIM")
        tablo = {}
        for aile, tanim in KATALOG.items():
            uyeler = [skorlar[m] for m in tanim["uyeler"] if m in skorlar]
            s = self.aile_skoru(uyeler)
            if s is not None:
                tablo[aile] = s
        if not tablo:
            return self._sonuc("girdi_yok", tablo, guven=0.0)
        # CASH tabani: en iyi ailenin LCB'si 0'i gecmiyorsa hedef CASH
        en_iyi = max(tablo, key=lambda a: tablo[a]["lcb"])
        hedef = en_iyi if tablo[en_iyi]["lcb"] > 0 else CASH
        # histerezis: mevcut karardan ayrilmak marj + teyit ister
        if hedef != self.karar_aile:
            if (hedef != CASH and self.karar_aile in tablo
                    and tablo[hedef]["lcb"] - tablo[self.karar_aile]["lcb"]
                    < AILE_MARJ):
                hedef = self.karar_aile          # marj icinde: kal
        if hedef != self.karar_aile:
            self._hedef_sayac = {hedef: self._hedef_sayac.get(hedef, 0) + 1}
            teyitli = self._hedef_sayac[hedef] >= TEYIT_TUR
            cooldown_ok = (self._tur - self._son_gecis_turu) >= COOLDOWN_TUR
            if teyitli and cooldown_ok:
                self.karar_aile = hedef
                self._son_gecis_turu = self._tur
                self._hedef_sayac = {}
        else:
            self._hedef_sayac = {}
        islem = tablo.get(self.karar_aile, {}).get("islem", 0)
        guven = min(1.0, islem / GUVEN_ISLEM_DOYMA)
        if self._hedef_sayac:                   # teyit beklenen aday var
            guven *= 0.7
        return self._sonuc("cekirdek", tablo, guven=round(guven, 2))

    def _sonuc(self, katman: str, tablo: dict, guven: float) -> dict:
        dagilim = ({} if self.karar_aile == CASH
                   else {self.karar_aile: 1.0})
        return {"surum": "v2", "katman": katman, "aile": self.karar_aile,
                "dagilim": dagilim, "guven": guven,
                "aile_tablosu": tablo,
                "bekleyen_aday": dict(self._hedef_sayac),
                "tur": self._tur}

    def temsilci(self, skorlar: dict) -> str | None:
        """KPI surekliligi: secili ailenin en yuksek pct'li uyesi."""
        if self.karar_aile == CASH:
            return None
        uyeler = [(m, float(skorlar[m].get("pct") or 0.0))
                  for m in KATALOG[self.karar_aile]["uyeler"]
                  if m in skorlar]
        if not uyeler:
            return None
        return max(uyeler, key=lambda x: x[1])[0]
