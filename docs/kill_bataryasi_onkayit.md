# Kill Bataryasi ON-KAYIT (Sprint 2) — 25 Tem 2026

Bu dosya SINAVDAN ONCE yazildi ve sinav gunune kadar DEGISTIRILEMEZ.
Degisiklik ancak kullanicinin acik talimatiyla ve degisiklik gerekcesi
bu dosyaya islenerek yapilabilir (kriter kaymasi gorunur olmali).

## Hipotez (dil guncellemesi, 25 Tem mutabakati)
"q (token + baglam), gozlenen yol dagilimini secili fonksiyoneller
uzerinden anlamli bicimde KOSULLUYOR mu?"
(Eski ad "Opportunity Quality dogrulamasi"; OQ turetilmis ozettir,
temel nesne P(Path|q) — bkz docs/edge_mimari_iskeleti.md.)

## Evren (durust tanim)
- data/q_veri_seti.jsonl satirlari: EKG yolu OLAN ve en az bir q
  bileseni OLAN tokenler ("sensorlu kesisim").
- Sensorler yalniz R0 izleme kumesini (tavan 25) olcer; EKG tetik
  evreninin tamami DEGIL. Sonuclar bu kesisime genellenir, otesine
  genellemek ek kanit ister.
- Her token icin YALNIZ terfi ani (ilk) sensor olcumu: gelecek
  sizintisi yok.

## Sinanan ozellikler (sabit liste; ekleme = on-kayit ihlali)
  holder.top1_pay, holder.top5_pay, holder.top10_pay,
  erken.yeni_eski_orani, erken.ort_yas_gun, erken.medyan_yas_gun,
  yaratici.runner_var_asof (ikili), yaratici.lansman_n_asof,
  yaratici.dead_orani_asof, lp.amm (pumpswap ikili), lp.lp_top1_pay

### DUZELTME 1 (25 Tem, sinav ONCESI; gerekce kaydi)
Ilk taslakta yaratici ozellikleri toplam sicilden geliyordu; DRY-RUN
AUC=1.0 gosterdi ve kok neden SIZINTIYDI: sicil, tokenin KENDI
sonucunu da sayiyor (runner token -> yaraticisinin runner_n'i >= 1).
Duzeltme: yaratici ozellikleri AS-OF hesaplanir: yalniz bu tokenin
dogumundan ONCEKI lansmanlar sayilir (leave-current-out zaten kapsam
ici). Esikler ve diger tanimlar DEGISMEDI.

## Hedef etiket
runner = yolun ATH'si >= +%100 (EKG tetik fiyatina gore; sicil ile
ayni tanim). Ikincil rapor etiketi: olu = yasam_dk < 30 ve ATH < +%10
(yalniz bilgi; karar kriterine girmez).

## Metrikler (donmus operasyonellestirme)
1. MEKANIZMA-AUC: ozellik basina Mann-Whitney AUC (runner vs degil,
   bagli degerlerde ortalama sira). Kriter: EN AZ BIR ozellik
   AUC >= 0.65 (veya <= 0.35, yon simetrik).
2. KAHIN-YAKALAMA (recall@20): ozellik siralamasinin ilk %20'lik
   dilimi, runnerlarin >= %30'unu iceriyor mu (ozellik basina; en iyi
   ozellik raporlanir). Yon: AUC < 0.5 ise siralama ters cevrilir.
Iki kriter DE saglanmazsa edge-siniflandirma programi KAPANIR
(22 Tem on-kaydinin ayni esikleri: 0.65 VE %30).

## Gecerlilik on-sartlari
- Tam satir (yol + 3 sensor + yaratici) n >= 200; altindaysa sinav
  ERTELENIR (hukum verilmez, "veri yetersiz" yazilir).
- Sinav tarihi: 2026-08-08 .. 2026-08-15 arasi tek kosu.
- Oncesindeki her kosu DRY-RUN etiketi tasir: boru hatti testi, hukum
  DEGIL; sonuclari karar metnine alintilanamaz.

## Arac
scripts/kill_bataryasi.py — bu dosyadaki tanimlarin bire bir kodu;
sklearn yok, ogrenilmis birlesik skor yok (kompozit model on-kayit
ihlali olur). Cikti: data/kill_bataryasi_sonuc.json + stdout.
