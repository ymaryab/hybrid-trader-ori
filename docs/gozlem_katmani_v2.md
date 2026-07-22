# Gözlem Katmanı v2 (Observation Layer v2) Tasarım Dokümanı

Tarih: 22 Tem 2026. Durum: TASARIM, kod yok, onay bekliyor.
Gerekçe: 30 günlük adli analiz zinciri, tuzak bandı (+5..20) tokenlerinin
karar anında mevcut kayıtlı verilerle ayırt EDİLEMEDİĞİNİ gösterdi.
Eksik olan strateji değil, karar anı enformasyonu.

Donanım gerçeği: Hetzner VPS, 2 çekirdek, 3.8GB RAM (~2.6 boş),
33GB boş disk, motorlar aynı makinede. Ücretli Geyser/indexer YOK.
Veri kaynakları: public RPC (HTTP+WSS), DexScreener, Jito public API.

## 0. Dürüstlük ön beyanı (neyin sözü verilmiyor)

- Nanosaniye yok. HTTP/WSS üzerinden gelen veride gerçek hassasiyet
  milisaniye + zincir slotu. Her olayda iki saat tutulur: `slot`
  (zincir gerçeği) ve `ts_ms` (bizim alım anımız). İkisinin farkı
  gecikme ölçümüdür, gizlenmez.
- Tam holder sayısı public RPC ile sayılamaz (mint bazlı
  getProgramAccounts ağır/kısıtlı). Yaklaşık holder = izleme
  başlangıcından itibaren kümülatif benzersiz alıcı - çıkanlar.
  Kesin sayım ancak ücretli indexer ile olur; alan `holder_approx`
  diye adlandırılır, kesinmiş gibi sunulmaz.
- AMM'de spread yok; `spread` yerine derinlik bazlı kayma tahmini
  (likidite + işlem boyu fonksiyonu) türetilir.
- Attention (Twitter/Telegram/izlenme) ücretsiz API'lerle yok.
  Tier C, varsayılan kapalı.
- Cüzdan geçmişi geriye dönük tam taranamaz (rate limit). İtibar
  skoru İLERİYE dönük birikir; soğuk başlangıç 18 günlük EKG token
  listesinden sınırlı tembel backfill ile desteklenir.

## 1. Mimari: üç halka + olay omurgası

Tek ilke: HAM OLAY tek gerçek kaynak; her türev çevrimdışı yeniden
üretilebilir.

```
   [chain-ws toplayıcı]   [poll toplayıcı]    [motor musluğu]
    logsSubscribe/WSS      DexScreener+RPC     motor süreçleri
         |                      |                    |
         v                      v                    v
   +---------------- OLAY OMURGASI (append-only) ----------------+
   | events/YYYYMMDD/HH.<akış>.jsonl  -> saat başı zstd + manifest|
   +--------------------------------------------------------------+
         |                                   |
         v (günlük, yeniden üretilebilir)    v (anlık, RAM)
   [DuckDB/Parquet sorgu katmanı]      [durum önbelleği: kayıt defterleri]
   feature store, analizler            token/cüzdan/yaratıcı registry
```

Halkalar (kapsam yönetimi, kaynak bütçesinin kalbi):
- **R0 izlenen**: filonun pozisyon açtığı veya EKG tetiklemiş tokenler
  (anlık ~20-40). Tam sadakat: swap seviyesi akış + 5 sn anlık görüntü.
- **R1 aday evreni**: tarayıcının gördüğü trending/arama evreni
  (~150-300 token). 60 sn anlık görüntü, swap akışı yok.
- **R2 piyasa sayımı**: TÜM lansmanlar. Sadece doğum/mezuniyet/havuz
  olayları (logsSubscribe: pump.fun + PumpSwap/Raydium programları).
  Hafif ama piyasa taban çizgisinin tek gerçek kaynağı.

Terfi/tenzil: R2'de doğan token tarayıcı evrenine girince R1'e,
EKG tetiği veya motor girişiyle R0'a terfi eder. R0'dan çıkış:
pozisyon kapalı + 6 saat sessizlik. Terfi/tenzil de birer olaydır
(`TrackingPromoted`, `TrackingDemoted`), kapsam tarihçesi kaybolmaz.

## 2. Olay şeması

