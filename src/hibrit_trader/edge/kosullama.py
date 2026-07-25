"""Path Conditioning arayuzu: q -> katman (yol arsivinde komsuluk anahtari).

BURASI ZINCIRIN EKSIK HALKASI ve BILEREK BOS. Gercek kosullama modeli
YALNIZCA kill-bataryasi (mekanizma-AUC >= 0.65 VE kahin-yakalama >= %30)
gecerse takilir; oncesinde buraya ogrenen model eklemek YASAK (on-kayit,
25 Tem). v1'de tek katman vardir: "hepsi" (kosullama yok varsayimi = H0).

Tasarim notu: katmanlar KABA baslar (birkac kova); q zenginlestikce
komsuluk seyreklesir, genisleme ancak katman basina yeterli orneklemle.
"""

from __future__ import annotations


class KosullamaArayuzu:
    """q sozlugunu bir katman anahtarina esler."""

    def katman(self, q: dict) -> str:
        raise NotImplementedError

    def katmanlar(self) -> list[str]:
        raise NotImplementedError


class TekKatman(KosullamaArayuzu):
    """v1 (H0): kosullama yok, butun yollar tek havuz."""

    AD = "hepsi"

    def katman(self, q: dict) -> str:
        return self.AD

    def katmanlar(self) -> list[str]:
        return [self.AD]
