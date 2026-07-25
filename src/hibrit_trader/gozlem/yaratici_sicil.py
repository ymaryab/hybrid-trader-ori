"""Tier A sensor 3: yaratici sicili (Sprint 2, 25 Tem). Kredi maliyeti: 0.

Kaynak: omurgadaki LaunchObserved olaylarinin HAM loglari (pump.fun
Create anchor olayi "Program data:" base64 icinde name/symbol/uri +
mint + bondingCurve + user(yaratici) tasir) + EKG bolum arsivi.

Cikti (turev, yeniden uretilebilir; omurgaya yazilmaz):
  data/yaratici_sicili.json  : yaratici -> sicil
  data/yaratici_map.json     : mint -> yaratici (karar-ani sorgusu icin)

Sicil alanlari (kullanici kapsami, 25 Tem):
  lansman_n        : kac token olusturmus (census donemi)
  izlenen_n        : kaci EKG izlemesine girmis (+50/h tetigi)
  dead_born_n      : kaci hic tetiklenememis (dogdu, kimse donmedi)
  runner_n         : kaci ATH >= +%100 yapmis (izlenenler icinde)
  ort_yasam_dk     : izlenenlerde ilk-son gorulme araligi ortalamasi
  ort_ath_pct      : izlenenlerde tetik fiyatina gore ortalama tepe
  guven            : n/(n+5) — ornek yeterliligi (kullanici Confidence
                     gelenegi); dusukse sicil "fikir", yuksekse "kanit"
KAPSAM DURUSTLUGU: yalniz census baslangicindan (22 Tem) sonraki
lansmanlar; oncesi bilinemez ve bilinmiyor olarak kalir.
"""

from __future__ import annotations

import base64
import glob
import io
import json
import os
import subprocess
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

from .lp_kilit import _b58

# tx-fallback (25 Tem): butce satiri docs/sprint2_rpc_butcesi.md'de ONCE
# eklendi. Ayristirilamayan LaunchObserved imzalari gecelik kosuda
# getTransaction ile tamamlanir; imza basina KALICI cache (negatif dahil),
# gunluk tavan SICIL_TX_TAVAN, ag hatasi cache'LENMEZ (ertesi gece dener).
TX_TAVAN = int(os.getenv("SICIL_TX_TAVAN", "500"))


def tx_loglari(sig: str, url: str, timeout: float = 8.0) -> list[str]:
    """getTransaction -> meta.logMessages (tam loglar; WSS kesiklerini
    tamamlar). Ag/parse hatasi raise eder (cagiran cache'lemez)."""
    istek = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [sig, {"maxSupportedTransactionVersion": 0,
                         "encoding": "json"}]}).encode()
    r = urllib.request.urlopen(
        urllib.request.Request(url, istek,
                               {"Content-Type": "application/json"}),
        timeout=timeout)
    y = json.loads(r.read())
    return (((y.get("result") or {}).get("meta") or {})
            .get("logMessages")) or []


def create_ayristir(logs: list[str]) -> dict | None:
    """pump.fun Create anchor olayindan (mint, yaratici) cikar.
    Yapisal parse + 'pump' son-eki dogrulamasi (pump mintleri 'pump'
    ile biter): dogrulanamayan kayit None doner (yanlis veri yazilmaz)."""
    for ln in logs:
        if not ln.startswith("Program data: "):
            continue
        try:
            ham = base64.b64decode(ln[len("Program data: "):])
        except Exception:
            continue
        if len(ham) < 8 + 4:
            continue
        i = 8
        alanlar = []
        ok = True
        for sinir in (64, 32, 256):          # name, symbol, uri
            if i + 4 > len(ham):
                ok = False
                break
            n = int.from_bytes(ham[i:i + 4], "little")
            if n > sinir or i + 4 + n > len(ham):
                ok = False
                break
            i += 4 + n
        if not ok or i + 96 > len(ham):
            continue
        mint = _b58(ham[i:i + 32])
        yaratici = _b58(ham[i + 64:i + 96])
        if mint.endswith("pump"):
            return {"mint": mint, "yaratici": yaratici}
    return None


def _oku(yol):
    if yol.endswith(".zst"):
        p = subprocess.Popen(["zstd", "-dc", yol], stdout=subprocess.PIPE)
        fh = p.stdout
    else:
        fh = open(yol, "rb")
    for ln in fh:
        if ln.strip():
            try:
                yield json.loads(ln)
            except ValueError:
                continue
    fh.close()


def _tx_fallback(basarisiz: list[tuple[str, float]], veri: Path,
                 tx_getir=None) -> tuple[dict, dict]:
    """Ayristirilamayan imzalari kalici cache + gunluk tavanla tamamla.
    tx_getir(sig) -> logs (test enjeksiyonu; None = gercek RPC)."""
    cache_yol = veri / "gozlem" / "create_tx_cache.json"
    try:
        cache = json.loads(cache_yol.read_text())
    except (OSError, ValueError):
        cache = {}
    if tx_getir is None:
        from .konsantrasyon import URLS
        url = URLS[0] if URLS else None
        if url is None:
            return cache, {"fetch_n": 0, "hata_n": 0, "neden": "url_yok"}
        tx_getir = lambda s: tx_loglari(s, url)  # noqa: E731
    fetch_n = hata_n = 0
    for sig, _ts in sorted(basarisiz, key=lambda x: -x[1]):  # yeni once
        if sig in cache or fetch_n >= TX_TAVAN:
            continue
        try:
            logs = tx_getir(sig)
        except Exception:          # ag hatasi: cache'leme, ertesi gece
            hata_n += 1
            if hata_n >= 10:       # uc arizali: geceyi bosa harcama
                break
            continue
        fetch_n += 1
        cache[sig] = create_ayristir(logs)     # None da KALICI yazilir
        time.sleep(0.15)
    tmp = cache_yol.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    os.replace(tmp, cache_yol)
    return cache, {"fetch_n": fetch_n, "hata_n": hata_n}


