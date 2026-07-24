# OTONOM ÇALIŞMA KURALLARI (24 Tem 2026)

Canlı hattın kaynak motorunu otomatik seçen sistemin resmi kural
kitabı. Kod: `src/hibrit_trader/otonom_secici.py` + `canli_session.py`
tasfiye kancası. Her kural kullanıcı kararıyla konuldu; değişiklik
ancak kullanıcı onayıyla yapılır ve AutonomConfigChanged olayıyla
kayda geçer.

## 1. Amaç
Canlı kasayı, kayan pencerede en güçlü YÜKSELİŞTEKİ motora bağlı tutmak;
hiçbir motor güçlü değilken canlı alımı tamamen durdurmak.

## 2. Ölçüt (skor)
- Kayan pencere: varsayılan 30 dk (24 Tem; buton açılışında değiştirilebilir).
- skor = eq_şimdi / eq_pencere_önce - 1
- eq_şimdi = start + GERÇEKLEŞEN K/Z + AÇIK POZİSYON ANLIK K/Z (MTM,
  state.last_price'tan; "satılmasa bile satılmış gibi").
- eq_önce: pencere başındaki equity örneği (MTM'li); örnek yoksa
  gerçekleşen kümülatif; motor pencereden gençse start.
- Pompa artı yazar, plato sıfır yazar, sönüş eksi yazar.
- Panel kart rozetleri AYNI fonksiyondan beslenir (tek gerçek kaynak).

## 3. Uygunluk (kim yarışabilir)
- Pozitiflik eşiği: skor >= +%1.5 (OTONOM_POZITIF_ESIK; 24 Tem, 30dk pencereye kalibre). Altında kalan
  motor "negatif" sayılır, lider olamaz.
- Pencerede en az 1 kapanmış işlem (OTONOM_MIN_ISLEM=1) ve kasa >=
  $150 (OTONOM_MIN_KASA_USD) şartı: salt MTM kıpırtısıyla veya cüce
  kasayla liderlik olmaz (24 Tem R1 vakası).

## 4. Lider seçimi
- STATE-TRIGGER: karar her turda "lider != mevcut kaynak" üzerinden;
  başarısız geçiş sonraki turda kendiliğinden yeniden denenir.
- Fark belirginse (> OTONOM_MARJ_PUAN = 1 puan) en yüksek skor kazanır.
- Fark marj İÇİNDEyse EĞİM kazanır (2 TUR = 10 dk önceki skora göre fark; 24 Tem);
  eğim önceliği YALNIZ eğimi pozitif olanlara tanınır (sıfır/negatif
  eğim "yükselen" değildir), yükselen yoksa seviye kazanır (24 Tem fix).
- VETO: mevcut motor uygunken, sönen (eğim<0) lidere marj içinden
  geçilmez.
- ZİRVEDE OLANDA KAL: mevcut kaynak lider ise hiçbir şey yapılmaz.
- Eşitlik bozma deterministik: önce mevcut, sonra alfabetik.

## 5. Geçiş prosedürü (hibrit tasfiye)
1. Karar anında canlıya YENİ ALIM kilitlenir.
2. ZAYIF kağıt (pnl <= -1 VEYA hiç +1 görmemiş) ANINDA satılır:
   lider uçarken ölü kağıdın arkasında beklenmez.
3. UMUTLU kağıda (kârda/güç göstermiş) doğal çıkış süresi tanınır:
   OTONOM_DOGAL_SN = 600 sn; kendi kuralıyla (TP/fren/kilit) çıkar.
4. Süre dolunca kalanlar zorla satılır (en fazla +180 sn).
5. Düzleşince lider YENİDEN doğrulanır; bu arada sönmüşse geçiş
   İPTAL (bayat lidere geçilmez).
6. Swap: drop-in + servis restart; defter sıfırlanmaz, kural_degisim
   satırı düşer. Restart sonrası mutabakat SwitchCompleted/Failed yazar.
- Cooldown: iki geçiş arası en az OTONOM_COOLDOWN_SN = 900 sn.

## 6. Rejim anahtarı (sistem bayrağı)
- TÜM motorlar %1 eşiğinin altındaysa: BEKLEMEDE + ŞALTER İNER
  (canlı alım durur, çıkışlar sürer).
- Bir motor %1'i aşıp lider olunca: şalter kalkar, gerekirse geçilir.

## 7. Kullanıcı kontrolü ve güvenlik hiyerarşisi
- Panel OTONOM butonu = user_enabled. AÇMAK şalteri de açar
  ("alıp satmaya başlasın"); KAPATMAK yalnız seçimi durdurur, kaynak
  son motorda sabit kalır, şaltere dokunmaz.
- Fiili çalışma = user_enabled VE system_enabled (rejim).
- LIVE_ONAY (acil fren) ve kill-switch HER ŞEYİN üstündedir; otonom
  bunların altında çalışır. Günlük zarar limitleri aynen geçerli.
- Otonom açıkken elle indirilen şalteri sistem pozitif liderde geri
  açar; kalıcı durdurmak için önce otonom kapatılır.

## 8. Denetim (kara kutu)
- Tüm olaylar Gözlem Katmanı omurgasında, akış "otonom":
  SelectorBoot, AutonomEvaluated (ham girdiler: equity_now, baseline,
  baseline_ts/source, açık poz MTM, eğimler, tam sıralama, config,
  git sha), SwitchRequested/Aborted/Completed/Failed (switch_id
  zinciri), AutonomOn/Off (şalter alanıyla), AutonomUserToggle ve
  AutonomConfigChanged (actor: user|system).
- Her karar yalnız logdaki girdilerden yeniden oynatılabilir olmalıdır.

## 9. Parametreler (env)
| parametre | varsayılan | anlam |
|---|---|---|
| pencere_dk (OTONOM_MOD.json) | 30 | kayan pencere |
| OTONOM_POZITIF_ESIK | 1.5 | uygunluk eşiği (%) |
| OTONOM_MARJ_PUAN | 1.0 | eğim kuralı marjı |
| OTONOM_KONTROL_SN | 300 | değerlendirme aralığı |
| OTONOM_COOLDOWN_SN | 900 | geçişler arası asgari süre |
| OTONOM_DOGAL_SN | 600 | umutlu kağıda doğal çıkış süresi |
| OTONOM_TASFIYE_SN | 180 | zorlama penceresi |
| OTONOM_MIN_ISLEM | 1 | pencere içi asgari işlem |
| OTONOM_MIN_KASA_USD | 150 | liderlik için asgari kasa |
| CANLI_TASFIYE_ZAYIF_PCT / _MFE | -1 / 1 | zayıf kağıt tanımı |

## 10. Bilinen sınırlar ve bekleyen onaylar
- Histerezis/dead-band ve eşik optimizasyonu bilinçli ERTELENDİ.
- ONAY BEKLİYOR: geçişe-hazırlık modu (cooldown'da alım kilidi) ve
  boot'ta yetim tasfiye mutabakatı (23 Tem yarış kazası dersi).
- Olaylardaki git_sha scp deploy'da bayat kalabilir (repo-sync işi).
- MTM ölçütü tepe-yanılsaması riski taşır (kullanıcı bilinçli tercihi);
  %1 eşiği + eğim vetosu kısmi fren.