Zarf (her olayda zorunlu):
```
v: 1                şema sürümü
seq: int            akış içi monoton sıra (boşluk = kayıp kanıtı)
ts_ms: int          alım zamanı (ms, UTC)
slot: int|null      zincir slotu (zincir olaylarında zorunlu)
sig: str|null       işlem imzası (zincir olaylarında) + ix indeksi
kind: str           olay tipi
token: str|null     mint adresi
src: str            kaynak (ws-rpc1, dexs, motor:yz ...)
payload: {...}      tipe özgü tam ham veri, KIRPILMADAN
```
Tekilleştirme anahtarı: (sig, ix) varsa o; yoksa (kind, token, slot,
payload_hash). Aynı olay iki kaynaktan gelirse ikisi de yazılır
(src farklı), tekilleştirme sorgu katmanında yapılır: ham katman
asla "düzeltilmez".

Olay tipleri ve kaynağı:

| tip | kaynak | halka | not |
|---|---|---|---|
| TokenDiscovered | tarayıcı | R1 | ilk görüş, tam DexScreener payload |
| PoolCreated | logsSubscribe | R2 | lansman sayımının temeli |
| LiquidityAdded / LiquidityRemoved | ws + poll | R0 (ws), R1 (poll farkı) | LP mint değişimi |
| SwapBuy / SwapSell | ws (tx parse) | R0 | cüzdan, miktar, fiyat etkisi, slot |
| HolderDelta | türev-olay | R0 | swap akışından; HolderCreated/Exited yerine net delta, yaklaşıklık beyanlı |
| WalletFirstSeen | ws | R0 | izlenen tokenle ilk etkileşim |
| WhaleBuy/Sell, SmartMoneyBuy/Sell | türev-olay | R0 | cüzdan registry eşiğine göre etiket; ham swap zaten var |
| CreatorBuy/Sell | ws | R0 | yaratıcı adresi eşleşen swap |
| BundleDetected, JitoBundle | türev-olay + Jito API | R0 | aynı-slot çoklu alım + ortak fonlayıcı sezgiseli; sezgisel olduğu payload'da beyan |
| PriceUpdate | poll (fast_price) | R0/R1 | mevcut hızlı fiyat altyapısı olaylaştırılır |
| MarketCapUpdate, VolumeUpdate | poll | R1 | DexScreener alanları |
| ATH / NewHigh / NewLow | türev-olay | R0 | anlık görüntücü üretir, ham fiyattan yeniden üretilebilir |
| EngineSignal / EngineEntry / EngineExit | motor musluğu | hepsi | karar anı TAM bağlam: motorun gördüğü her girdi alanı payload'a |
| GapDetected | toplayıcı | hepsi | WSS kopması/aşırı yük: süreklilik yalanı yok |
| TrackingPromoted / Demoted | kapsam yöneticisi | hepsi | halka geçişleri |

Kritik ilke: WhaleBuy, ATH, HolderDelta gibi türev-olaylar yalnızca
kolaylık içindir, her zaman ham kaynağından (swap/price) yeniden
üretilebilir ve üreten kural sürümü payload'da taşınır.

## 3. Anlık görüntü (snapshot) deposu

