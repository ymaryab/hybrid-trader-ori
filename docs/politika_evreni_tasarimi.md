# Politika Evreni Tasarimi (arastirma dokumani) — 25 Tem 2026

Amac: mevcut 11 motoru iyilestirmek DEGIL, Edge'in secim yapabilecegi
en guclu ortogonal politika evrenini tasarlamak. Bu dokuman kod/patch
icermez; karar secenekleri + gerekceler + arastirma yol haritasidir.

## 1. Mevcut evrenin taksonomisi (veriyle)

- 6h-kova PnL korelasyon matrisi (7 gun) + kural setleri + karne birlikte:
  11 kimlikte ~2.5 gercek politika.
- Scalp ailesi (v7, v7c, v7d, v7hizli, v7new, v7ht, v7t, yz, yzn1):
  tek politikanin cikis varyasyonlari; ayni akis gelgitine bagli.
- R1: filodaki TEK gercek dekorelasyon kaynagi (~0/negatif korelasyon).
- R2: cikis felsefesi ozgun, giris dalgasi scalp kumesiyle +0.9.
- Yapisal bulgu: uzun-tek-yon ayni evrende cesitlendirme sinirli; gercek
  eksenler ZAMAN (oynamamak) ve EVREN (token yasam evresi).

## 2. Ideal evren: 7 aile

### F1 SCALP-CORE (mevcut, asiri temsilli -> damit)
- Hipotez: taze trending FOMO itkisi dakikalar surer; +2-5 yuksek
  isabetle doner (kazanma %71-84, TP kaymasi lehte +0.5).
- Calisir: yuksek lansman/kosucu-dogum saatleri. Calismaz: kuraklik
  (timeout kovasi medyan -6: ana kanama), bicak gecesi.
- Giris: h1/m5 momentum + liq taban + guvenlik + tavanlar.
- Cikis: TP+2 + karla-timeout + KISA cuval + felaket.
- Edge: isabet yuksek/kar kucuk; ortalama yalniz kuyruk filtreli
  zamanlarda pozitif (yogun arsiv: medyan +2, ort -12).
- Yakin: 9 uye. Eksik: hayir, fazlalik var.

### F2 RUNNER-CORE (mevcut, tek saglam uye)
- Hipotez: dikkat kaskadi az tokeni 2-10x kosturur; sarsintiya dayanan
  kazanir (kosucu dogumu 3.4/saat; RAKO gunu +$36).
- Calisir: kosucu-dogum yuksek rejim. Calismaz: chop; erken kesen her
  kural zehir (sonda kaniti: %59 toparlanma).
- Giris: 30-150 bant + m5 + yas. Cikis: breakeven zirhi + kilitler +
  ratchet trail; erken kesme yok. Edge: pozitif carpiklik,
  mfe-yakalama ~0.5. Yakin: R2 (cekirdek), R1 (varyant).

