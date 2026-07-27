"""Forensic Factory: veri katmani ve GUVEN KAPISI.

Tasarim ilkesi: fabrika, 28 Tem veri denetiminde bulunan tuzaklara
YAPISAL olarak izin vermez.

  1) Guvenilir baslangic tarihi (23 Tem 2026) ONCESI satirlar varsayilan
     olarak evrene alinmaz; alinacaksa cagiran acikca ister ve rapora
     "kirli pencere" damgasi basilir.
  2) Hic yazilmayan alanlar (ALAN_SICILI'nde durum="yok") kullanilamaz;
     istenirse hata verir. Sifirla doldurma YASAK: eksik alan, satirin
     dusurulmesi demektir (fabrika bunu sayar ve raporlar).
  3) Kismi dolu alanlar (durum="kismi") kullanilabilir ama analiz o
     alan icin ALT-EVRENDE calisir ve rapor bunu belirtir.
  4) Kismi cikis satirlari (tp_partial/kilit/trail) ayni pozisyonun
     parcalaridir; varsayilan olarak pozisyon duzeyinde birlestirilir.
"""

from __future__ import annotations

import calendar
import glob
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# 28 Tem veri denetimi: bu tarihten once defterler sansurlu (kayip
# tarafi cikisi olmayan motorlar), pool_yas_dk bos, gozlem omurgasi yok.
GUVENILIR_BASLANGIC = "2026-07-23T00:00:00Z"

# Alan sicili: durum = tam | kismi | yok | supheli
#   tam     : guvenilir pencerede pratikte %100 dolu
#   kismi   : dolu ama secilmis alt-evrende (oran verilir)
#   yok     : hic yazilmiyor, kullanilamaz
#   supheli : yaziliyor ama degeri dogrulanamadi
ALAN_SICILI: dict[str, dict] = {
    "pnl_pct":            {"durum": "tam", "damga": "cikis"},
    "pnl_usd":            {"durum": "tam", "damga": "cikis"},
    "cost_usd":           {"durum": "tam", "damga": "giris"},
    "exit_reason":        {"durum": "tam", "damga": "cikis"},
    "hold_sec":           {"durum": "tam", "damga": "cikis"},
    "token_address":      {"durum": "tam", "damga": "giris"},
    # Turetilmis alan: defterde yok, yukleyici ts - hold_sec ile kurar.
    # Buyuklugun kendisi (girisin ne zaman yapildigi) giris anina aittir;
    # yeniden kurulum yontemi cikis verisi kullanir, bu bir sizinti degil.
    "_giris_ts":          {"durum": "tam", "damga": "giris",
                           "not": "yukleyici tarafindan ts - hold_sec ile kurulur"},
    "chg_h1":             {"durum": "tam", "damga": "giris"},
    "chg_m5":             {"durum": "tam", "damga": "giris"},
    "liq_entry":          {"durum": "tam", "damga": "giris"},
    "mae_pct":            {"durum": "tam", "zaman": "sonra"},
    "mfe_pct":            {"durum": "tam", "zaman": "sonra"},
    "karar_fiyat":        {"durum": "tam"},
    "karar_cikis":        {"durum": "tam", "zaman": "sonra"},
    "karar_pnl_pct":      {"durum": "tam", "zaman": "sonra"},
    "pool_yas_dk":        {"durum": "tam", "guvenilir_ts": "2026-07-21T00:00:00Z",
                           "damga": "giris"},
    # 28 Tem kod denetimi (v7hizli_session referans alindi):
    #   :692  tetik_gecikme = now - _price_ts  -> CIKIS aninda uretilir
    #   :721  friction_pct  = entry_slip_pct + cikis slip  -> dolum SONRASI
    #   :518/:551 entry_fresh_fark_pct -> giris dogrulamasinda, karar aninda
    #   :395-403/:541-546 chg_h1/chg_m5/liq_entry/cost_usd -> giris damgasi
    "tetik_gecikme_sec":  {"durum": "kismi", "oran": 0.75, "damga": "cikis",
                           "not": "cikisi tetikleyen ornek ile kapanis arasi gecikme"},
    "entry_fresh_fark_pct": {"durum": "kismi", "oran": 0.58, "damga": "giris"},
    "friction_pct":       {"durum": "kismi", "oran": 0.99, "damga": "dolum_sonrasi",
                           "not": "entry_slip + cikis slip; karar aninda bilinemez"},
    "sol_chg_h1":         {"durum": "supheli", "not": "canli defterde sabit 0.000"},
    "mae_at_sec":         {"durum": "yok"},
    "mfe_at_sec":         {"durum": "yok"},
    "entry_drift":        {"durum": "yok"},
    "dec_to_entry_sec":   {"durum": "yok"},
    "source":             {"durum": "yok"},
}

KISMI_CIKIS = {"tp_partial_1", "tp_partial_2", "tp_kilit_25", "tp_kilit_40",
               "tp_kilit_1", "tp_kilit_2", "runner_trail"}

VARSAYILAN_MOTORLAR = ("v7", "v7c", "v7d", "v7hizli", "v7ht", "v7new",
                       "v7t", "yz", "yzn1", "canli")


def _ts(iso: str) -> float:
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