def sicil_uret(veri: Path = Path("data"), tx_getir=None) -> dict:
    # 1) lansmanlar: mint -> (yaratici, dogum_ts)
    lansman = {}
    atlanan = 0
    basarisiz: list[tuple[str, float]] = []
    for yol in sorted(glob.glob(str(veri / "gozlem/events/*/*.sayim.jsonl*"))):
        for e in _oku(yol):
            if e.get("kind") != "LaunchObserved":
                continue
            r = create_ayristir((e.get("payload") or {}).get("logs") or [])
            if r is None:
                atlanan += 1
                if e.get("sig"):
                    basarisiz.append((e["sig"], e["ts_ms"] / 1000))
                continue
            lansman.setdefault(r["mint"],
                               (r["yaratici"], e["ts_ms"] / 1000))
    # 1b) tx-fallback: cache + gunluk tavan; kurtarilanlar birlesir
    kurtarilan = 0
    fb_ozet = {"fetch_n": 0, "hata_n": 0}
    if basarisiz:
        cache, fb_ozet = _tx_fallback(basarisiz, veri, tx_getir)
        for sig, ts in basarisiz:
            r = cache.get(sig)
            if r and r.get("mint"):
                if r["mint"] not in lansman:
                    kurtarilan += 1
                lansman.setdefault(r["mint"], (r["yaratici"], ts))
    # 2) EKG bolumleri: mint -> (ath_pct, yasam_dk)
    ekg = {}
    for ln in open(veri / "kosucu_ekg.jsonl"):
        if not ln.strip():
            continue
        try:
            t = json.loads(ln)
        except ValueError:
            continue
        m = t.get("token_address")
        p = float(t.get("price_usd") or 0)
        ts = float(t.get("ts") or 0)
        if not m or p <= 0:
            continue
        e = ekg.setdefault(m, {"ilk": p, "mx": p, "bas": ts, "son": ts})
        e["mx"] = max(e["mx"], p)
        e["son"] = max(e["son"], ts)
    # 3) yaratici bazinda birlestir
    sicil = defaultdict(lambda: {"lansman_n": 0, "izlenen_n": 0,
                                 "dead_born_n": 0, "runner_n": 0,
                                 "yasam": [], "ath": [],
                                 "son_lansman_ts": 0.0})
    mint_map = {}
    for mint, (yar, ts) in lansman.items():
        s = sicil[yar]
        s["lansman_n"] += 1
        s["son_lansman_ts"] = max(s["son_lansman_ts"], ts)
        mint_map[mint] = yar
        e = ekg.get(mint)
        if e is None:
            s["dead_born_n"] += 1
            continue
        s["izlenen_n"] += 1
        ath = 100 * (e["mx"] / e["ilk"] - 1)
        s["ath"].append(ath)
        s["yasam"].append((e["son"] - e["bas"]) / 60)
        if ath >= 100:
            s["runner_n"] += 1
    cikti = {}
    for yar, s in sicil.items():
        n = s["lansman_n"]
        cikti[yar] = {
            "lansman_n": n,
            "izlenen_n": s["izlenen_n"],
            "dead_born_n": s["dead_born_n"],
            "runner_n": s["runner_n"],
            "ort_yasam_dk": round(sum(s["yasam"]) / len(s["yasam"]), 1)
                            if s["yasam"] else None,
            "ort_ath_pct": round(sum(s["ath"]) / len(s["ath"]), 1)
                           if s["ath"] else None,
            "guven": round(n / (n + 5.0), 3),
            "son_lansman_ts": s["son_lansman_ts"],
        }
    ozet = {"uretim_ts": time.time(), "yaratici_n": len(cikti),
            "lansman_n": len(lansman), "ayristirilamayan": atlanan,
            "tx_kurtarilan": kurtarilan,
            "tx_fetch_n": fb_ozet.get("fetch_n", 0),
            "tx_hata_n": fb_ozet.get("hata_n", 0),
            "ekg_eslesen": sum(1 for m in lansman if m in ekg)}
    (veri / "yaratici_sicili.json").write_text(
        json.dumps({"ozet": ozet, "sicil": cikti}))
    (veri / "yaratici_map.json").write_text(json.dumps(mint_map))
    # as-of ozellik hesabi icin ham lansman listesi (25 Tem, sizinti
    # duzeltmesi: q veri seti yalniz dogum-oncesi lansmanlari sayar)
    (veri / "yaratici_lansmanlar.json").write_text(json.dumps(
        {m: [yar, ts] for m, (yar, ts) in lansman.items()}))
    return ozet


if __name__ == "__main__":
    print(json.dumps(sicil_uret(), indent=1))
