# EDGE GO ON-KAYDI — 26 Tem 2026

Karar verildi: Edge canli karar verici olacak. Bu dosya GO kanitinin
tanimini SINAVDAN ONCE dondurur. Esikler degistirilemez; zorunlu
duzeltme yalniz gerekce kaydiyla (kill-bataryasi disipliniyle ayni).

## Durustluk beyani
Gölge verisinin ilk ~2 gunu (veto ayrismasi dahil) esikler yazilirken
GORULMUSTU. Bu yuzden DEGERLENDIRME DONEMI bu dosyanin commit aninda
BASLAR; onceki gunler GO kanitina SAYILMAZ (yalniz boru hatti testi).

## Birincil KPI (26 Tem tartisma sonucu)
**Eslestirilmis net PnL farki, gecis maliyetleri dahil.**
- Birim: degerlendirme turu (ardisik EdgeShadowEvaluated olaylari
  arasi pencere, ~5 dk).
- Her turda: golge_aday motorunun o penceredeki gerceklesen paper
  PnL'i (USD) EKSI legacy_hedef motorunun ayni penceredeki PnL'i.
- Golge karari CASH ise golge tarafi 0 USD sayilir (nakit getirisiz).
- GECIS MALIYETI: golge karari onceki turdan FARKLIYSA golge tarafina
  -1.50 USD yazilir. Turetim: otonom_tasfiye karnesi (n=55, medyan
  -1.6%) x ortalama bilet ~30 USD x ortalama ~3 acik poz ~= 1.4;
  muhafazakar yuvarlama 1.50. CASH'e gecis de gecistir (tasfiye).
- Referans: LEGACY (goreli). Mutlak esikler Governor kisitidir, KPI
  degildir (26 Tem karari); raporlarda mutlak seviye YANINDA yazilir.

## Mekanizma metrikleri (tesheis + sans bekcisi; tek baslarina GO/NO-GO veremez)
1. Siralama-IC: her turda edgeler vektoru ile motorlarin izleyen
   pencere gerceklesen PnL'i arasinda Spearman; gunluk medyan.
2. Veto ayrismasi: salter turlerinin izleyen-pencere filo PnL ortalamasi
   ile aktif turlerinkinin farki (veto_degeri.json serisi).

## GO KRITERI (on-kayitli)
Degerlendirme donemi: ts=1785058502 aninda baslar; >= 7 gun VE
>= 1500 degerlendirme turu. Sonunda:
- (Z1) Eslestirilmis toplam fark (maliyet sonrasi) > 0, VE
- (Z2) pencere-fark serisinde isaret testi: pozitif pencere payi > %50
  (bag pencereler haric), VE
- (Z3) en az BIR mekanizma metrigi pozitif: gunluk medyan IC > 0
  (gunlerin cogunlugunda) VEYA veto ayrismasi pozitif (gunlerin
  cogunlugunda).
Uc kosul da saglanirsa GO; degilse NO-GO ve donem 7 gun uzatilir
(en fazla iki uzatma; sonra tasarima geri donulur).

## Kapsam notlari
- Bu olcum v1 golge vekiliyle yapilir (pencere-pct tabanli). Karar
  cekirdegi (LCB/aile katalogu) yayina girdiginde AYNI KPI korunur;
  yalniz karar ureticisi degisir.
- Censoring kurali bu olcumun disindadir (gerceklesen PnL kullanilir);
  replay-tabanli karar cekirdegi icin ayri on-kayit sarttir (CRITICAL
  blocker listesinde).
- Olcum araci: scripts/golge_defter.py; gece zinciri gunluk kosar;
  data/golge_defter.json birikimli seridir.

## GO GUNU PROSEDURU (26 Tem kullanici mutabakati, DONUK: secenek C)
GO raporu Z1+Z2+Z3 gecerse:
1. Rapor + KULLANICI ONAYI (kademe acilisi otomatik degil).
2. KADEME 1 YALNIZ-VETO: 48 saat VE >=10 veto olayi. Edge yalniz
   "yeni giris yok" diyebilir (salter tek-yazar kurallarina tabi);
   aile secimi legacy'de kalir. Gecis kriteri: veto pencerelerinin
   gerceklesen degeri golge tahminiyle tutarli + sifir cift-otorite
   catismasi + fallback temiz.
3. KADEME 2 TAM YETKI (kullanici onayiyla): parametreler ilk 7 gun
   DONUK; RUNNER "dogrulanmamis" etiketi surdukce guven kisiti aynen.
4. HER KADEMEDE TEK-KOMUT GERI ALMA: Edge aninda golgeye doner,
   legacy devralir. Geri alma insansiz olabilir; YENIDEN yetki YALNIZ
   kullanici onayiyla.
5. Olcum surekliligi: golge-defter ve KPI'lar kademelerde kesintisiz.
Gerekce: danisman fazi 7 gunluk golge doneminin kendisidir (tekrar
bilgi uretmez); veto, canli yurutmenin en dusuk-riskli sinavidir.
