# Politika Ailesi ON-KAYITLARI: F3 / F4 / F5 — 25 Tem 2026

Bu dosya SINAVDAN ONCE yazildi. Basari/basarisizlik esikleri sonradan
DEGISTIRILEMEZ; zorunlu duzeltme ancak gerekce kaydiyla (kill
bataryasi on-kayit disipliniyle ayni). Dil: "motor" degil "politika
ailesi" (25 Tem kullanici karari).

## Ortak degerlendirme protokolu (uc aile icin ayni)

1. **REPLAY KAPISI (paper oncesi zorunlu):** aile iskeleti yogun yol
   arsivinde (sadakat sinirlari beyanli) kosulur. Kapi kriterini
   gecemeyen aile paper'a HIC cikmaz; hipotez orada olur.
2. **PAPER KULUCKA:** taze $1000 defter; minimum sure/orneklem asagida
   aile basina; en az 2 farkli rejim kesiti (kosu + kuraklik) sart.
3. **KARNE SINAVI:** vaad-uyum metrikleri + MEVCUT EVRENLE KORELASYON
   CEZASI: SCALP-CORE ve RUNNER-CORE ile 6h-kova korelasyon > 0.5 ise
   aile "yeni cesitlilik" sayilmaz (basari esigi saglansa bile evren
   karari kullaniciya ayri sunulur).
4. **HOLDOUT:** eleme penceresi ile sinav penceresi AYRIK; ayni gunler
   iki kez kullanilamaz.
5. Terfi/emeklilik karari raporla kullaniciya; otomatik terfi yok.

---

## F3 DIP-CLAIM on-kaydi

**Mekanizma hipotezi:** ilk 15 dakikada guclu acilan tokenda -3..-8
cekilme cogunlukla erken satici yorgunlugudur, olum degil; likidite
cekilmemisse fiyat toparlar. Baz kanit: R2 sonda karsi-olgusali
(n=34: %59'u 10dk icinde kesim fiyatinin +5 ustune dondu, %3'u coktu);
kazananlarin medyan MAE -0.2 (guclu token dusmuyor: dusenlerin icinden
saglam secmek ayri bir evren acar).

**Beklenen rejim:** normal/yuksek akis saatleri. **Calismamasi
beklenen:** rug dalgasi saatleri ve kuraklik (cekilme = olum sinyali
olur); SOL negatif rejim.

**Giris iskeleti (parametre kutulari, spec degil):** tetik: tepe
sonrasi -3..-8 bandi; sartlar: ilk-dakika guc metrigi esik ustu,
likidite dususu <= %20, yas <= 30dk, guvenlik yesil.
**Cikis felsefesi:** hizli tamamlama: +4..+8 hedef; sarsinti dibinin
altina -4..-6 stop; sure tavani <= 30dk (bu aile kosucu TUTMAZ;
toparlanmayi satar).

**Beklenen edge:** momentum ailelerine yapisal dusuk korelasyon
(girisleri onlarin kacindigi anda); yuksek isabet orta kar.

**REPLAY KAPISI:** arsivde tanim-uyumlu cekilme orneklerinde iskelet
politika beklenen degeri (ucret dahil) > 0 VE isabet >= %50 degilse
paper yok.
**BASARI (paper, min 100 islem + 10 gun + 2 rejim):** isabet >= %55;
medyan pnl >= +2; profit factor > 1.3; timeout payi <= %25;
korelasyon sarti (ortak protokol).
**BASARISIZLIK/KILL:** 100 islem sonrasi isabet < %45 VEYA PF < 1.0;
veya p5 pnl <= -20 (kontrolsuz kuyruk); veya iki rejimden birinde
surekli negatif.

