# hibrit-trader Politika Dokumani

(tarih: 2026-06-22. Rakamlar in-sample snapshot, ~112 trade paper. Cok-rejim onayi bekliyor.)

## Mevcut Durum

net -%8.7, PF 0.98, kronik basa-bas-alti, paper.

## A) Kaybeden Kisimlar

- flash -1525 (baskin kanama, gap-down, cikisla kesilemez)
- friction ~%98 (TODO: dolar olarak olc, flash -1525 ile sirala, #1 kaldirac olabilir)
- <2dk churn -744 (hizli giris-cikis, round-trip maliyeti edge'i yiyor)

## B) Olu Agirlik

- giris alfasi r~0
- yuksek-skor -0.291 (NOT: r~0 degil, hafif anti-predictive, skor aktif zararli olabilir, kesif gerek)
- scratch-grace (:228) -53

## C) Calisan / Dokunma

- cikis motoru +2018 (gercek edge, tartisma disi)
- C1 (genesis yari-boyut)
- guvenlik filtresi (holder vb sert filtre)

## 6 Ilke

1. Giris-alfasini dondur.
2. Scale-in: KOSULLU. Exit-log runner-zaman-profili runner'larin yavas oldugunu dogrularsa uygulanir, yoksa hayir. Su an hipotez, doktrin degil.
3. Friction-kesimi: az ve uzun-tutulan trade, churn'u kes.
4. Ucuz stop'lar additif degil, RESIDUAL etkiyi olc (flash gap cogu cikisi deler).
5. Cikisa dokunma.
6. Tek-degisken + cok-rejim. Durust tavan PF 1.0-1.3.

## KILL-CRITERION

- Levereler sonrasi PF < 1.0 ise CANLI YOK.
- Canli bari: cok-rejim PF > 1.1 istikrarli (en az N trade, M ayri rejim uzerinde; TODO: N ve M tanimla) VE friction modellenmis.

## Disiplin

Forward gercektir, in-sample yon verir. Kucuk ornekleme guvenme. Kasa gecikmeli yer gercegi. Her degisiklik tek basina forward'da dogrulanir.

## Friction Sadakat Bulgusu (2026-06-22, read-only backfill)

Olcum duzeltmesi: paper fill slippage'i %0.38 modelliyor; botun kendi canli quote estimator'u ~%5.27. Paper PnL friction-kor. A) bolumundeki "friction TODO" boylece cevaplandi. PAPER_SLIPPAGE_PCT knob eklendi (local commit, ileriye donuk, geri alinabilir). Mevcut ~120 trade %5.27'ye analitik backfill edildi (brut = net + uygulanan per-trade friction, sonra yeniden uygula).

%5.27 friction sonuclari:
- Toplam derin negatif, PF 0.10. Paper'daki PF ~0.7-0.98 friction-korlukten. KILL-CRITERION (PF < 1.0) kesin tetik.
- Friction-invariant kaybedenler (her ayarda buyuyerek): <2dk churn, pump_fun sinif, kucuk-move (mfe <%15 = trade'lerin %78'i, PF 0.00).
- Tek yasayan segment: buyuk-runner (mfe >=%15) PF 6.06. Cikis motoru kazanan dolarin %99'unu runner'lardan aliyor (motor saglam, sorun girise dair).
- Holder filtresi: dolar edge robust pozitif (+255 -> +727) ama filtreli kitap mutlak hala ZARAR; kayip-azaltici, kar-yapici degil.

Runner tahmini (giris-ani ayrac):
- VAR: moonshot (Cohen d +0.95), dusuk-likidite (-0.67), genc-yas (-0.66). Skor ayirmiyor (d +0.12, teyit).
- Ama YETMIYOR: en iyi alt-kume (moon>=65 & age<=12h & dusuk-liq) PF 0.32, runner %54. Hicbir esik/kombo PF > 1.0 gecmiyor. 0.32 ile 1.0 arasi ~3x kapanmayan bosluk.

Yapisal tuzak: runner = dusuk-likidite; dusuk-likidite = yuksek-friction. Runner'i yaratan sey friction'i olduren seyle ayni. Ayni anda yuksek-frekans + dusuk-friction olamiyor.

Sonuc: gercekci friction'da bu konsept (yuksek-frekans pump_fun runner-avi) mevcut veride hicbir giris seciminiyle pozitif beklentiye ulasmiyor. Eksik olan bir filtre degil, yapinin kendisi. Gercek kaldiraclar: (a) gercek execution friction'ini olc (%5.27 botun tahmini, tek sayi degil), (b) dusuk-frekans + yuksek-likidite pair'lere kay, ya da yuksek-frekans pump_fun'dan vazgec.

## V-serisi final (05 Tem 2026, rakamlar 06 Tem 01:45 kesimiyle yeniden hesaplandi)

Tum rakamlar data/*_trades.jsonl'den, kesim noktasi karar commit'i a8da524 (2026-07-06 01:45 +03). Pencere/tanim her maddede belirtilir.

- Fren (v7 stop_felaket, -%10 aninda sat): 15 tetik, fren islemlerinin kendi neti -$346.3 (v7 tum omru, dogum 04 Tem 23:30 +03 .. kesim). Ikiz test (ayni token, +-2 saat, v6'da karsiligi olan 12/15 tetik): v7 tarafi -$289.8, v6 ikiz tarafi -$286.1, fark -$3.7. Ayni pencerede toplam ikiz kiyas: v6 48 islem -$73.7, v7 58 islem -$96.1, fark -$22.4. Sonuc: fren notr ile hafif negatif arasi, "kanitlandi" degil; kayiplari erken realize ediyor ama kurtarma sansini da kesiyor.
- Tavansizlik faturasi (golge, h1 ust siniri yok): golge'nin h1>50 girisleri 8 islem net -$160.4 (pencere: v6 dogumu 04 Tem 18:15 +03 .. kesim; tanim: chg_h1 > 50 olan girisler, v6'nin reddedip golge'nin aldigi kume). Ayni pencerede toplam: golge 68 islem -$318.0, v6 ikiz 71 islem -$91.0, fark -$227. Tavansizligin dogrudan faturasi -$160, dolayli toplam fark -$227.
- 20dk mutlak tavan (v8): timeout_20 cikislari 8 islem net -$88.1, 8'in 7'si zararla kapandi (v8 tum omru .. kesim, toplam 11 islem -$54.2). Sonuc: tavan curudu, zarar realize makinesi.
- Ortak kaybettiren parametre sol_h1 0..0.5 bandi: sol_h1 kaydi olan motorlarda bu banttaki girisler toplam 41 islem net -$136.6 (v6: 22/-$44.7, v7: 16/-$48.1, v8: 3/-$43.8; pencere: her motorun kendi omru .. kesim). v7 rejim esigi bu kanitla 0.5'e cekildi.
- Aktif filo: v4 / v7 / v9 / v10 / X1. Durdurulan: golge, v6, v8 (state/trades korunur, panel arsivinde).

## Sentez 06 Tem

Kanit major golu gosteriyor (memecoin ~2900 islem -$2243, islem basi -$0.77; major ilk gun +$60). M2 birincil dogrulama adayi. Dogrulama bari: 100+ islem, 5-7 gun, en az bir sol_h1 negatif gun, slot-kilit senaryosu gozlenmis olacak. Kill: islem/gun < 3 ya da 48+ saat slot kilidi = tasarim sorgulanir. M2'ye timeout EKLENMEZ, saflik korunur.

Uygulama (2026-07-09): v4, v9, v10 durduruldu (ENABLED=0, state/trades korunur, panel arsivinde). Aktif filo: M1, M2, v7, X1 (+EKG kaydedici).

v10 KINS vakasi 07 Tem: tp-only sistemlerde realized karne sansurludur (kaybedenler kapanmaz, defterde gorunmez; KINS 3+ gun -%43 acik, realized 4/4 yesilken MTM -$50). M2 dogrulamasi realized ile DEGIL, mark-to-market equity + slot yasi ile okunacak.

09 Tem: v6 guclendirilmis haliyle (rejim 0.5 + hizli goz) yeniden aktif. Filo reset: 5 motor esit $1000, adil yaris. Onceki veriler backup_reset_20260709'da. M2 dogrulama penceresi resetten itibaren yeniden: 100+ islem, 5-7 gun, bir kotu SOL gunu, MTM + slot yasi gozlugu.

## Veri arizasi dersi: ORCA vakasi (09 Tem 2026)

DexScreener, ORCA'nin en likit havuzlarinda (ORCA/JUP $24M, ORCA/RAY, ORCA/MET vb) priceUsd'yi ~5000-6000x sapik basti (gercek ~$1.2 iken ~$6013). M1 girisleri bu sapik fiyattan acildi; fiyat tek adimda 6013 -> 1.22'ye "dusunce" M1 stop_felaket ile -$199.6 realize etti, M2'nin acik ORCA slotu mae -%99.98 gordu. Bu kayiplar STRATEJI KAYBI DEGIL, VERI ARIZASIDIR. State bilerek duzeltilmedi, kayitlar duruyor: M1 realized icindeki -$199.6 ve M2'nin acik ORCA pozisyonu bu notla okunmali; M2 MTM'i fiyat kaynagi duzelirse toparlar. Ayni gun ikinci ariza: GeckoTerminal sol_h1 fetch'i basarisizken rejim kapisi fail-open oldugu icin 23 giris rejim-korlugunde atildi.

Dersler ve alinan onlemler:
- Fiyat kaynagina kor guven yok. Tum motorlarda fiyat sanity bandi (price_sanity.guard_price): tek adimda 5x+ sapma = veri arizasi; tick yok sayilir, islem tetiklenmez, degerleme son gecerli fiyatla surer, ariza loglanir. Sapma 5dk kesintisiz surerse yeni seviye taban kabul (motor sonsuza dek kor kalmaz).
- Likiditeye kor guven de yok. Evren tazelemede havuz secimi stabil-kota referansli (_best_sane_pool): USDC/USDT havuzlarinin medyan fiyatindan 5x+ sapan havuz evrene giremez. ORCA'yi sokan sey "en likit havuz" kuralinin sorgusuzluguydu.
- ORCA evrenden cikarildi. Saglikli fiyat basan en buyuk havuzu $712k (< $3M evren esigi), ikame havuz yok; kaynak duzelirse gunluk tazeleme dogal olarak geri alir.
- Rejim fail-closed: sol_h1 alinamazsa giris kapisi KAPALI (eski davranis: filtre atlanirdi). Son basarili deger 10dk'ya kadar gecerli, sonrasi veri yoksa giris yok. Veri kaybi "serbest gecis" degil "dur" demektir.

## Veri arizasi dersi 2: DexScreener MET/JUP kotali havuz carpan arizasi (09 Tem 2026 aksam)

Golge olcum (broker katmani, dryrun_fills.jsonl) devreye girdigi ilk iki kayitta yakaladi: M1'in JTO ve PYTH alislari, Jupiter'in gercek yurutulebilir fiyatindan ~5000x yukarida (fark_bps -9998). Canli dogrulama: DexScreener, MET/JUP kotali havuzlarda priceUsd'yi ~5000x sisik basiyor (JTO/MET 3335 vs gercek 0.64; PYTH/JUP 214.9 vs gercek 0.043; PUMP/MET, BONK/MET ayni). ORCA vakasiyla ayni imza. Stabil-kota medyan referansi burada YETMEDI: JTO/PYTH'nin USDC/USDT havuzu yok, fallback "tum havuzlarin medyani" cogunluk bogus olunca sapik havuzu secti.

Veri-arizasi iptali: DexScreener MET/JUP kotali havuz carpan arizasi, 3 pozisyon veri-arizasi iptali (JTO ve PYTH M1, PYTH M2), strateji kaybi degil. Dorduncu hedef (Bonk M1) iptal penceresinden once timeout_90 ile kendisi kapandi (-$1.25; carpan giris-cikis boyunca tutarli oldugu icin goreli PnL gercekciydi, sahte kayip olusmadi; kayit ORCA emsaline uygun olarak duruyor). Iptal disiplini: state yedeklendi (data/backup_iptal_20260709/), iptal edilenler tum detayla data/iptal_veri_ariza.jsonl'a yazildi, bakiyeye acilis maliyeti (cost + gas) iade edildi (hic acilmamis gibi), trades'e YAZILMADI (karne temiz), cooldown uygulanmadi. Eski ORCA kaydi ders olarak aynen duruyor.

Dersler ve alinan onlemler:
- Tek guvenilir fiyat referansi YURUTULEBILIR fiyattir (Jupiter quote). Evren tazelemede Jupiter hakem zorunlu (fail-closed): hakem cevap vermezse token evrene giremez; havuz secimi hakem referansindan 5x icinde olmali.
- Giris kapisinda veri_ariza reddi: tarama fiyati evrendeki hakem referansindan 5x+ sapiyorsa giris yok.
- Re-base hakem onayina bagli: guard_price'ta 5dk kalici sapma artik otomatik taban degistirmez; Jupiter hakem yeni fiyati onaylamazsa degerleme son gecerli fiyatta kalir, pencere bastan baslar. Onceki tasarimda kalici bogus carpan 5dk sonra taban olur, carpan duzelince sahte -%99 kayip realize olurdu.
- Golge olcum ayni zamanda veri-guvenilirlik sensorudur: paper fiyat ile Jupiter quote farki buyukse (ornegin |fark_bps| > 3000) once veri arizasi suphesi.
