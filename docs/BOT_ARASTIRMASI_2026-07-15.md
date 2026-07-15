# EN IYI 3 BOT ARASTIRMASI (read-only kesif)

**Tarih:** 2026-07-15 UTC
**Kapsam:** Bastan bugune TUM veri (2026-06-21 - 2026-07-15, ~24 gun)
**Amac:** Verinin isaret ettigi en yuksek beklentili 3 bot konfigurasyonu
**Uygulama:** YOK. Kesif + simulasyon. Kod/motor/env dokunulmadi.

---

## 1) VERI ENVANTERI

### Trade dosyalari (birlesik: 1406 trade)

| motor | dosya | n | ilk | son | gun | not |
|---|---|---|---|---|---|---|
| MOM | momentum_trades | 314 | 07-01 21:08 | 07-04 11:37 | 2.6 | ilk canli momentum |
| GLG | golge_trades | 135 | 07-02 17:54 | 07-05 22:37 | 3.2 | shadow sim |
| V3 | v3_trades | 81 | 07-02 17:37 | 07-04 11:50 | 1.8 | arsiv |
| V4 | v4_trades | 204 | 07-02 19:25 | 07-08 23:12 | 6.2 | arsiv |
| V5 | v5_trades | 57 | 07-03 12:44 | 07-04 11:56 | 1.0 | arsiv |
| V6 | v6_trades | 96 | 07-09 06:48 | 07-14 23:25 | 5.7 | aktif paper |
| V7 | v7_trades | 46 | 07-09 06:50 | 07-15 00:58 | 5.8 | **CANLI hat** |
| V7C | v7c_trades | 8 | 07-13 01:11 | 07-14 16:33 | 1.6 | major paper |
| V8 | v8_trades | 11 | 07-04 21:58 | 07-05 21:18 | 1.0 | arsiv |
| V9 | v9_trades | 13 | 07-04 21:56 | 07-07 19:35 | 2.9 | arsiv |
| V10 | v10_trades | 4 | 07-05 22:24 | 07-07 19:35 | 1.9 | arsiv |
| X1 | x1_trades | 417 | 07-09 06:39 | 07-15 03:10 | 5.9 | major aktif |
| M1 | m1_trades | 18 | 07-09 08:02 | 07-09 15:56 | 0.3 | kisa deneme |
| M2 | m2_trades | 2 | 07-09 10:20 | 07-09 12:31 | 0.1 | kisa deneme |

### Yardimci dosyalar

| dosya | n | pencere | not |
|---|---|---|---|
| momentum_rejects | 12191 | 07-01/07-15 (13.3g) | tum motorlarin reject kayitlari |
| kosucu_ekg | 294812 | 07-04/07-15 (10.2g) | fiyat yolu (488 pool) |
| attribution | 467 | 06-21/07-09 (17.8g) | eski attribution |
| decisions | 3260 | 06-21/07-09 (17.9g) | eski decision log |

### Donem cesitliligi (subjektif etiket)

- **07-01/07-04:** karisik piyasa, momentum ilk canli — orta boga
- **07-05/07-08:** genel yatay/geriye — arsiv motorlar (v4, v9) test
- **07-09/07-13:** boga fazi baslangici — v6/v7/x1 canli baslama, kayda deger trade cikti
- **07-14/07-15:** boga zayifladi, sik felaket — v7 canlida stop_gec kayiplari

Yani havuz "sadece boga" degil, karisik. Overfit riskini kismen sinirlar.

---

## 2) SINYAL MADENCILIGI (1406 trade uzerinde)

### h1 bandi (canli para ile tetiklenen alimlar)

| h1 kova | n | pnl | ort/tr | win% |
|---|---|---|---|---|
| <5 | 28 | -$206 | -$7.37 | %25 |
| 5-10 | 333 | -$678 | -$2.03 | %31 |
| **10-15** | **329** | **+$404** | **+$1.23** | **%51** |
| 15-20 | 84 | -$166 | -$1.98 | %51 |
| 20-25 | 61 | -$280 | -$4.59 | %44 |
| **25-30** | **28** | **+$69** | **+$2.46** | **%64** |
| 30-40 | 54 | -$277 | -$5.12 | %48 |
| 40-50 | 48 | -$156 | -$3.26 | %50 |
| 50-75 | 154 | -$419 | -$2.72 | %46 |
| 75+ | 287 | -$932 | -$3.25 | %50 |

**Karar:** yalniz **h1 = 10-15** ve **h1 = 25-30** pozitif. Digerleri hepsi negatif. 75+ agir toksik (zaten pumpalanmis). Mevcut motorlarin (v6/v7) 10-50 bandi cok genis; 10-15'e daraltmak ana bulgu.

### h1 x m5 kesitli (moment yonu ile birlesim)

| h1 | m5 yonu | n | pnl | ort/tr |
|---|---|---|---|---|
| **10-15** | **m5+** | **282** | **+$436** | **+$1.55** |
| 10-15 | m5- | 43 | +$4 | +$0.09 |
| 15-20 | m5+ | 62 | -$206 | -$3.32 |
| 20-25 | m5+ | 49 | -$199 | -$4.05 |
| **25-30** | **m5-** | **6** | **+$37** | **+$6.19** |
| 30-40 | m5+ | 40 | -$288 | -$7.19 |
| 50-75 | m5+ | 152 | -$423 | -$2.79 |

