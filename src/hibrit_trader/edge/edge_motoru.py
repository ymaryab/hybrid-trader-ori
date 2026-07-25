"""Edge Engine: Edge(q, pi) = katman ici replay ortalamasi. OGRENMEZ.

Edge turetilen bir buyukluktur: arsiv (P(Path|q) ampirik olcusu) +
politika simulatoru kompozisyonu. Bu modul yalniz o kompozisyonu kosar
ve ham istatistik dondurur.

v1 koprusu: kill-bataryasi sonuclanana kadar CANLI karar akisi mevcut
otonom seciciden gecmeye devam eder; secici_koprusu() ayni skorlari
bu arayuzun sozlesmesiyle sunar. Boylece cagiran kod bugunden yeni
mimariye yazilir, model gelince yalniz kosullama degisir.
"""

from __future__ import annotations

from statistics import median

from .kosullama import KosullamaArayuzu, TekKatman
from .simulator import degerlendir


class EdgeMotoru:
    def __init__(self, arsiv, kosullama: KosullamaArayuzu | None = None,
                 q_saglayici=None):
        """q_saglayici: token -> q sozlugu (yoksa {} = tek katman)."""
        self.arsiv = arsiv
        self.kosullama = kosullama or TekKatman()
        self.q_saglayici = q_saglayici

    def edge(self, politika: dict, q: dict | None = None) -> dict:
        """Verilen q'nun katmanindaki yollar uzerinde politikayi kos."""
        hedef = self.kosullama.katman(q or {})
        sonuclar = []
        cikislar: dict[str, int] = {}
        for yol in self.arsiv.yollar():
            qy = self.q_saglayici(yol.token) if self.q_saglayici else {}
            if self.kosullama.katman(qy) != hedef:
                continue
            s = degerlendir(yol, politika)
            sonuclar.append(s["pnl_pct"])
            cikislar[s["cikis"]] = cikislar.get(s["cikis"], 0) + 1
        if not sonuclar:
            return {"katman": hedef, "n": 0, "edge_pct": None}
        return {"katman": hedef, "n": len(sonuclar),
                "edge_pct": round(sum(sonuclar) / len(sonuclar), 3),
                "medyan_pct": round(median(sonuclar), 3),
                "kazanma_orani": round(
                    sum(1 for p in sonuclar if p > 0) / len(sonuclar), 3),
                "cikislar": cikislar}


def secici_koprusu(pencere_dk: float = 30.0) -> dict[str, float]:
    """v1: mevcut otonom secici skorlarini edge sozlesmesine cevir.

    Donen deger tahsis.dagit()'in bekledigi {aday: edge} sozlugudur;
    edge vekili = kayan pencere pct (mevcut CANLI davranisla ayni girdi).
    """
    from hibrit_trader.canli_session import DESTEKLENEN_KAYNAKLAR
    from hibrit_trader.otonom_secici import pencere_skorlari
    skorlar = pencere_skorlari(pencere_dk, sorted(DESTEKLENEN_KAYNAKLAR))
    return {m: s["pct"] for m, s in skorlar.items()}
