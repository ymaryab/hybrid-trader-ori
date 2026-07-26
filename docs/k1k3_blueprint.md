# K1+K3 Islem-Akisi Paketi Blueprint (26 Tem, onayli)

Icerik sohbette onaylanan 10 madde + Madde 11. Ozet basliklari:
akis (WSS on_ham -> sinirli kuyruk -> ortak anchor decode -> dakika
agregati -> omurga "islem" akisi), backpressure 3 kademe (dusur-say /
ornekleme / puls; hicbiri sessiz degil), kabul kriterleri (48h RSS<400M,
CPU<%5, decode>=%95, DexS capraz <%3, retro >=%90, kapi sayaclari).

## 11. SCHEMA VERSIONING (26 Tem kullanici sarti)
- TradeAggregate ve TUM yeni observation satirlari `sv` (schema_version)
  alani tasir; ilk surum sv=1.
- KIRICI degisiklikte surum artar; eski surum yazimi durur ama eski
  ARSIV ASLA donusturulmez.
- Decoder/parserlar surum-anahtarli tablodan secilir: gece analizleri
  satirin sv alanina gore dogru parser'i kullanir; bilinmeyen sv
  sessizce atlanmaz, sayilir.
- Anchor layout kayitlari da surumlu ayri dosyadadir
  (data/gozlem/anchor_kayit.json, kendi sv alaniyla): layout kesfi
  degisirse yeni kayit YENI surumle eklenir, eskisi silinmez.