- R0: 5 sn kadans. R1: 60 sn. Aynı zarf, `kind: Snapshot`.
- Alanlar: price, mcap, fdv, liq, hacim (pencere ham toplamları değil
  DexScreener'ın verdiği ham alanlar aynen), buy/sell count (varsa),
  holder_approx, top10_pct, top20_pct, largest_wallet_pct,
  creator_pct, lp_pct, burn_pct, lock durumu, smart/fresh/whale cüzdan
  sayıları (registry'den), yaş, tespitten süre, önceki ATH'den süre,
  kayma tahmini (100$/500$/2000$ işlem boyu için).
- top10/20/largest: dakikada bir getTokenLargestAccounts (hafif, tek
  çağrı) → aradaki 5 sn'lik kareler son değeri taşır ve
  `top_age_ms` alanıyla bayatlığını beyan eder.
- Sıralı pencere metrikleri (10s/30s/60s/5dk order flow, hız, ivme,
  akış ivmesi): DEPOLANMAZ. Bunlar türevdir; swap akışından çevrimdışı
  hesaplanır (gereksinim 10 ile tutarlı). Motorlar canlıda isterse
  aynı kural sürümüyle RAM'de hesaplar, diske türev yazılmaz.

## 4. Cüzdan gözlemi (registry, append-only gözlem + yeniden kurulabilir özet)

- Ham katman: her cüzdanın izlenen tokenlerdeki her swap'ı zaten
  olay omurgasında. Registry bunun özetidir, SQLite'ta tutulur ve
  TAMAMEN yeniden kurulabilir (kaynak: olaylar).
- Alanlar: ilk görülme, etkileşim sayısı, gerçekleşen pnl (izlenen
  tokenlerde, FIFO), kazanma oranı, medyan tutma, giriş zamanlaması
  (lansmandan kaçıncı dakika/kaçıncı alıcı), rug katılımı, koşucu
  katılımı, itibar skoru (sürümlü formül), küme id (ortak fonlayıcı
  analizi), bayraklar: sniper (ilk 2 slot alıcısı), farmer, mm, dev.
- Bayraklar ve skorlar gecelik toplu işte hesaplanır, `hesap_v` sürüm
  alanıyla yazılır; formül değişince tüm geçmiş yeniden hesaplanır
  (ham olaylar durduğu için mümkün).
- Soğuk başlangıç: 18 günlük EKG + defter tokenlerinin alıcı listeleri
  tembel backfill (getSignaturesForAddress, düşük öncelikli kuyruk,
  saniyede ≤2 istek). Backfill tamamlanana kadar skorlar
  `guven: dusuk` etiketi taşır.

## 5. Yaratıcı gözlemi

- Kaynak: R2 sayımı her lansmanla yaratıcı adresini verir. O andan
  itibaren her yaratıcının: lansman sayısı, koşucu oranı, rug oranı,
  medyan ATH/ömür/likidite/holder_approx, ortalama LP, önceki
  projeler zinciri (aynı adres + fonlama izi).
- Geriye dönük: sınırlı (sadece elimizdeki 119 + EKG tokenlerinin
  yaratıcıları backfill edilir). Gerçek güç 2-4 hafta birikimden
  sonra gelir; bu beklenti dürüstçe kabul edilir.

## 6. Likidite dinamiği

- R0: LP havuz hesabı değişimleri ws'ten olay olarak (Added/Removed),
  kilit programları (bilinen locker adresleri) + LP mint burn kontrolü
  keşifte ve her LiquidityAdded'da.
- LP hız/ivme/oynaklık: türev, çevrimdışı.
- Unlock takvimi: kilit hesabından okunabiliyorsa `UnlockScheduled`
  olayı; okunamıyorsa alan boş, uydurulmaz.

## 7. Piyasa yapısı ve 8. piyasa bağlamı

- Piyasa yapısı metrikleri (HH/HL, salınım, impuls, geri çekilme,
  toparlanma hızı, momentum kalıcılığı, kırılım kalitesi): tamamı
  türev; feature store'da sürümlü tanımlarla, ham fiyat/swap'tan.
- Piyasa bağlamı: dakikada bir `MarketContext` olayı: SOL fiyat/vol/
  momentum, BTC momentum (mevcut btc_macro beslemesi), öncelik ücreti
  (getRecentPrioritizationFees), Jito tip taban API'si, ve R2
  sayımından türeyen üç altın metrik: lansman/saat, koşucu doğumu/saat,
  rug/saat, piyasa genişliği. ETH momentumu: Tier C, eklemiyoruz.
- Her EngineSignal/Entry payload'ına o anki MarketContext gömülür
  (karar anı bağlamı, join gerektirmeden).

## 9. Attention

Ücretsiz güvenilir kaynak yok. Tasarımda yer ayrıldı (kind alanları
rezerve), toplayıcı YAZILMAYACAK. Tier C.

## 10. Feature store

- Ham olaydan türetilen her özellik: (tanım sürümü, girdi olay
  aralığı, kod hash) üçlüsüyle üretilir → aynı üçlü aynı sonucu verir
  (yeniden üretilebilirlik sözleşmesi).
- Günlük DuckDB/Parquet çıktıları `derived/` altında; SİLİNEBİLİR
  sınıfı: her an ham katmandan yeniden kurulur.

## 11. Depolama stratejisi

- Format: satır başı JSON, akış başına saatlik segment:
  `events/YYYYMMDD/HH.<akış>.jsonl` → saat kapanınca zstd (seviye 9)
  + `manifest.jsonl` satırı: dosya, seq aralığı, satır sayısı, sha256.
- Append-only, üzerine yazma yok, birleştirme ham katmanda yok.
- Replay sözleşmesi: herhangi bir t anı için dünya durumu =
  segmentleri seq sırasıyla oynat; GapDetected olayları boşlukları
  açıkça işaretler.
- fsync politikası: EngineEntry/Exit ve LiquidityRemoved anında
  fsync; kalanı 1 sn'lik grup fsync (mevcut WAL deneyimi).

## 12. Kapasite tahminleri (2 çekirdek / 3.8GB / 33GB gerçeğiyle)

Hacim (sıkıştırılmış, zstd ~8-12x):

| akış | ham/gün | sıkışık/gün |
|---|---|---|
| R2 sayım (~20-30k lansman + havuz olayı) | ~8 MB | ~1 MB |
| R0 swap akışı (~100-200k olay) | ~60 MB | ~7 MB |
| R0 snapshot 5sn (~500k satır) | ~180 MB | ~16 MB |
| R1 snapshot 60sn (~300k satır) | ~100 MB | ~9 MB |
| cüzdan/yaratıcı gözlem + bağlam + motor | ~15 MB | ~2 MB |
| **toplam** | **~360 MB** | **~35 MB** |

→ ayda ~1.1 GB, 33GB disk ile 2+ yıl ham saklama. Retention:
ham katman süresiz; `derived/` istenildiğinde silinir; SQLite
registry'ler yeniden kurulabilir sınıfı.

CPU/RAM bütçesi (motorlar aynı makinede, tavan sözleşmesi):
- chain-ws toplayıcı: ortalama %5-10 tek çekirdek, tepe %30; RSS ~150MB
- poll toplayıcı + snapshotter: %5-10; RSS ~120MB (mevcut fast_price
  havuzunu paylaşır, YENİ istek eklemez, olanı olaylaştırır)
- gecelik işler (sıkıştırma, registry, feature): 03:00-05:00 UTC
  düşük öncelik (nice 19), %50 tek çekirdek tavan
- toplam ek RSS tavanı 500MB; aşımda önce cüzdan backfill kuyruğu
  durur, sonra R1 kadansı 120 sn'ye düşer, R0 ve R2 ASLA kısılmaz.
  Her kısılma GapDetected/Throttled olayı üretir.

## 13. Toplama öncelikleri

- **P0 (ilk hafta)**: motor musluğu (EngineSignal/Entry/Exit tam
  bağlamla), R2 lansman sayımı, R0 swap akışı, R0 5sn snapshot,
  MarketContext. Bu beşli, "karar anında ne vardı" sorusunun bir
  daha asla cevapsız kalmamasının asgarisi.
- **P1 (2-3. hafta)**: largest-accounts yoklayıcı (konsantrasyon),
  LP kilit/burn kontrolü, cüzdan registry + WalletFirstSeen,
  yaratıcı registry, Jito/öncelik ücreti.
- **P2 (4+ hafta)**: bundle sezgiseli, cüzdan sınıflandırma toplu
  işleri, küme analizi, tembel backfill, R1 kayma tahmini.

## Sensör sınıflandırması

Gerekçe çıpası: adli bulgu "tuzak vs koşucu t0'da ayrıştırılamadı";
Tier A = tam o anda ayrım gücü taşıması en muhtemel sensörler
("bir sonraki alıcı kim" modeli).

**Tier A (yüksek alfa olasılığı)**
1. R0 swap akışı + erken alıcı kimliği (sniper/fresh/smart karışımı,
   ilk 30 dk)
2. Yaratıcı geçmişi (koşucu/rug oranı): lansman anında mevcut tek
   tarihsel sinyal
3. Holder konsantrasyonu (top10/largest/creator payı) + LP kilit/burn
4. LiquidityRemoved gerçek zamanlı (rug'un öncü göstergesi)
5. R2 sayımı (lansman/koşucu-doğumu/rug oranı taban çizgileri:
   rejim tespitinin ham kaynağı)
6. Bundle/JitoBundle tespiti (orkestre edilmiş lansman = tuzak adayı)

**Tier B (faydalı)**
7. 5sn snapshot serisi (piyasa yapısı türevlerinin hammaddesi)
8. MarketContext (SOL/BTC momentum, ücretler; tek başına zayıf,
   etkileşimlerde değerli)
9. Cüzdan itibar skoru (birikince A'ya terfi edebilir; ilk aylarda B)
10. Kayma tahmini / derinlik (execution kalitesi)
11. WalletFirstSeen / taze cüzdan oranı

**Tier C (olsa iyi)**
12. Attention kaynakları (veri yok, rezerve)
13. ETH momentumu, FDV (mcap ile mükerrer), izlenme/watchlist
14. UnlockScheduled (nadiren okunabilir)

## Açık onay soruları (uygulama ÖNCESİ kullanıcıya)

1. P0 kapsamı ve kaynak tavanları (500MB RSS, %20 ortalama CPU) onay?
2. R0 tanımı (pozisyon + EKG tetiği) yeterli mi, genişletilsin mi?
3. Dondurma taahhüdü: YZ/YZn1 motor koduna dokunulmaz; motor musluğu
   yalnız YENİ süreçte dinleme yapacak şekilde tasarlandı, motor
   içine kanca eklenmesi YZn1 100 işlem sonrası mı yapılsın?
