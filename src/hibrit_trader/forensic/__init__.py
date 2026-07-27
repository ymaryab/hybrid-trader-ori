"""Forensic Factory (28 Tem): karliligi bozan az sayida islemin ortak
imzasini cikaran, salt-okur analiz altyapisi.

Kapsam disi (bilerek): motor yazmak, giris/cikis mantigina dokunmak,
yurutme analizi. Fabrika hicbir sey degistirmez, yalniz betimler.

Akis:
    veri.yukle()  ->  kohort.uygula()  ->  karsilastir.imza()  ->  rapor

Genisletme noktalari:
    kohort.kaydet(...)    yeni kohort secici
    ozellik.kaydet(...)   yeni ozellik (zaman ve alan beyani ZORUNLU)
"""

from . import karsilastir, kohort, ozellik, rapor, veri  # noqa: F401

__all__ = ["veri", "kohort", "ozellik", "karsilastir", "rapor"]