class GuvenHatasi(RuntimeError):
    """Kullanilamaz alan veya kirli pencere talebi."""


@dataclass
class Evren:
    """Yuklenen islem evreni + saydamlik kayitlari."""
    islemler: list[dict]
    baslangic_ts: float
    kirli_pencere: bool = False
    dusen: dict = field(default_factory=dict)   # neden -> adet
    birlestirilen_poz: int = 0
    motorlar: tuple = ()

    def ozet(self) -> dict:
        return {"n": len(self.islemler),
                "baslangic": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime(self.baslangic_ts)),
                "kirli_pencere": self.kirli_pencere,
                "dusen": dict(self.dusen),
                "birlestirilen_pozisyon": self.birlestirilen_poz,
                "motorlar": list(self.motorlar)}


def alan_kontrol(alanlar: tuple[str, ...]) -> None:
    """Istenen alanlar kullanilabilir mi; degilse yapisal hata."""
    for a in alanlar:
        s = ALAN_SICILI.get(a)
        if s is None:
            raise GuvenHatasi(f"bilinmeyen alan: {a} (sicile ekleyin)")
        if s["durum"] == "yok":
            raise GuvenHatasi(
                f"'{a}' uretimde HIC yazilmiyor; bu alanla analiz yapilamaz. "
                "Once motor tarafinda yazilmasi gerekir.")


def _veri_dizin() -> Path:
    return Path(os.getenv("MOMENTUM_DATA_DIR", "data"))


def yukle(motorlar: tuple[str, ...] = VARSAYILAN_MOTORLAR,
          baslangic: str | None = None,
          kirli_pencereye_izin: bool = False,
          poz_birlestir: bool = True) -> Evren:
    """Defterleri guven kapisindan gecirerek yukler."""
    bas = _ts(baslangic or GUVENILIR_BASLANGIC)
    kirli = bas < _ts(GUVENILIR_BASLANGIC)
    if kirli and not kirli_pencereye_izin:
        raise GuvenHatasi(
            f"{baslangic} guvenilir baslangictan ({GUVENILIR_BASLANGIC}) once. "
            "Bilerek istiyorsaniz kirli_pencereye_izin=True verin; rapor "
            "damgalanir.")
    d = _veri_dizin()
    dusen: dict = {}
    ham: list[dict] = []
    for m in motorlar:
        yol = d / f"{m}_trades.jsonl"
        if not yol.exists():
            dusen["defter_yok"] = dusen.get("defter_yok", 0) + 1
            continue
        for ln in yol.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except ValueError:
                dusen["bozuk_satir"] = dusen.get("bozuk_satir", 0) + 1
                continue
            if t.get("type"):                    # kural_degisim vb. meta
                continue
            if t.get("pnl_pct") is None or not t.get("ts"):
                dusen["eksik_sonuc"] = dusen.get("eksik_sonuc", 0) + 1
                continue
            if t["ts"] < bas:
                dusen["pencere_disi"] = dusen.get("pencere_disi", 0) + 1
                continue
            t["_motor"] = m
            t["_giris_ts"] = t["ts"] - (t.get("hold_sec") or 0)
            ham.append(t)

    birlesen = 0
    if poz_birlestir:
        ham, birlesen = _pozisyon_birlestir(ham)
    ham.sort(key=lambda t: t["_giris_ts"])
    return Evren(islemler=ham, baslangic_ts=bas, kirli_pencere=kirli,
                 dusen=dusen, birlestirilen_poz=birlesen,
                 motorlar=tuple(motorlar))


def _pozisyon_birlestir(satirlar: list[dict]) -> tuple[list[dict], int]:
    """Ayni trade_id'nin kismi cikislarini TEK pozisyona indirger.

    Neden: kismi satirlar pozisyon duzeyi mae/mfe tasir; bagimsiz islem
    sayilirsa hem n sisirilir hem ayni yol birden cok kez oylanir.
    """
    grup: dict[str, list[dict]] = {}
    tekil: list[dict] = []
    for t in satirlar:
        tid = t.get("trade_id")
        if not tid:
            tekil.append(t)
            continue
        grup.setdefault(tid, []).append(t)
    cikti = list(tekil)
    birlesen = 0
    for tid, g in grup.items():
        if len(g) == 1:
            cikti.append(g[0])
            continue
        birlesen += 1
        g.sort(key=lambda t: t["ts"])
        son = dict(g[-1])                       # giris damgalari son satirdan
        son["pnl_usd"] = sum(x.get("pnl_usd") or 0.0 for x in g)
        son["cost_usd"] = sum(x.get("cost_usd") or 0.0 for x in g)
        son["pnl_pct"] = (100 * son["pnl_usd"] / son["cost_usd"]
                          if son["cost_usd"] else 0.0)
        son["_parca_n"] = len(g)
        son["_parca_cikislar"] = [x.get("exit_reason") for x in g]
        son["exit_reason"] = next(
            (x.get("exit_reason") for x in reversed(g)
             if x.get("exit_reason") not in KISMI_CIKIS), g[-1].get("exit_reason"))
        son["_giris_ts"] = min(x["_giris_ts"] for x in g)
        cikti.append(son)
    return cikti, birlesen