### F3 DIP-CLAIM (YENI — en net bosluk)
- Hipotez: guclu acilan tokenda ilk dakikalarin -3..-8 cekilmesi satici
  yorgunlugu; kanit: sonda karsi-olgusali (34'un 20'si 10dk'da +5 ustu,
  1'i coktu), kazanan medyan MAE -0.2 (ayna goruntusu).
- Calisir: normal/yuksek akis. Calismaz: rug dalgasi, kuraklik
  (ayristirici sart: liq kaliciligi + ilk-dakika guc).
- Giris: sarsinti bandinda alim (filo yuksekten alirken ucuzdan).
- Cikis: hizli +4..+8 veya sarsinti dibi alti stop.
- Edge: momentum ailelerine yapisal dusuk korelasyon. Yakin: YOK.

### F4 GRAD-EVENT (YENI — tek olay-tetikli aday)
- Hipotez: bonding curve dolumu -> PumpSwap gocu zamani belli likidite
  olayi; gorunurluk sicramasi ongorulebilir mikro pencere acar.
- Bilgi avantaji: kendi WSS GraduationObserved akisimiz (~13-15/gun),
  scanner gecikmesiz. Calismaz: mezuniyetin onceden fiyatlandigi
  hiper-dikkat donemleri. Cikis: dakikalar, siki. Yakin: yok.

### F5 REVIVAL (YENI/yari-bosluk)
- Hipotez: ilk dalgayi atlatan yasli token oturmus holder tabaniyla
  ikinci dalgada guvenli kosar (RAKO cift dongusu; R2 yas>=60dk ilkel
  hali). Calismaz: taze-lansman cilginligi.
- Giris: yas 2-24h + konsolidasyon sonrasi hacimli kirilim (EKG evren
  hazir). Cikis: runner trail. Edge: dusuk rug + zaman-ayriklik.

### F6 CASH-VETO (YENI — kavramsal bosluk)
- Hipotez: akis kuruyunca en iyi politika hicbiri. Kanit: timeout
  kanamasi + golge %40 salter + negatif edge tablolarinin es-zamani.
- Salter guvenlik refleksi; CASH-VETO fiyatlanan SECENEK olur: Edge
  "en iyi politika = nakit" diyebilmeli.

### F7 Q-SELECT (bilincli bos — batarya kilidi)
- Sensor-kosullu giris. DRY-RUN erken sinyal: erken alici cuzdan yasi
  AUC 0.765 (n=33, hukum degil); Faz 0 negatifti. 8-15 Agu hukmune
  kadar tasarim spekulatif.

## 3. Hic bakilmayanlar (varsayim sorgulari)

- N1 CROSS-TOKEN LEAD-LAG: ayni yaratici/temanin tokenlari arasi
  oncul-artcil kosu; yaratici haritasi + EKG ile bugun test edilebilir.
- N2 FLOW-IMBALANCE: R0 swap akisinin alim/satim dengesizligi HAM ve
  kullanilmamis; muhtemelen tum ailelere ortak duyu organi.
- N3 EXIT-AS-POLICY (en buyuk sorgu): evren giris-merkezli; olcumler
  kaybin CIKISTA oldugunu soyluyor (mfe-yakalama 0.5, esik-delme,
  timeout). "Ayni giris, radikal farkli cikis" arastirma ekseni.
- N4 ANTI-CROWD: kalabalik bot tespiti; short yok, veto sinyali olur.
- N5 SIZE-AS-SIGNAL: sabit boy sorgusu; boyutlamanin politikalasmasi
  (tahsis isi, batarya sonrasi).

## 4. 11 motor karar tablosu

| Motor | Karar | Gerekce |
|---|---|---|
| R2 | KORU (RUNNER-CORE) | ozgun cikis, kanitli yakalama |
| R1 | KORU -> REVIVAL tohumu | tek dekorelasyon kaynagi |
| yz | BIRLESTIR -> SCALP-CORE cekirdegi | en temiz damitilmis set |
| v7ht | BIRLESTIR (tavan+cuval+karla mirasi) | A/B felaket-sifir kaniti |
| v7hizli | BIRLESTIR sonra emekli | A/B kontrol bitince |
| v7new | DENEY KULVARI (TP5, sureli) | canli kaynak; deney bitince karar |
| yzn1 | DENEY KULVARI (sonda A/B) | sonda kuralinin yasam alani |
| v7d | DENEY KULVARI (stop_6, sureli) | n=3, hukum icin veri yok |
| v7 | EMEKLI ET | mirasi yz'de |
| v7c | EMEKLI ET | kirik WIP, olcum gurultusu |
| v7t | EMEKLI ET | ayirt edici iddia kalmadi |

Hedef: 11 kimlik -> 4 cekirdek + 3 sureli deney kulvari + 3 yeni aile.

## 5. Davranistan-motor-turetme: retro + yeniden tasarim

- Olgunlasmama koku: METODOLOJI (mekanizma hipotezsiz taklit +
  hayatta-kalan yanliligi + eleme altyapisi yoklugu). Veri ikincil,
  zamanlama erken.
- Bugunku tasarim (hibrit kulucka hatti): insan AILE tasarlar
  (hipotez + on-kayitli vaad zorunlu), makine aile ici VARYANT uretir,
  secim OGRENILMEZ; dort on-kayitli kapi: (1) yogun arsiv replay
  (sadakat beyanli), (2) paper kulucka: min N gun + >=2 rejim + min
  orneklem, (3) karne sinavi: vaad-uyum + MEVCUT EVRENLE KORELASYON
  CEZASI, (4) holdout ayrikligi. Terfi/emeklilik kullanici raporuyla.
- Altyapinin ~%80'i mevcut (arsiv, sadakat, karne, gece zinciri);
  eksik: varyant uretici + yasam dongusu yonetimi.

## 6. Yol haritasi

- BUGUN karar verilebilir (kodsuz): (1) bu dokumanin referans ilani,
  (2) scalp birlesmesi ILKE karari (takvim ayri; deney kulvarlari
  dogal bitisine kadar surer), (3) F3/F4/F5 vaad on-kayitlarinin
  yazilmasi, (4) CASH-VETO'nun tahsis sozlugune mesru secenek ilkesi.
- BATARYA SONRASI (hukumden bagimsiz): kulucka hatti insasi; F3/F4/F5
  paper prototipleri; N2 analizi; N1 arastirma sorusu.
- YETERLI VERI SONRASI: R2 sonda-kapali karsi-olgusal raporu; scalp
  birlesme uygulamasi; F7 (yalniz batarya GECERSE); N3 sistematik
  calismasi.

## 7. Acik itirazlar (varsayim duzeltmeleri)

1. "11 motor = cesitlilik" varsayimi YANLIS: cesitliligin ~%80'i tek
   motordan (R1).
2. Giris-merkezli dusunme agirligi muhtemelen yanlis; kayiplar cikista
   oluyor: N3 yukari itilmeli.
3. "Daha cok motor = daha cok ogrenme" yanlisti: 9 scalp klonu tek
   temiz A/B'den az bilgi uretti.
