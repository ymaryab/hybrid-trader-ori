#!/bin/bash
# Gece edge/analiz zinciri (25 Tem): sicil (04:17) SONRASI 04:47'de kosar.
# Her adim bagimsiz: biri duserse digerleri devam eder; ozet log'a akar.
# Uretilenler: q_veri_seti.jsonl, kill_bataryasi_sonuc.json (DRY-RUN),
# edge_rapor.json, kural_karnesi.json
cd /home/bot/yz/hybrid-trader-ori
LOG=data/edge_gece.log
{
  echo "=== gece zinciri $(date -u +%FT%TZ) ==="
  python3 scripts/q_veri_seti.py || echo "q_veri_seti HATA ($?)"
  python3 scripts/kill_bataryasi.py | head -4 || echo "batarya HATA ($?)"
  python3 scripts/edge_rapor.py --saat 24 >/dev/null \
    && echo "edge_rapor OK" || echo "edge_rapor HATA ($?)"
  python3 scripts/kural_karnesi.py --gun 7 >/dev/null \
    && echo "kural_karnesi OK" || echo "kural_karnesi HATA ($?)"
} >> "$LOG" 2>&1
