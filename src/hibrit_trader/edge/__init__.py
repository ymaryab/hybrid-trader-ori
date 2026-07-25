"""EDGE MIMARI ISKELETI (HAT 2, 25 Tem 2026). OGRENEN MODEL YOK.

Zincir (tek referans teori, kullanici onayi 25 Tem):

    q (token + baglam)
        |
    P(Path | q)  ~= q-katmanli ampirik yol arsivi   [yol_arsivi]
        |
    kosullama: q -> katman                           [kosullama]
        |
    politika simulatoru: payoff_pi(yol)              [simulator]
        |
    Edge(q, pi) = katman ici replay ortalamasi       [edge_motoru]
        |
    tahsis                                           [tahsis]

KAPI SORUSU: her yeni fikir bu zincirin hangi halkasini guclendiriyor?
Hicbirini guclendirmiyorsa girmez.

DONDURULMUS (kill-bataryasi sonucuna kadar, on-kayit):
  - yeni sensor, yeni teori, yeni latent degisken,
  - yeni hedef fonksiyonu, yeni OGRENEN model.
Serbest: refactoring, arayuz, dataset formati, replay/simulator/tahsis API.

Kill kriteri gecerse SADECE kosullama moduluna gercek model takilir;
gecmezse iskelet yine kalir (arsiv + simulator + tahsis kalici kazanim).
"""
