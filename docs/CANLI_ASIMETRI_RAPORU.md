# Canli Asimetri Raporu (12 Tem 2026, read-only tarama)

Soru: v7 canli para tasiyor; paper'da masum ama canlida v7 aleyhine maliyet
ureten yapisal fark var mi?

Kapsam: sadece tespit + hazir uygulama onerisi. HICBIR degisiklik uygulanmadi.
Kaynaklar: v6/v7_session.py, broker.py, jupiter.py, fast_price.py, scanner.py,
momentum_session.py, sunucu trade/reject kayitlari, dryrun_fills.jsonl (n=819),
docs/POLICY.md.

## Ozet tablo

| # | Bulgu | Canli maliyet tahmini | Oncelik |
|---|-------|----------------------|---------|
| B1 | Satis slippage 50 bps sabit + basarisiz satis 30s retry | rug vakasinda bilet basina %10-50 ek kayip; cikis tam ihtiyac aninda bloklanir | KRITIK |
| B2 | v7'de hizli goz yok: fren/stop tetikleri 30s poll | stop asimi 0.4-1.0 puan/vaka; rug'da 30s korluk %5-10 ek kayip | ORTA-YUKSEK |
| B3 | Likidite cokusunu gorme hizi: v7 30s, v6 fiyat sinyalini 2s'de gorur | B1+B2 ile ayni kok; onlar cozulunce kapanir | ORTA |
| B4 | POLICY "Jotchua-tipi ek onlem YOK" karari paper varsayimiyla alindi | karar canli kosulda bayat; B1/B2 ile yeniden degerlendirilmeli | ORTA |
| B5 | Alim 50 bps toleransi | giris kacirma (para kaybi degil, firsat maliyeti) | DUSUK |
| B6 | Canli fee muhasebesi: fee_usd=0, priority fee pnl'de yok | karne hafif iyimser, bilet basina ~sent mertebesi | DUSUK |
| B7 | Kaynak sirasi (faz takasi sonrasi) | kalan yapisal sira dezavantaji YOK | DUSUK (temiz) |

## B1. KRITIK: satis slippage 50 bps sabit, basarisiz satis 30 saniye bekler

Tespit:
- ExecOrder.slippage_bps varsayilani 50 (broker.py 114) ve v7 _exec_fill
  ExecOrder'i override etmeden kurar (v7_session.py 221-231): alim VE satis
  canli tx'leri 50 bps toleransla gider.
- .env'deki MAX_SLIPPAGE_BPS=100 SADECE eski live.py motorunu besler
  (config.py 149 -> live.py); v7'nin broker yoluna BAGLI DEGIL. Yaniltici.
- Satis basarisizsa (_close_position, v7_session.py ~457): "SATIS ERTELENDI"
  loglanir ve pozisyon bir SONRAKI 30s kadansta tekrar denenir.

Canli senaryo: fren (-%10) tetiklendiginde fiyat zaten hizla dusuyordur.
Quote ile tx onayi arasinda fiyat 50 bps'ten fazla kayarsa tx zincirde
basarisiz olur (islem_hatasi) -> 30s bekle -> fiyat daha da dusuk -> yine
50 bps tolerans -> yine basarisiz. Rug'da kisir dongu: cikis en cok ihtiyac
olan anda bloklanir. Eski Jotchua kayitlari dusus hizini gosteriyor: tek
~35s tikte -%5 (22 Haz kayitlari, mae -5.0/-5.4); mogcat vakasi 100x cokus.
Boyle bir vakada her basarisiz tur $25 biletin %5-10'unu yer; toplam kayip
%10-50 bandina cikabilir ($2.5-12.5/vaka).

Not: dryrun olcumu (n=819, cogunlukla major PROBE) |fark_bps| medyan 6.4,
p90 51.2. Yani SAKIN majorlerde bile fark p90'da 50 bps sinirinda; v7'nin
memecoin evreninde (h1 +%10-50 hareketli tokenlar) makas belirgin daha genis.

UYGULAMA ONERISI (onay sonrasi):
1. Cikis emirlerinde kademeli slippage: normal cikis (tp_2, timeout) 150 bps;
   stop_gec 300 bps; stop_felaket 1000 bps (yaklasik: zarari durdurma aninda
   dolgu kalitesi degil KESINLIK onceliklidir). ExecOrder'a yon-bazli bps
   gecirmek icin v7 _close_position'da reason'a gore slippage_bps parametresi.