**Yakin mevcut politika:** yok (sonda_kes'in tersine cevrilmisi).

---

## F4 GRAD-EVENT on-kaydi

**Mekanizma hipotezi:** pump.fun bonding curve dolumu -> PumpSwap
havuz acilisi zamani ongorulebilir zorunlu likidite olayidir;
gorunurluk sicramasi kisa pencerede yon ve akis uretir. Bilgi
avantaji: kendi WSS GraduationObserved akisimiz (scanner gecikmesiz;
gozlenen hacim ~13-15 olay/gun).

**Beklenen rejim:** olay-bazli, akisla olcekli. **Calismamasi
beklenen:** mezuniyetin onceden fiyatlandigi hiper-dikkat donemleri;
dusuk hacim gece olaylari.

**ON-VERI KAPISI (replay kapisindan da once):** once SAF VERI ANALIZI:
arsivden mezuniyet-sonrasi ilk 30dk yol dagilimi cikarilir. Bu
dagilimda en az bir sabit iskelet politika (ucret dahil) pozitif
beklenti vermiyorsa AILE DOGMADAN OLUR ve bu sonuc rapor edilir.
Hipotezin olmesi de kabul edilen sonuctur.

**Giris iskeleti:** GraduationObserved + ilk havuz liq/fiyat sartlari;
olaydan girise gecikme butcesi <= 30 sn (olculur ve raporlanir).
**Cikis felsefesi:** dakikalar olcekli, siki: sabit hedef + kisa fitil
stop; pozisyon tasima YOK.

**BASARI (paper, min 60 olay-islem + 14 gun):** PF > 1.3; isabet >=
%50; medyan gecikme butce icinde; korelasyon sarti.
**KILL:** on-veri kapisi negatif; veya olay hacmi < 8/gun kalirsa
(orneklem imkansiz); veya 60 islemde PF < 1.0.

**Yakin mevcut politika:** yok (tek olay-tetikli aday).

---

## F5 REVIVAL on-kaydi

**Mekanizma hipotezi:** ilk dalgayi atlatip 2-24 saat konsolide olan
token, oturmus holder tabaniyla ikinci dikkat dalgasinda daha dusuk
rug olasiligiyla kosar. Baz kanit: RAKO cift dongusu (ayni gun iki
tam kilit+trail dongusu, MAE 0 ikinci dongu); R2 yas>=60dk filtresi
hipotezin ilkel halinin zaten calistigi.

**Beklenen rejim:** orta akis; kosucu-dogum orta/yuksek. **Calismamasi
beklenen:** taze-lansman cilginligi saatleri (dikkat yeniye akar);
genel kuraklik.

**ON-VERI KAPISI:** EKG arsivinden "ikinci dalga" tanimina uyan
kirilimlarin (yas 2-24h, konsolidasyon sonrasi yeni tepe) taban
oranlari: kirilim sonrasi runner orani, taze evren runner oranindan
DUSUKSE hipotez reddedilir.

**Giris iskeleti:** yas 2-24h; konsolidasyon bandi (tepe -%30..-%60
arasi yatay) sonrasi hacimli yeni kirilim; liq taban.
**Cikis felsefesi:** RUNNER-CORE mirasi: breakeven zirhi + kilit +
ratchet trail; erken kesme yok.

**BASARI (paper, min 80 islem + 14 gun + 2 rejim):** PF > 1.3;
felaket orani, R2 taze-evren felaket baz oraninin yarisindan az;
korelasyon sarti.
**KILL:** on-veri kapisi negatif; 80 islemde PF < 1.0; felaket orani
baz orani asarsa (hipotezin ana iddiasi curur).

**Yakin mevcut politika:** R2 kismen; R1 tohum adayi.

---

## CASH-VETO tanimi (25 Tem kullanici karari)

CASH-VETO bagimsiz bir politika ailesi/motor DEGILDIR; Allocation
katmaninin mesru bir SONUCUDUR: hicbir politikanin beklenen avantaji
esigi gecmiyorsa bos dagitim ({}) secilir ve bu bir karar olarak
loglanir/fiyatlanir. Mevcut karsiligi tahsis arayuzunun bos sozluk
donmesi + salter mekanizmasidir; olgun halinde veto saatlerinin
gerceklesen degeri (kacinilan zarar) duzenli rapor edilir.
