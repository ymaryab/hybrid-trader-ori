"""Allocation arayuzu: edge sozlugu -> sermaye paylari (toplam 1.0).

v1 = HepsiLidere: mevcut otonom secici davranisinin birebir karsiligi
(tek motora %100). Kesirli-Kelly vb. gelecek surumler AYNI arayuzu
doldurur; cagiran kod degismez. Ogrenen model yok, saf fonksiyon.
"""

from __future__ import annotations


class TahsisArayuzu:
    def dagit(self, edgeler: dict[str, float]) -> dict[str, float]:
        """edgeler: aday -> beklenen avantaj. Donen paylar toplami 1.0
        (pozitif edge yoksa bos sozluk = salter indir)."""
        raise NotImplementedError


class HepsiLidere(TahsisArayuzu):
    """Kazanan-hepsini-alir; esitlikte alfabetik (deterministik)."""

    def __init__(self, esik: float = 0.0):
        self.esik = esik

    def dagit(self, edgeler: dict[str, float]) -> dict[str, float]:
        uygun = {m: e for m, e in edgeler.items() if e > self.esik}
        if not uygun:
            return {}
        lider = min(uygun, key=lambda m: (-uygun[m], m))
        return {lider: 1.0}