2. Alternatif/ek: Jupiter dynamicSlippage=true (swap payload) cikislarda.
3. Basarisiz satista 30s beklemeden kisa araliklarla tekrar: ornegin 3 deneme
   x 3s, sonra kadansa don (sadece stop_felaket/stop_gec yolunda).
4. MAX_SLIPPAGE_BPS env'inin v7 yoluna bagli olmadigini POLICY'ye not dus
   (veya ExecOrder varsayilanini bu env'e bagla; tek satir).

## B2. ORTA-YUKSEK: v7'de hizli goz yok, fren 30s cozunurlukte

Tespit:
- v6: fast_exit_tick her 2s'de fast_price feed'inden okur (v6_session.py 386);
  girof pozisyon havuzu feed'e eklenir (feed.add_pool). Cikis tetik gecikmesi
  kayitlarda ~0.5-0.8s (12 Tem SCAM trade'leri: tetik_gecikme_sec 0.49/0.82).
- v7: cikis SADECE 30s tam tikte, fetch_pool_snapshot tek poll
  (v7_session.py 418). fast_price docstringi acikca "v7/X1/live_sim bu
  modulu import ETMEZ" der. Yani v7'nin fren'i (-%10) en kotu 30s gec gorur.

Sayisal kanit (paper doneminden, v7_trades.jsonl):
- tolywifhat 09 Tem 07:23: esik -%10, gerceklesen -%10.97 (asim 0.97 puan,
  mae -10.86, hold 285s, $207 bilette -$22.7; asimin payi ~$2).
- HOOD 09 Tem 09:03: gerceklesen -%10.40 (asim 0.40 puan, ~$0.8).
Asim, 30s poll'un dogrudan olcumu: fiyat esigi tikler ARASINDA gecti.
2s gozle asim tipik olarak 1/15'ine iner (0.03-0.07 puan).

Canli maliyet: $25 bilette normal fren vakasi basina $0.10-0.25; rug
vakasinda (fiyat %5-10/30s duserken) $1.25-2.50 + B1 dongusunun carpani.
Frekans: v7 paper'da 49 trade'de 2 stop_felaket (%4). Bilet buyudugunde
maliyet dogrusal buyur; LIVE_MAX_USD artirilmadan once bu kapanmali.

UYGULAMA ONERISI:
1. v6'daki deseni v7'ye tasi: giriste feed.add_pool, cikista feed.remove_pool,
   2s fast_exit_tick (TUM cikis kurallari dahil, ozellikle stop_felaket).
   fast_price docstringindeki "v7 import etmez" sozlesmesi guncellenir.
   Feed zaten v6 pozisyon havuzlarini tasiyor; ek API yuku pozisyon basina
   zaten odenen maliyetin aynisi (30 havuzluk tek batched istekte yer var).
2. Minimal alternatif (feed'e dokunmadan): v7 run_forever'daki duz
   time.sleep(SCAN_INTERVAL_SEC) yerine v6'daki gibi 2s'lik dilimli bekleme +
   sadece ACIK POZISYON VARKEN hizli cikis kontrolu. (v6 ile ayni kod deseni,
   dusuk risk.)
Onerim: secenek 1 (olcum kolonlari price_source/tetik_gecikme_sec de bedava
gelir, v6 ile karne kiyasi simetrik olur).

## B3. ORTA: likidite cokusu gorme hizi

Tespit:
- v7: 30s tikte fetch_pool_snapshot fiyat+likidite dondurur; guard_price
  likidite-teyitli crash re-base icerir (liq girisin %20'si altina dustuyse
  asagi yonlu fiyat aninda taban, POLICY ders 3). Yani v7 cokusu EN GEC 30s
  icinde gorur ve degerlemesi duzelir; sorun gorme degil TEPKI hizi (B1/B2).
- v6: feed 1s fiyat verir ama likidite tasimaz; likidite teyidi v6'da da 30s
  tiktedir. Ancak rug'da fiyat sinyali likidite sinyalinden once gelir ve
  v6 bunu 2s'de yakalar.

Sonuc: bagimsiz bir acik degil; B1+B2 cozulunce v7'nin cokus tepkisi v6
seviyesine (fiyat 2s, likidite teyidi 30s) gelir. Ayri is kalemi ONERMEM.

## B4. POLICY ertelenen vidalar: canli etiketi

| POLICY maddesi | Canliyi etkiliyor mu | Not |
|---|---|---|
| X1 rejim kapisi (sol_h1 0.2-0.3) | HAYIR | X1 paper; karne gunu gundemi aynen kalsin |
| Jotchua-tipi rug ek onlem YOK karari | EVET | Karar "kucuk bilet + yarim-tp yeterli" gerekcesiyle PAPER kosulda alindi. v7 canli + B1 (satis bloklanmasi) varken bayat. B1/B2 uygulanirsa karar yeniden gecerli olur; uygulanmazsa rug vergisi canli parada katlanir. |
| v6/v7 slot_dolu kaydi | DOLAYLI | Olcum eksigi; para riski yok ama v7 kacan-aday analizi (canli firsat maliyeti) bu veriye muhtac. Dusuk oncelikle yapilmali. |
| entry_fresh kayit-yazma log debug->warning | DOLAYLI | Canli denetim defterinde sessiz kayit kaybini gorunur yapar; para riski yok. Ucuz, B1 paketiyle birlikte gecebilir. |

## B5. DUSUK: alim 50 bps toleransi

- $25 bilet, liq>=$100k havuz: mekanik fiyat etkisi ~2.5 bps; sorun etki degil
  momentum ani (m5 +%4 tokenda quote->onay arasi drift).
- Alim basarisizligi GUVENLI taraftadir: giris kacar, para kaybolmaz. paper/canli
  karne farkina "kacan canli giris" olarak yansir.
- ONERI: alim 50 bps KALSIN. Sadece izleme: islem_hatasi nedenli alim
  basarisizliklarini decisions/log uzerinden sayan basit bir karne notu.
  50 bps'lik alim reddi orani > %20 olursa 100 bps'e cikarma karari o veriyle
  alinir. Simdiden degistirme ONERMEM (henuz canli fill orneklemi yok).

## B6. DUSUK: canli fee muhasebesi

- LiveExecBroker fill'i fee_usd=0.0 dondurur; JITO_EXIT=1 ile cikis tx'ine
  prioritizationFeeLamports=auto ekleniyor (dogru tercih) ama bu fee pnl'ye
  islenmiyor. Gas GAS_COST_USD sabitiyle yaklasik dusuluyor.
- Maliyet: bilet basina sent mertebesi; $25 bilette karne %0.1-0.5 iyimser.
- ONERI: dusuk oncelik. Jupiter swap yanitindaki prioritizationFee/fee alani
  varsa fee_usd'ye yazilip proceeds'ten dusulmesi tek noktali degisiklik
  (LiveExecBroker.execute). LIVE_MAX_USD buyuyene kadar erteleneblir.

## B7. TEMIZ: kaynak sirasi (faz takasi sonrasi dogrulama)

30s dongude faz sirasi ve tarama cache'i (SCAN_CACHE_SEC=20):
- x1 5.6s (dongunun taze fetch'ini genelde bu oder), ekg 7.5s, v7 11.25s,
  v6 18.75s, v7c 20.6s.
- v7 artik fotografi fetch'ten ~5.7s sonra okur, v6 ~13.1s sonra: 12 Tem
  takasi amaclanan yonu verdi, SCAM tipi "fotograf bir adim geride" vakasinin
  yeni magduru artik v6 olur (paper, kabul edilmis).
- sol_h1 cache (3600s) simetrik paylasimli; kim once gelirse tazeler, ikisi de
  ayni degeri okur. Asimetri yok.
- entry_fresh/taze_teyit: iki motor da memecoin adaylarinda ayni tek-fetch
  yolunu kullanir (fast feed kapsami major evren + v6 ACIK pozisyon havuzlari;
  giris aninda ikisi de kapsam disi). Giris tarafinda asimetri yok; cikis
  tarafindaki feed asimetrisi B2'de.
- recheck/huni kuyruklari salt olcum; yarisa etki etmez.

Sonuc: faz takasi sonrasi v7 aleyhine kalan yapisal SIRA farki yok.

## Onerilen uygulama sirasi (onay bekliyor, madde madde)

1. B1 paketi: cikislarda kademeli slippage (150/300/1000) + stop yolunda
   3x3s hizli tekrar + MAX_SLIPPAGE_BPS baglanti notu. (KRITIK, kucuk diff)
2. B2: v7'ye hizli goz (v6 fast_exit_tick deseni, feed.add_pool dahil).
   (ORTA-YUKSEK, orta diff, testleri v6'dan uyarlanir)
3. B4-entry_fresh log seviyesi debug->warning. (ucuz, B1 ile ayni PR olabilir)
4. B6 fee muhasebesi ve B5 alim-red sayaci: LIVE_MAX_USD artirilmadan once.
