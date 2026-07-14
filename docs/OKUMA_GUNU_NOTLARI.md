# OKUMA GUNU NOTLARI (hedef: 2026-07-20 civari, tek oturum)

Hazirlayan: CC, 2026-07-13. Tum olcumler read-only; karar ve uygulama ONAYLA.

## Gundem (onem sirasina gore)

1. Rejim esigi kalibrasyonu: golge defter okumasi (esik kovalari 0.2 / 0.35 / 0.4)
   KOVA KAYNAGI NOTU (14 Tem, cift ayar sonrasi): v7 kapisi 0.35'e indigi icin
   rejim_reject satirlari artik yalniz sol_h1 < 0.35'te yaziliyor. Golge defter
   fiilen sadece 0.2-0.35 bandini olcer; 0.35 ve 0.4 kovalari v7'nin KENDI
   islemlerinden (v7_trades.jsonl, sol_chg_h1 alani) okunmali. Karsilastirma:
   0.2-0.35 golge sanal PnL vs 0.35-0.5 gercek v7 PnL vs 0.5+ gercek v7 PnL.
2. h1 band-skip karari (20-40 bandi): bootstrap guven araligi + m5 ayristirmasi, n>=40 sart (v6 verisi havuza dahil, ayni giris kurallari)
   DILIM BAZLI ANALIZ SARTI (14 Tem): h1_bant_skip kayitlari 20-25 / 25-30 /
   30-35 / 35-40 dilimlerine ayrilsin. Her elenen icin UC ZAMAN UFKU: 15 dk /
   30 dk / 60 dk; her ufukta tavan%, taban%, kapanis% + v7 kurallariyla sanal
   sonuc (tp+2 / -10 felaket / 30dk sonrasi -2 / 60dk tavan). Fiyat yolu:
   kosucu_ekg.jsonl, yoksa GT dakikalik mum. Acikca cevaplanacak soru: bant
   cok mu genis, sinirlar kaydirilmali mi (orn 25-40 veya 20-35)? Ufuklar
   arasi fark yorumlansin: kisa vadede kazanip uzun ufukta coken dilim var mi
   (varsa o dilim v7'nin 60dk penceresi icin dogru elemedir, tersi ise yanlis).
   ORNEK VAKA - LEVI (14 Tem 00:48-01:18 UTC): h1 25.7, sol_h1 0.508, bantta
   elendi; iki skip kaydi arasinda fiyat 0.0013734 -> 0.0016763 (+%22 kostu).
   Eski kural (bant yok, rejim 0.5) bu girisi alir ve buyuk olasilikla tp+2
   kapatirdi. Tekil vaka bandi aklamaz (retro n17 -$85) ama 25-30 diliminin
   ayri olculmesi gerektiginin kanitidir.
3. Pahali tekrar-binis kurali (onceki cikisin +%5 ustunde girme?)
4. Grace ici -5 freni: golge kayitlarindaki taban_pct ile offline A/B (asagida retro sonuc)
5. Not: v7 felaket freni icin fast_price kadansi (canli motor koduna dokunur, ayri onay ister)
6. Park: X1 stop/rug sorunu (v7 gundemi disi), v7c canli kablolama (sicil + ayri esik tasarimi on-sartli)

## BOS ZAMAN BOLUSUMU OLCUMU (2026-07-13, GT saatlik SOL OHLCV ile)

Soru: canli kasanin bos oturmasinin kaynagi rejim kapisi mi, aday kitligi mi?

V7 PAPER penceresi (ilk islem 2026-07-09 -> 13 Tem, 107.9 saat):
- rejim ACIK oran: %6.4 (7/109 saat)
- bos zaman bolusumu: rejim kapali 93.6 puan | acik ama islemsiz 1.6 puan | islemde 4.8 puan
- ACIK saat ici doluluk: %74.7 (rejim acikken motor zaten calisiyor)

V7 CANLI penceresi (kilit acilisi -> 13 Tem, 23.5 saat):
- rejim ACIK oran: %8.3 (2/24 saat)
- bolusum: rejim kapali 91.7 | acik ama islemsiz 6.9 | islemde 1.4

Dogrulama: 224 rejim_reject satirinin 215'i (%96) kapali saatlere dusuyor (tutarli).

Yontem uyarisi: seri saat-kapanisi bazli; motor ise yuvarlanan h1 + 1 saatlik cache goruyor.
25 acilisin sadece 3'u benim saat gridimde "acik" gorunuyor, yani acik pencereler cogunlukla
saat-alti kisa spike'lar ve gercek acik oran %6-15 araliginda olabilir. Sonuc yine de saglam:
bos zamanin ezici cogunlugu (%85-94) REJIM KAPALI kaynakli, aday kitligi degil.

KARAR ETKISI:
- Evren genisletme (v7c, ayni sol_h1 kapisi) yalniz "acik ama islemsiz" dilimine (1.6-6.9 puan) etki eder.
- Esik kalibrasyonu "rejim kapali" blokuna (91.7-93.6 puan) etki eder. Kaldirac acik ara ile esikte.
- Golge defter negatif cikarsa esik kapanir; o zaman tek buyuyecek yol evren + dakikalik veri (1b) olur.
- Yan bulgu: kisa pencereler + saatlik cache, bazi girislerin pencere fiilen kapandiktan sonra
  gerceklestigini ima ediyor; dakikalik sol_h1 (ucretli veri) bu kaymayi da duzeltir.

## RETRO SONUCLAR (okuma gununde tekrar dogrulanacak)

- Grace ici -5 freni: V6+V7'de mae<=-5 goren 20 islem, gercek toplam -82 puan; hepsi -5'te
  kesilseydi -100 puan. Fren -18 puan ALEYHTE (7/20 islem toparlayip kazandi, +14.8'e kadar).
  Golge defterde taban_pct ile ayni analiz bedava tekrarlanir; ikinci defter kodu gerekmez
  (pozisyon ilk tetikte kapandigi icin taban ekstremleri kapanistan once yasanir, sira belirsizligi yok).
- Cikis kurali: tp+2 aklandi. 62 tp isleminde kacan kuyruk toplam 6.4 puan; -1 puan trail
  simulasyonu -55.6 puan kaybettirirdi. V7 cikisina DOKUNMA.
- h1 kovalari (V6+V7, 13 Tem itibariyla): 10-15: n39 +$168 | 15-20: n14 +$122 (win %100) |
  20-25: n5 -$21 | 25-30: n1 +$4 | 30-40: n11 -$64 | 40-60: n10 +$27 (win %90).
  Dogru aday BAND-SKIP (20-40), tavan indirme DEGIL (40+ pozitif).
- Pahali binis (V6+V7): ucuz/esit n35 ort +2.24% (+$163) | 0-5 pahali n14 ort +0.09% |
  5-15 pahali n11 ort -0.97% (-$22) | 15+ n5 ort +1.7%.
- X1 liq tabani karsi-olgusal: >=50k -$241, >=75k -$32, >=100k -$93. Hicbiri artiya cevirmiyor
  (kazanan medyan liq 28.8k, ayni havuzda). "Tek satir tamir" iddiasi yanlislandi; dogru aday
  fast_price stop kadansi veya X1 kucultme. PARK.

## OKUMA GUNU KONTROL LISTESI

- [ ] golge defter ozet: .venv/bin/python -m hibrit_trader.golge_rejim_disi ozet (sunucuda)
- [ ] esik kovalari pnl karsilastirma (0.2 / 0.35 / 0.4) + kayit sayisi yeterli mi (hedef 30+)
- [ ] h1 band bootstrap: n>=40 kontrol, m5 etkilesim ayristirmasi
- [ ] golge taban_pct ile -5 fren offline A/B tekrari
- [ ] tekrar-binis kurali karari
- [ ] bos zaman bolusumu guncelle (ayni script, taze OHLCV)
- [ ] fast_price fren kadansi tartismasi (istenirse ayri onayla is)
- [ ] gunluk kesici gecikmesi (14 Tem): limit -20 iken sayac -48.69'da durdu.
      Mekanizma gecikmesi analiz edilsin: limit yalniz YENI girisi kesiyor,
      acik pozisyonlarin kapanis zararlari limit asildiktan sonra da sayaca
      ekleniyor; es-zamanli dolu slotlar tek turda toplu zarar yazabiliyor.
      Soru: kesici acik pozisyonlara uzansin mi, yoksa slot/es-zamanlilik mi
      sinirlansin? (Yeni PCT kesici de ayni giris-kesme mekanigi, asma payi
      orada da gecerli.)
