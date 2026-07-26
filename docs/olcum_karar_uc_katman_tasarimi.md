# Uc Katmanli Olcum-Karar Mimarisi (Motor Card alternatifi)

**Tarih:** 26 Tem 2026, aksam oturumu
**Statu:** TASARIM REFERANSI. Kod yok; uygulama adimi ayri kullanici
onayi gerektirir. Motor Card onerisinin elestirisi sonucunda kabul
edilen cerceve.
**Baglam:** BUB adli incelemesi (26 Tem, -%10.27) ve LCB analizi.
Tespit: LCB motor-agregatlarinin yayilimini olcuyor (uye_n=2 nokta),
islem-duzeyi varyansi, tek-olay kompozisyonunu, zamansal kumelenmeyi
ve sonme egimini GORMUYOR. Guven (0.18) uretiliyor ama karara bagli
degil. Sorun girdi eksikligi degil: bilginin karar fonksiyonuna
girmeden imha edilmesi + uretilen sinyallerin kablosuz kalmasi.

## Ilke: zengin veri, fakir karar fonksiyonu

Girdi ne kadar zenginlesirse zenginlessin, karar fonksiyonu az
parametreli, on-kayitli ve deterministik kalir. Bu orneklem
boylarinda (aile basina gunde 9-70 islem) cok-kriterli agirlikli
skor DOGRULANAMAZ; serbestlik derecesi eklemek kill-bataryasi ve
on-kayit disiplinine aykiridir. Motor Card'in reddedilme nedeni:
self-report (kendi karnesini yazan ogrenci) + dinamik skor davetiyesi.

## Katman 1: Motorlar, yalniz OLGU

- Islem kayitlari (mevcut: *_trades.jsonl) aynen surer.
- Anlik ic durum yayini: exposure (acik poz, MTM), tetikteki kural,
  aktif config surumu. YALNIZ motorun bilebilecegi olgular.
- Motor yorum yapmaz, istatistik uretmez, saglik skoru YAZMAZ.
  Gerekce: self-report yanlligi (motor kendi secilme olasiligini
  etkiler), 12 farkli hesap yolu karsilastirilamaz, denetlenemez.

## Katman 2: Merkezi olcum, "durum vektoru"

Tek modul, surumlu sema (sv disiplini, k1k3_blueprint Madde 11 ile
ayni kural). Defterden turetilebilen HER istatistik burada uretilir;
motorlar degil. Icerik (her aile/motor icin):

- **Islem-duzeyi ampirik dagilim:** son N islem, zamana gore
  agirlikli. Agregat-pct DEGIL. (BUB dersi 1: tek-olay kompozisyonu
  agregat icinde gorunmez olur.)
- **Etkin orneklem (n_etkin):** zamansal kumelenme duzeltmeli.
  (BUB dersi 2: ayni yarim saatten gelen 9 islem, 9 bagimsiz cekilis
  degildir; n_etkin ~2-3.)
- **Egim/sonme:** pencere ici trend. (BUB'da -1.9 vardi, kullanilmadi.)
- **Rug orani:** <= -%25 dolum payi. (26 Tem: runner %3.1-3.4,
  scalp %0-0.7; kara liste ve runner-canli-yasak kararlarinin
  veri tabani.)
- **Rejim etiketi**, exposure ozeti (Katman 1'den gecirilir).

Gece zinciri bu modulu denetler (uretim dogrulugu, sema uyumu).
Karar girdisi TEK NESNE olarak loglanir: replay (H8) ve adli
inceleme dogrudan bu nesneden yapilir.

## Katman 3: Edge, az parametreli karar fonksiyonu

Uc dar kanal; kart alanlarini serbest agirlikla birlestirmek YASAK:

1. **Taban siralama = islem-duzeyi LCB.** Medyan ve yayilim
   Katman 2'nin ampirik dagiliminden (islem cekimlerinden), payda
   n_etkin'den. Mevcut LCB'nin (uye-agregat pstdev / sqrt(toplam
   islem)) yerine gecer. BUB karsi-hesabi: bu tanimla LCB negatife
   duser, CASH tabanina takilirdi.
2. **Ikili VETO'lar (az sayida, tek tek kanitli).** Ornekler:
   egim negatif VE n_etkin < X iken aileye GECME; rug orani > Y ise
   aile canli-dISI (runner karari bunun elle verilmis hali). Her
   veto eklenmeden once eslesitirilmis karsi-olgusal olcumle
   kanitlanir (golge/karsi-olgusal altyapi mevcut). Kanitlanamayan
   sinyal durum vektorunde durur ama karara BAGLANMAZ.
3. **Guven -> boyut kanali.** Guven karar degil bilet carpani
   etkiler (dusuk guven = kucuk bilet). Karar fonksiyonuna esik
   olarak sokulmaz.

Histerezis, cooldown, CASH tabani, governor ve fallback merdiveni
aynen korunur. Cift-cekirdek canli kisiti (EDGE_CANLI_AILE_YASAK)
bu cercevede "elle verilmis veto" statusundedir; kalici hali
Katman 3 veto setine kanitla girer.

## Uygulama sirasi (onerilen, HER ADIM AYRI ONAY)

1. Katman 2 iskeleti: durum vektoru uretimi + gece denetimi
   (yalniz olcum, karara etkisiz; risk sifir).
2. Golgede kiyas: islem-duzeyi LCB vs mevcut LCB, ayni gecmis
   uzerinde eslestirilmis fark olcumu (KPI: net PnL farki,
   referans legacy).
3. Kanit PASS ise Katman 3 taban siralamayi degistir; veto'lar
   tek tek ayni kapidan gecer.
4. Motor Card'in loglama-sozlesmesi hali (surumlu sema) ancak
   1-3'ten sonra, ayri karar.

## Reddedilenler (kayit)

- Motor self-report saglik skoru: self-report yanlligi,
  karsilastirilamazlik, denetlenemezlik.
- Cok-kriterli dinamik Edge skoru: bu orneklemde dogrulanamaz,
  asiri-uyum yuzeyi.
- Kartin karar mekanizmasi olmasi: kart en fazla loglama
  sozlesmesidir; karar fonksiyonu dar kalir.
