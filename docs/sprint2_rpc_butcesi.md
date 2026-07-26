# SPRINT 2 RPC BUTCESI (Helius Free: 1M kredi/ay, 10 istek/sn) - 25 Tem 2026

## Temel aritmetik
- 1M kredi/ay = 33.333/gun = 1.388/saat = 0.386/sn surekli ortalama.
- TASARIM TAVANI: 25.000 kredi/gun (%75) — %25 pay beklenmedik/acil icin.
- Hiz siniri 10 istek/sn: tum sensorler istekleri >=2 sn arayla serpistirir;
  tepe es-zamanlilik < 3 istek/sn (asagida).
- ILKE: WSS firehose (census+swap) HELIUS'A TASINMAZ; kamu mainnet-beta
  WSS'te kalir (calisiyor, kredi yakmaz). Helius YALNIZ anahtar isteyen
  HTTP cagrilarda.

## Sensor basina butce (kredi/gun)

| tuketici | cagri | formul | kredi/gun |
|---|---|---|---|
| Konsantrasyon: terfi ani | largestAccounts+supply | ~150 yeni token x 2 | 300 |
| Konsantrasyon: tazeleme | largestAccounts | ~25 token x 4/saat x 24 (supply 24h cache) | 2.400 |
| LP kilit (AKTIF 25 Tem) | accountInfo (+supply+largest yalniz raydium) | 150 terfi x 1-3, kalici cache, TAVAN 600/gun | 550 |
| Yaratici sicili (AKTIF 25 Tem) | 0 kredi (log ayristirma, gecelik 04:17) | v0 RPC fallback YOK; ayristirilamayan sayilir (%40, iyilestirme adayi) | 0 |
| Erken alici (AKTIF 25 Tem) | largest+multipleAccounts+getSignaturesForAddress(1000) | terfi basina <=22; cuzdan yasi KALICI cache; TAVAN 2.000/gun | 2.000 |
| Senkron bekcisi + kasa | balance/largest(mint) | poz basina 60sn + 10dk kasa | ~1.500 |
| Canli broker mutabakat | signatureStatuses/getTransaction | ~90 islem x ~3 | ~300 |
| TOPLAM (tum sensorler acikken) | | | **~7.550** |

**Sonuc: 7.550/gun = 226.500/ay = butcenin %23'u; tavana 3.3x, limite 4.4x pay var.**

## Ek satir (25 Tem): yaratici sicili tx-fallback
- (1) getTransaction, ~5 kredi/istek (Helius standart cagri).
- (2) Tetik: gecelik sicil uretiminde "Program data:" ayristirilamayan
  LaunchObserved imzalari (fiili ~%40 lansman; yalniz YENI basarisizlar,
  cache'liler bedava). Tavan dolana kadar en yeni imzalardan geriye.
- (3) Cache: imza basina KALICI (data/gozlem/create_tx_cache.json);
  negatif sonuc da KALICI yazilir (ayni imza iki kez sorgulanmaz).
- (4) Gunluk TAVAN: SICIL_TX_TAVAN=500 istek (~2.500 kredi); asim
  sayilarak birakilir, kalan imzalar ertesi geceye kalir.
- (5) Toplama etkisi: 7.550 -> ~10.050/gun = butcenin %30'u; tavan
  25.000/gun icinde. Hiz: gecelik seri, 0.15 sn ara -> ~6.7/sn tepe,
  gunduz sensorleriyle CAKISMAZ (04:17 kosusu).

## Ek satir (26 Tem): R0 kadans artisi (kullanici karari, batarya orneklem hizi)
- GOZLEM_R0_MAX 25 -> 40: izlenen kume buyur, sensor olcum hacmi ~1.6x.
- Maliyet etkisi: konsantrasyon ~1.6x (az-ve-oz mod ayni kurallar),
  lp_kilit gunluk <=~64 istek (tavan 600 icinde), erken_alici token
  basi <=22 istek x ~64 terfi/gun ~= 1400 (tavan 2000 icinde),
  snapshot DexScreener 30'luk parti -> 2 parti/15sn (kota etkisi kucuk).
- Toplam kredi: ~10.1k -> ~13k/gun = butcenin ~%39'u; 25k tasarim
  tavani ve 33k gunluk limit icinde. Tavanlar DEGISMEDI; asim yine
  Throttled ile sayilarak birakilir.

## Hiz siniri kaniti (10 istek/sn)
- Konsantrasyon: 1 istek / >=10 sn (seri, tekli kuyruk) -> 0.1/sn
- LP kilit: terfi olayina bagli, seri, >=2 sn ara -> tepe 0.5/sn (nadir)
- Cuzdan yasi: tembel kuyruk, sabit 1 istek/2 sn tavani -> 0.5/sn
- Senkron/kasa/broker: olay bazli tekil istekler -> <0.2/sn
- En kotu es-zamanli toplam < 1.5 istek/sn << 10/sn. Ayrica her sensor
  429/hata gorunce ustel geri cekilir (60->960 sn), tekrar firtinasi yok.

## Cache kurallari (tekrar sorgu engelleme)
- getTokenSupply: mint basina 24 saat (pump tokenlerde arz sabit).
- Cuzdan yasi: KALICI (cuzdan yasi geriye gitmez); negatif sonuc 1 saat.
- LP mint kesfi ve yaratici adresi: pool/mint basina KALICI.
- Konsantrasyon: ayni mint icin >=15 dk araligindan sik OLCULMEZ
  (az-ve-oz mod); izlemeden dusen mint kuyruktan atilir.
- Basarisiz istek: ayni hedefe 1 saat yeniden denenmez (negatif cache).

## Yonetisim: YENI SENSOR = ONCE MALIYET ANALIZI (zorunlu politika)
Her yeni sensor bu dosyaya su kalemlerle SATIR EKLEMEDEN yayina alinamaz:
  (1) cagri tipi ve kredi katsayisi, (2) tetik formulu (adet/gun),
  (3) cache plani, (4) gunluk sabit TAVAN (asilinca birak + Throttled
  olayiyla SAYARAK birak: sessiz kirpma yasak), (5) toplamin 25.000/gun
  tavanina etkisi. Toplam tavani asan tasarim reddedilir veya baska
  sensorden butce kirpilir.

## Izleme
- Her sensor kendi istek sayacini ObserverHealth'e yazar (kind_sayi).
- Gunluk kredi ozetini haftalik rapora ekle; %60 kullanim gorulurse
  once tazeleme kadanslari gevsetilir (kalite kaybi en dusuk kalem).

## Ek satir (26 Tem): K1+K3 islem-akisi paketi (Observation Factory)
- (1) RPC: SIFIR (mevcut WSS aboneligi; dusen veri islenir hale gelir).
- (2) Disk: TradeAggregate zst tavani 40MB/gun; asim egiliminde 5dk
  agregat kademesi (onceden tanimli, Throttled beyanli).
- (3) CPU: eklenti hedefi ort <%5 / tepe <%15 tek cekirdek (kabul
  kriteri; ObserverHealth ile izlenir).
- (4) RAM: eklenti <40MB; tavanlar aktif_mint<=4000, kuyruk<=20k,
  cuzdan seti<=64/mint; ObserverHealth nesne sayaclarina eklenir.
