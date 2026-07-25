# Edge Mimari Iskeleti (HAT 2) — 25 Tem 2026

## Uc hat karari (kullanici, 25 Tem)

- **HAT 1 (Arastirma, DONMUS):** Sprint 2 sensorleri veri toplar; hipotez,
  kill kriterleri ve batarya AYNEN durur. Hicbir mudahale yok.
- **HAT 2 (Mimari refactoring, BASLADI):** yeni zincirin iskeleti bugunden
  kurulur; ogrenen model YOK, arayuzler ve dummy v1'ler var.
- **HAT 3 (Operasyonel borc, PARALEL):** boot guvenilirligi, recovery,
  mutabakat, watchdog. Arastirma degildir; canli davranisi degistiren
  her kalem tek tek onayla yayina alinir.

**Dondurulan:** yeni sensor, yeni teori, yeni latent degisken, yeni hedef
fonksiyonu, yeni ogrenen model.
**Serbest:** refactoring, katman ayrimi, arayuz tasarimi, dataset formati,
replay/simulator/edge/tahsis API.

**Kapi sorusu:** her yeni fikir "zincirin hangi halkasini guclendiriyor?"
sorusuna cevap vermek zorunda. Cevabi yoksa girmez.

## Zincir ve modul haritasi

```
q (token + baglam)                    Sprint 2 sensorleri (HAT 1, ayri)
    |
P(Path | q) ~ ampirik yol arsivi      edge/yol_arsivi.py   (salt-okur)
    |
q -> katman                           edge/kosullama.py    (v1: TekKatman = H0)
    |
payoff_pi(yol)                        edge/simulator.py    (saf fonksiyon)
    |
Edge(q, pi) = katman ici replay ort.  edge/edge_motoru.py  (turetilir, ogrenmez)
    |
tahsis (paylar toplami 1.0)           edge/tahsis.py       (v1: HepsiLidere)
```

## Ilkeler

1. **Piyasa modellenmez.** Yol dagilimi uretilmez, tahmin edilmez;
   arsivdeki gercek yollar dagilimin kendisidir. Generative/sequence/
   diffusion sinifi hicbir sey gundemde degildir.
2. **Ogrenilecek tek sey** q -> katman eslemesi (dogru komsuyu bulmak);
   o da YALNIZ kill-bataryasi gecerse `kosullama` modulune takilir.
3. **Edge ogrenilmez, turetilir:** arsiv x simulator kompozisyonu.
4. **Ham-veri ilkesi:** turevler diske yazilmaz, okuma aninda hesaplanir.
5. **Motor donuk:** paket cevrimdisi ve salt-okurdur; canli karar akisi
   kill-bataryasi sonucuna kadar mevcut otonom secicide kalir
   (`edge_motoru.secici_koprusu` v1 koprusu).

## Sadakat siniri (her edge raporunda tekrarlanir)

EKG yollari dakika-cozunurluklu ve tetik-kosullu (secilim etkisi);
tick arasi hareket, kayma ve ucret modellenmez. Edge tahmini hatasi =
kosullama hatasi + simulator sadakat hatasi; ikisi ayri raporlanir.

## Edge Engine'in nihai rolu (REFERANS TANIM, kullanici onayi 25 Tem)

Edge Engine motorlarin yerine gecmez; onlari FIYATLAYAN katmandir:
"su kosulda su politikanin beklenen avantaji nedir" cevabini uretir,
karar bu fiyatin uzerine kurulur. Uc ufuk:

1. **Bugun (golge):** rolu yok; mevcut seciciyle ayni girdilerle kiyas
   aynasi (EdgeShadowEvaluated).
2. **Orta vade (kill bataryasi GECERSE):** motorlar korunur; secici
   cekirdegi geriye-bakan momentumdan ("son 30dk kim kazandi") ileriye
   bakan kosul-temelli tahsise ("q verildiginde hangi politikanin
   edge'i pozitif") gecer; kesirli tahsis ayni arayuzden.
3. **Uzun vade (YENI kanitla, ayri kullanici karari):** karar taneciği
   motordan FIRSATA iner: aday basina politika fiyatlanir ve atanir;
   motor kimligi politika kutuphanesine donusur, hicbir sey silinmez.

Degismezler: edge OGRENILMEZ (arsiv x simulator turetimi; ogrenen tek
parca kosullama, sinav sartli); Edge Engine yurutmeyi ASLA devralmaz;
broker/kill-switch/LIVE_ONAY/senkron katmanlari her zaman ustundedir.
Batarya kalirsa terfi yok: golge olcum araci kalir veya kapanir.

## Sprint 2 sonu hedefi

Calisan yeni mimari + eksik TEK parca: kosullama modeli.
- Kill kriterleri GECERSE: yalniz `kosullama.py`'ye gercek katmanlayici
  takilir, baska hicbir modul degismez.
- GECMEZSE: iskelet kalir (arsiv, replay, simulator, tahsis kalici
  kazanim); edge-siniflandirma programi on-kayit geregi kapanir.

## Test

`tests/test_edge_iskelet.py` (5 test): arsiv okuma/eleme, TP/stop/timeout,
runner trail, tek-katman edge, kazanan-hepsini-alir tahsis.
