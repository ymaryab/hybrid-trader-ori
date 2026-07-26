# Edge Aile-Secici Mimari Tasarimi — 26 Tem 2026 (kullanici onayli)

Prensip: "Edge kanitlandiginda secim birimi motor degil, POLITIKA
AILESIdir." Bu dokuman ust duzey tasarimdir; kod/implementasyon
icermez. Referans zincir: q -> P(Path|q) arsivi -> simulator ->
Edge -> Allocation (docs/edge_mimari_iskeleti.md).

## 1. Girdi katmanlari

- A. Akis/rejim OLCUMLERI: lansman_1h, havuz_1h, EKG tetik hizi,
  SOL h1, gunun saati. Edge rejim TAHMIN ETMEZ (Faz 0 dersi); olculmus
  akisi kullanir.
- B. Aile-kosullu turetilmis edge (kalp): son N saatin yogun arsiv
  yollari uzerinde her ailenin payoff fonksiyoneliyle replay DAGILIMI.
- C. Gerceklesen kisa-pencere aile PnL (MTM dahil): birincil degil
  DOGRULAMA CAPRAZI.
- D. Golge/veto gecmisi: kararlarin izleyen-pencere isabeti
  (veto_degeri serisi) -> guven katsayisi. Seri biriktikce devreye.
- E. (sartli) q token ozellikleri: YALNIZ kill bataryasi gecerse.
- F. Operasyonel saglik: feed/RPC durumu, sadakat olcumu; saglik
  dusukken karar CASH'e meyleder.

Zorunlu cekirdek: B + A-cekirdegi + C + F. Opsiyonel/sartli: D, E,
volatilite etiketi (yalniz etkilesim kaniti gelirse).

## 2. Karar akisi

    GIRDI: aile katalogu F={SCALP, RUNNER, ..., CASH} + pencere verileri
    1 UYGUNLUK   min veri + ailenin on-kayitli "calismaz-rejim" beyani
                 + operasyonel engeller. CASH her zaman uygun.
    2 EDGE       uygun ailelere son-N-saat replay dagilimi
                 (medyan, kuyruk, n, belirsizlik).
    3 CAPRAZ     gerceklesen kisa-pencere PnL ile isaret uyumu;
                 celiski -> belirsizlik cezasi.
    4 SKOR       risk-ayarli ALT GUVEN SINIRI (LCB). CASH skoru sabit 0:
                 herkes LCB'de 0 altindaysa CASH kazanir (veto dogal
                 birinci-sinif sonuc).
    5 KARARLILIK histerezis (marj + iki ardisik tur teyidi); gecis
                 maliyeti (tasfiye+kayma) skordan dusulur.
    6 GUVEN      f(veri hacmi, capraz uyum, kalibrasyon isabeti).
    7 FALLBACK   guven dusuk -> mevcutta kal; veri sagligi bozuk ->
                 CASH; Edge olu -> legacy secici; hepsi olu -> salter.
                 Karari HANGI katmanin verdigi loglanir.
    CIKTI: aile dagilimi + tum girdi anlik goruntusuyla gerekce kaydi
           (omurga; replay edilebilir).

## 3. Secim prensipleri

- Beklenen deger degil ALT SINIR (LCB): az veriyle parlayani frenler
  (AUC 0.765'in bir gunde sonmesi ampirik gerekce).
- Gecis maliyeti icsellestirilir; "birazcik daha iyi"ye gecis caydirilir.
- Kararlilik > mikro-optimallik: histerezis sart (golgenin ilk gun
  r1<->r2 salinimi ciplak argmax'in kaniti).
- Rejim uyumu DEKLARATIF: ailenin on-kayit beyani uygunluk filtresi.
- Risk/drawdown SKOR degil KISIT: aile basina kayip limitleri ayri
  katman (kill-switch mantiginin aile duzeyi).
- Carpiklik bilinci: skor fonksiyoneli aile tipine gore on-kayitli
  (runner icin medyan + kuyruk payi; ortalama korlugu yasak).
- CASH tabani: her karsilastirma sifira karsi.

## 4. Cikti bicimi: dagilim sozlesmesi

Edge her turda aile DAGILIMI + belirsizlik uretir (orn. SCALP .4,
RUNNER .35, CASH .25). Allocation v1 bunu "argmax + histerezis + CASH
tabani" ile TEKE indirger (kucuk kasa, cakisma/atif sorunlari, sahte
hassasiyet). Kasa/kanit buyuyunce yalniz Allocation politikasi degisir;
Edge sozlesmesi sabit kalir.

## 5. Genisletilebilirlik: katalog sozlesmesi

Aile Edge'e KOD degil DEKLARASYON olarak eklenir:
  payoff fonksiyoneli (simulator girdisi) + uygunluk beyanlari
  (calismaz-rejim, min veri) + skor fonksiyoneli tipi + risk
  kisitlari + on-kayit referansi.
Edge cekirdegi aile-agnostiktir. Iki sart: (a) ORTAK SKOR BIRIMI:
pencere basina USD-normalize edge; (b) kulucka entegrasyonu: aday aile
katalogda "golge uye" (skorlanir, secilemez), terfi kullanici karari.
Yeni aile = katalog kaydi + on-kayit dosyasi; cekirdek degismez.

## 6. Riskler, yanlis varsayim adaylari, borclar (acik kayit)

1. SAG-KESILME SIZINTISI: son-N-saat replay'inde tamamlanmamis yollar;
   katmak bias, dislamak gecikme. Censoring kurali ON-KAYITLI olmali.
2. KUCUK-N REJIM DONUSLERI: LCB frenler ama Edge donuslerde yapisal
   GEC kalir. "Edge momentumdan iyidir" varsayimi golge kiyasinin
   izleyen-pencere karar-kalitesi metrigiyle TEST edilmeden kabul
   edilmez.
3. KENDINE-REFERANS: kalibrasyon yalniz KARSI-olgusal pencere
   olcumuyle (secilmeyenler de olculur); secilenin performansiyla
   kalibrasyon yasak.
4. TAKSONOMI KEYFILIGI: aile-ICI varyant varyansi aile-ARASI varyansa
   yaklasirsa taksonomi yeniden acilir (olculebilir tetik).
5. CASH CEKIM MERKEZI: veto olcumunun iki yuzu de raporlanir:
   kacinilan zarar VE kacirilan kazanc.
6. PARAMETRE YAMASI BORCU: Edge parametreleri (marj, histerezis, LCB
   katsayisi) on-kayit + gerekceli degisiklik kaydi disiplinine tabi.
7. TEK OMURGA BAGIMLILIGI: fallback merdiveni var ama hic tatbik
   edilmedi; bilinçli ariza tatbikati (kaos testi) ileri borc.
8. PENCERE UZUNLUGU VARSAYIMI: onceden ilan edilmis 2-3 pencere adayi
   + holdout ile TEK seferlik secim; serbest tarama yasak.
