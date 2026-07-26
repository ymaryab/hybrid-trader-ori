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
  python3 scripts/edge_rapor.py --saat 24 --kaynak anlik >/dev/null \
    && echo "edge_rapor_anlik OK" || echo "edge_rapor_anlik HATA ($?)"
  python3 scripts/sadakat_rapor.py --motor yz --gun 1 --kaynak anlik \
    >/dev/null && echo "sadakat OK" || echo "sadakat HATA ($?)"
  python3 scripts/kural_karnesi.py --gun 7 >/dev/null \
    && echo "kural_karnesi OK" || echo "kural_karnesi HATA ($?)"
  python3 scripts/kacis_payi.py --motor yz --gun 1 >/dev/null \
    && echo "kacis_payi OK" || echo "kacis_payi HATA ($?)"
  python3 scripts/veto_degeri.py --saat 24 >/dev/null \
    && echo "veto_degeri OK" || echo "veto_degeri HATA ($?)"
  python3 scripts/calkalama_olcum.py --saat 24 >/dev/null \
    && echo "calkalama OK" || echo "calkalama HATA ($?)"
  python3 scripts/golge_defter.py --saat 24 >/dev/null \
    && echo "golge_defter OK" || echo "golge_defter HATA ($?)"
  python3 scripts/alpha_kapilari.py >/dev/null \
    && echo "alpha_kapilari OK" || echo "alpha_kapilari HATA ($?)"
  python3 scripts/edge_replay_dogrula.py --saat 24 >/dev/null \
    && echo "edge_replay OK" || echo "edge_replay HATA ($?)"
  python3 scripts/sadakat_rapor.py --motor r2 --gun 1 --kaynak anlik \
    >/dev/null && echo "sadakat_r2 OK" || echo "sadakat_r2 HATA ($?)"
} >> "$LOG" 2>&1