**Karar:** Yalniz **h1=10-15 & m5>0** anlamli pozitif havuz (n=282, ort +$1.55). 15+ bantlarda m5>0 (moment devam) toksik — piyasa tepelemek uzere sinyali.

### Likidite

| kova | n | pnl | ort | win% |
|---|---|---|---|---|
| <50k | 460 | -$400 | -$0.87 | %42 |
| 50-100k | 268 | -$547 | -$2.04 | %37 |
| 100-200k | 356 | -$702 | -$1.97 | %55 |
| 200-500k | 245 | -$325 | -$1.33 | %45 |
| 500k-1M | 46 | -$462 | -$10.04 | %48 |
| 3M+ | 30 | -$207 | -$6.90 | %27 |

**Karar:** Hicbir likidite bandi net pozitif degil, ama **100-200k** win rate %55 (nadir). Belki genis liq band'i kismen nedeni piyasa faz'i — likidite basli basina sinyal degil. Su anki v7 kapisi (>=100k) makul.

### Rejim (sol_h1)

| kova | n | pnl | ort | win% |
|---|---|---|---|---|
| negatif | 34 | -$94 | -$2.75 | %29 |
| 0-0.2 | 244 | -$505 | -$2.07 | %39 |
| 0.2-0.35 | 105 | -$77 | -$0.74 | %44 |
| 0.35-0.5 | 115 | -$82 | -$0.71 | %49 |
| 0.5-1 | 292 | -$401 | -$1.37 | %43 |
| **1+** | **109** | **+$262** | **+$2.41** | **%48** |
| ? | 507 | -$1746 | -$3.44 | %48 |

**Karar:** Yalniz sol_h1 **>= 1.0** net pozitif. Mevcut v7 esigi 0.35 cok gevsek. 0.5+ bile marjinal negatif. Sıkı rejim (>=1) sample'i cok kısıtlar (n=109/24g = ~4.5/g) ama net karli.

---

## 3) UC KONFIGURASYON

### AGRESIF (yuksek frekans, kucuk kayip erken)
- Evren: memecoin
- Giris: liq >= $100k, h1 10-50, m5 sınırsız, rejim >=0 (gevsek)
- Cikis: tp +2%, felaket -5%, late -2% grace 10dk, tavan 10dk
- Bilet carpani: 1.0 ($32/trade)
- **Beklenti:** cok islem, hizli tp, kucuk kayipla ciktikca yeni firsata dus

### SECICI (az ama nitelikli)
- Evren: memecoin
- Giris: liq >= $150k, **h1 10-20 (dar)**, **m5 > 0 zorunlu**, **rejim >= 0.5 sıkı**
- Cikis: **tp +2.5%** (yuksek), felaket -15%, late -2% grace 15dk, tavan 20dk
- Bilet carpani: **1.5** ($48/trade)
- **Beklenti:** az islem ama isabetli, buyuk pozisyon, kuyruk koruma gevsek

### MAJOR (dusuk makas, buyuk pozisyon)
- Evren: major (v7c/x1 verisi)
- Giris: liq >= $3M, h1 2-15, rejim >= 0.5
- Cikis: tp +1.5%, felaket -8%, late -1% grace 20dk, tavan 30dk
- Bilet carpani: 2.0 ($64/trade)
- **Beklenti:** major evrenin dusuk friction'i, dusuk hedef, buyuk boyut

---

## 4) BACKTEST SONUCLARI

Backtest metodolojisi:
- Havuz: 1406 trade motor tag'li birlesik
- Her konfigin giris filtresine uyanlar secildi (evren + likidite + h1 + m5 + sol_h1)
- Cikis: EKG fiyat yolu varsa tick tick, yoksa MFE/MAE proxy
- Bilet: cost_usd x bilet_carpan; friction sabit %0.2 (trades ort)

### Konfig performansi (24 gun)

| konfig | n_trade | toplam pnl | ort/tr | win rate | felaket kayip |
|---|---|---|---|---|---|
| **AGRESIF** | 150 | +$147.88 | +$0.99 | %54.0 | 27 fen -$582 |
| **SECICI** | 48 | **+$300.70** | **+$6.26** | **%66.7** | 1 fen -$62 |
| MAJOR | 8 | -$28.61 | -$3.58 | %12.5 | 0 fen |

### $120 baslangic kasasi senaryosu (24 gun, sabit bilet, compound YOK)

| konfig | trade/gun | pnl/gun | 24g toplam | ~ getiri |
|---|---|---|---|---|
| AGRESIF | 6.2 | +$5.78 | **+$138.64** | %116 |
| **SECICI** | 2.0 | +$7.83 | **+$187.94** | **%157** |
| MAJOR | 0.3 | -$0.56 | -$13.41 | -%11 |

