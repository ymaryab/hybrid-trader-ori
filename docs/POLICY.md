# hibrit-trader Politika Dokumani

(tarih: 2026-06-22. Rakamlar in-sample snapshot, ~112 trade paper. Cok-rejim onayi bekliyor.)

## Mevcut Durum

net -%8.7, PF 0.98, kronik basa-bas-alti, paper.

## A) Kaybeden Kisimlar

- flash -1525 (baskin kanama, gap-down, cikisla kesilemez)
- friction ~%98 (TODO: dolar olarak olc, flash -1525 ile sirala, #1 kaldirac olabilir)
- <2dk churn -744 (hizli giris-cikis, round-trip maliyeti edge'i yiyor)

## B) Olu Agirlik

- giris alfasi r~0
- yuksek-skor -0.291 (NOT: r~0 degil, hafif anti-predictive, skor aktif zararli olabilir, kesif gerek)
- scratch-grace (:228) -53

## C) Calisan / Dokunma

- cikis motoru +2018 (gercek edge, tartisma disi)
- C1 (genesis yari-boyut)
- guvenlik filtresi (holder vb sert filtre)

## 6 Ilke

1. Giris-alfasini dondur.
2. Scale-in: KOSULLU. Exit-log runner-zaman-profili runner'larin yavas oldugunu dogrularsa uygulanir, yoksa hayir. Su an hipotez, doktrin degil.
3. Friction-kesimi: az ve uzun-tutulan trade, churn'u kes.
4. Ucuz stop'lar additif degil, RESIDUAL etkiyi olc (flash gap cogu cikisi deler).
5. Cikisa dokunma.
6. Tek-degisken + cok-rejim. Durust tavan PF 1.0-1.3.

## KILL-CRITERION

- Levereler sonrasi PF < 1.0 ise CANLI YOK.
- Canli bari: cok-rejim PF > 1.1 istikrarli (en az N trade, M ayri rejim uzerinde; TODO: N ve M tanimla) VE friction modellenmis.

## Disiplin

Forward gercektir, in-sample yon verir. Kucuk ornekleme guvenme. Kasa gecikmeli yer gercegi. Her degisiklik tek basina forward'da dogrulanir.

## Friction Sadakat Bulgusu (2026-06-22, read-only backfill)

Olcum duzeltmesi: paper fill slippage'i %0.38 modelliyor; botun kendi canli quote estimator'u ~%5.27. Paper PnL friction-kor. A) bolumundeki "friction TODO" boylece cevaplandi. PAPER_SLIPPAGE_PCT knob eklendi (local commit, ileriye donuk, geri alinabilir). Mevcut ~120 trade %5.27'ye analitik backfill edildi (brut = net + uygulanan per-trade friction, sonra yeniden uygula).

%5.27 friction sonuclari:
- Toplam derin negatif, PF 0.10. Paper'daki PF ~0.7-0.98 friction-korlukten. KILL-CRITERION (PF < 1.0) kesin tetik.
- Friction-invariant kaybedenler (her ayarda buyuyerek): <2dk churn, pump_fun sinif, kucuk-move (mfe <%15 = trade'lerin %78'i, PF 0.00).
- Tek yasayan segment: buyuk-runner (mfe >=%15) PF 6.06. Cikis motoru kazanan dolarin %99'unu runner'lardan aliyor (motor saglam, sorun girise dair).
- Holder filtresi: dolar edge robust pozitif (+255 -> +727) ama filtreli kitap mutlak hala ZARAR; kayip-azaltici, kar-yapici degil.

Runner tahmini (giris-ani ayrac):
- VAR: moonshot (Cohen d +0.95), dusuk-likidite (-0.67), genc-yas (-0.66). Skor ayirmiyor (d +0.12, teyit).
- Ama YETMIYOR: en iyi alt-kume (moon>=65 & age<=12h & dusuk-liq) PF 0.32, runner %54. Hicbir esik/kombo PF > 1.0 gecmiyor. 0.32 ile 1.0 arasi ~3x kapanmayan bosluk.

Yapisal tuzak: runner = dusuk-likidite; dusuk-likidite = yuksek-friction. Runner'i yaratan sey friction'i olduren seyle ayni. Ayni anda yuksek-frekans + dusuk-friction olamiyor.

Sonuc: gercekci friction'da bu konsept (yuksek-frekans pump_fun runner-avi) mevcut veride hicbir giris seciminiyle pozitif beklentiye ulasmiyor. Eksik olan bir filtre degil, yapinin kendisi. Gercek kaldiraclar: (a) gercek execution friction'ini olc (%5.27 botun tahmini, tek sayi degil), (b) dusuk-frekans + yuksek-likidite pair'lere kay, ya da yuksek-frekans pump_fun'dan vazgec.

## V-serisi final (05 Tem 2026)

- Fren kanitlandi: +$66, 3/3 dogru.
- Tavansizlik faturasi: -$298.
- 20dk mutlak tavan curudu: -$154.
- Ortak kaybettiren parametre: sol_h1 0-0.4 bandi (v7 rejim esigi 0.5'e cekildi).
- Aktif filo: v4 / v7 / v9 / v10 / X1. Durdurulan: golge, v6, v8 (state/trades korunur, panel arsivinde).
