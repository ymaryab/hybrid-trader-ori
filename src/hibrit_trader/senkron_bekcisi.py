"""Cuzdan-motor senkron bekcisi (18 Tem).

Aktif canli motorun state.positions'undaki canli_miktar>0 pozisyonlari,
Solana RPC'deki gercek token bakiyeleriyle karsilastirir. Fark varsa
telegram'a UYARI (5dk dedup).

Amac: motor "hayali acik poz" tutar hale gelirse (V7HIZLI 16 Tem olayi
gibi) kullaniciya erken haber ver.

SENKRON_ENABLED=0 ile kapatilir.
Env: SENKRON_PERIOD_SEC (default 60), SENKRON_DEDUP_SEC (default 300),
     SENKRON_EKSIK_ORAN (default 0.5 - beklenen*0.5'dan az ise UYARI),
     SENKRON_TAZE_POZ_SEC (default 120 - daha taze pozisyonlar atlanir).

Yanlis alarm korumasi (20 Tem): taze alimda RPC token hesabini henuz
indekslememis olabilir; 120s'den taze pozisyon kontrol edilmez ve alarm
ancak ART ARDA IKI turda ayni uyumsuzluk gorulurse calar.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

CUZDAN = os.getenv("SENKRON_CUZDAN",
                   "DZXZGD5FURZDwa5BWByxxd7iLdCvGxSCy6RWHsgupaYa")
PERIOD_SEC = float(os.getenv("SENKRON_PERIOD_SEC", "60"))
DEDUP_SEC = float(os.getenv("SENKRON_DEDUP_SEC", "300"))
EKSIK_ORAN = float(os.getenv("SENKRON_EKSIK_ORAN", "0.5"))
TAZE_POZ_SEC = float(os.getenv("SENKRON_TAZE_POZ_SEC", "120"))
DATA = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
TOKEN_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

_son_uyari: dict[str, float] = {}
_supheli: dict[str, float] = {}  # key -> ilk uyumsuzluk ts (iki-tur teyidi)


def _rpc(method: str, params: list, timeout: float = 15.0):
    # 18 Tem: multi-RPC fallback (primary fail -> sirada URL)
    from hibrit_trader.rpc_multi import rpc_post
    return rpc_post(method, params, timeout=timeout)


def _cuzdan_token_bakiye(mint: str) -> float | None:
    """Belirli bir mint icin cuzdan toplam bakiye. Mint filter -> Token-2022 dahil.
    Hata halinde None (senkron kararsiz - uyari basmaz)."""
    try:
        r = _rpc("getTokenAccountsByOwner",
                 [CUZDAN, {"mint": mint}, {"encoding": "jsonParsed"}])
        accts = r.get("result", {}).get("value", []) or []
        toplam = 0.0
        for a in accts:
            info = a["account"]["data"]["parsed"]["info"]
            toplam += float(info["tokenAmount"].get("uiAmount") or 0)
        return toplam
    except Exception as e:
        log.warning("SENKRON: %s icin cuzdan okunamadi: %r", mint[:8], e)
        return None


def _uyar(mesaj: str, kanal_key: str) -> None:
    """Log CRITICAL + telegram notify (dedup)."""
    now = time.time()
    if now - _son_uyari.get(kanal_key, 0.0) < DEDUP_SEC:
        return
    _son_uyari[kanal_key] = now
    log.critical("SENKRON UYARI: %s", mesaj)
    try:
        from hibrit_trader.killswitch import notify
        notify(f"⚠️ SENKRON UYARI: {mesaj}")
    except Exception:
        log.warning("SENKRON telegram gonderilemedi", exc_info=True)


def check_once() -> None:
    canli_motor = os.getenv("CANLI_MOTOR", "v7").strip().lower()
    sp = DATA / f"{canli_motor}_state.json"
    if not sp.exists():
        return
    try:
        s = json.loads(sp.read_text())
    except Exception:
        return

    # state'teki canli_miktar>0 pozisyonlari icin her mint icin ayrı sorgu
    # (mint filtresi Token-2022 dahil TUM hesaplari getirir)
    su_tur_supheli: set[str] = set()
    for p in s.get("positions", []) or []:
        cm = float(p.get("canli_miktar") or 0)
        if cm <= 0:
            continue
        mint = p.get("token_address")
        pair = p.get("pair", "?")
        if not mint:
            continue
        opened_ts = float(p.get("opened_ts") or 0)
        if opened_ts and time.time() - opened_ts < TAZE_POZ_SEC:
            continue  # taze alim: RPC indeks gecikmesi, bu tur atla
        gercek = _cuzdan_token_bakiye(mint)
        if gercek is None:
            continue  # RPC hata, atla
        if gercek < cm * EKSIK_ORAN:
            key = f"{canli_motor}:{mint}:eksik"
            su_tur_supheli.add(key)
            if key not in _supheli:
                # ilk tur: alarm yok, sonraki turda teyit beklenir
                _supheli[key] = time.time()
                log.warning("SENKRON suphe (teyit bekleniyor): %s %s "
                            "state=%.2f cuzdan=%.2f", canli_motor.upper(),
                            pair, cm, gercek)
                continue
            _uyar(f"{canli_motor.upper()} {pair}: state={cm:.2f} "
                  f"cuzdan={gercek:.2f} (hayali poz - motor takip ediyor "
                  f"ama cuzdanda YOK)", key)
    # uyumsuzlugu gecen (duzelen/kapanan) pozisyonlarin suphe kaydini sil
    for key in list(_supheli):
        if key not in su_tur_supheli:
            _supheli.pop(key, None)


def run_forever() -> None:
    log.warning("SENKRON BEKCISI basladi: cuzdan %s..%s, period=%.0fs, "
                "dedup=%.0fs, eksik_oran=%.0f%%",
                CUZDAN[:6], CUZDAN[-4:], PERIOD_SEC, DEDUP_SEC, EKSIK_ORAN * 100)
    # Ilk kontrol icin kucuk gecikme (motor start_up bekle)
    time.sleep(20.0)
    while True:
        try:
            check_once()
        except Exception:
            log.exception("SENKRON check exception")
        time.sleep(PERIOD_SEC)