### Kiyas: mevcut motorlarin gercek sonuclari

| motor | n | toplam | ort/tr | not |
|---|---|---|---|---|
| **V7 canli** | 46 | **-$21.67** | -$0.47 | son 5.8g, mevcut para |
| V6 paper | 96 | +$228.63 | +$2.38 | ayni pencere |
| **X1 major** | 417 | **-$371.80** | -$0.89 | major evren, buyuk kayip |
| AGRESIF (backtest) | 150 | +$147.88 | +$0.99 | — |
| **SECICI (backtest)** | 48 | **+$300.70** | **+$6.26** | — |
| MAJOR (backtest) | 8 | -$28.61 | -$3.58 | — |

**Kazanan: SECICI**
- Trade basi ort **+$6.26** (V6'nin 2.6x, V7'nin uzerinde 13x)
- Win rate **%66.7** (V7 %78 nominal ama net -$0.47/tr)
- Az islem ama sürekli pozitif; ana motoru "h1 10-20 & m5+ & rejim>=0.5" bulgusu

---

## 5) DURUSTLUK SERHI

### Overfit riski
- **SECICI** parametreleri sinyal madenciligindeki en gucli bulguya (h1 10-15 m5+) tam oturuyor. Bu bir tur "in-sample optimization" — ayni veriden hem sinyal cikardik hem geri test ettik. Out-of-sample'de %30-50 performance dususu beklenebilir.
- Onerilen out-of-sample test: Simdiden 2 hafta bekle, sonra sadece 2-15 Temmuz'daki backtest ile 16-31 Temmuz'daki simulasyonu kiyasla.

### Orneklem buyuklugu
- SECICI: 48 trade. Guclu sinyal ama t-test yapmadim. Standard error ~$0.90/tr; %95 CI ort ~[$4.5, $8.0]. Yani "belki $4/tr, belki $8/tr" — kesin degil.
- AGRESIF: 150 trade, daha guvenli sample.
- MAJOR: 8 trade, hicbir sey soylenemez. Sadece deger vermek icin dahil.

### Donem yanliligi
- Havuz 24 gun: 07-01/07-15. Bu donem `mixed bull-flat`. Ana boga pencereleri 07-09/07-13 aralikta yogunlasti (v6/v7/x1 canli baslama denk geldi).
- **Ayi periyodu YOK.** Boga isaretlerine asiri uyum var; ayi'da (BTC/SOL dusen trende) motorlarin nasil davraninacagi bu retrodan **bilinmiyor**.
- Zaman kesitli olcum: son 3 gun (07-13/07-15) rejim zayifladi, V7 canli o donemde -$22 verdi. Yeni konfiglar da benzer donemde `felaket` sikligi artabilir.

### Makas / friction hassasiyeti
- Backtest'te sabit %0.2 friction kullanildi (trades ort). Gercek slippage 0.055-0.40 arasi degisken. %0.5 friction yapsak SECICI +$300 -> +$220 civari duser (~%25 azalis). Yine pozitif ama fark kucululur.
- Priority fee, RPC failure, exec retry: bunlar backtest'te YOK. Gercek canli'da her trade %0.5 ek friction beklenir. SECICI dahi bunu absorbe edecek marjda; AGRESIF marj daralir.

### Guvenilirlik siniflari
| bulgu | guven |
|---|---|
| h1 10-15 m5+ pozitif | **yuksek** (n=282, tutarli) |
| h1 15-50 m5+ toksik | **yuksek** (n=343 farkli bantta hep negatif) |
| sol_h1 >= 1 net pozitif | **orta** (n=109, tek pencere) |
| SECICI +$187 24g toplam | **orta** (in-sample optimization) |
| MAJOR profil deger | **DUSUK** (n=8, X1 aktual -$372 negatif) |
| AGRESIF felaket -%5 dogru esik | **DUSUK** (retro'da felaket agresyonu -$582 kaybettirdi) |

---

## 6) TAVSIYE

1. **Simdi**: hicbir sey degistirme. SECICI'yi paper motoru olarak (v7d?) 2 hafta canli test et. Ozel adi/durumu netlese.
2. **V7 mevcut kural**: tam olarak SECICI'nin ozeti ile hizali degil (h1 10-50, m5 sart yok). Ancak son sweep -%15 felaket ile birkac gunluk retro'da hafif iyilesme gosterdi. Bu retro sonuclari bunu destekliyor: felaket agresiflestikçe kotu.
3. **X1 major**: aktual -$372 kayip. Ciddi sorgulanmali. Ya evren cok toksik, ya kurallari uyumsuz. Ayri sunulmalidir.
4. **AGRESIF** profil: felaket agresyonu (retro'da 27 tetik, -$582) uyari; -%5 esigi cok siki. Retro sweep bunu zaten gosteriyordu.

**Sonuc:** En yuksek beklentili konfig **SECICI** (h1 10-20, m5>0, rejim>=1, tp+2.5%, felaket -15%, bilet 1.5x). Ancak in-sample overfit riski var — 2 hafta out-of-sample paper testi kritik. Uygulanmaz, sadece kesif belgesi.
