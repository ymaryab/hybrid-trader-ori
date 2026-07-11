# Denetim Klasoru

Amac: vergi ve denetim icin islem kayitlarini tek yerde toplamak.

Icerik:

- `YYYY-MM_defter.csv`: aylik kapali islem defteri. Ay devrinde otomatik
  yazilir (panel icindeki denetim dongusu). Manuel calistirma:
  `.venv/bin/python -m hibrit_trader.denetim 2026-07`
  Kolonlar: tarih, motor, cift, giris_fiyati, cikis_fiyati, miktar,
  pnl_usd, tx_imzasi. Paper islemlerde tx imzasi bos kalir.
- `cuzdan_kimlik.md`: bot cuzdaninin public adresi ve fonlama kaynagi notu.
- Binance TR ekstre PDF'leri bu klasore ELLE eklenir (git'e commit edilmez,
  sadece bu iki md dosyasi izlenir).

Bu klasorde ASLA private key, seed veya keypair dosyasi bulunmaz.
