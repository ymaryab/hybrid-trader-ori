"""Multi-RPC fallback (18 Tem) — primary fail (503/429/timeout) auto-switch.

Kullanim:
    from hibrit_trader.rpc_multi import rpc_post
    data = rpc_post("getBalance", ["ADDR"], timeout=15)

Env:
    SOLANA_RPC_URL           : birincil (opsiyonel)
    SOLANA_RPC_FALLBACK_URLS : virgul ayrimli fallback (opsiyonel)
    RPC_MULTI_DISABLE=1      : fallback devre disi (sadece primary)

Davranis:
    Primary URL denenir. 503/429/timeout/connect_error alirsa sirada URL.
    Tum URL'ler basarisiz olursa RuntimeError (motorun kendi hata yolu devrede).
    Ilk basarili yaniti dondurur.

Log:
    Her fallback tetigi log.warning (5dk dedup, spam engeli).
    Tum URL'ler fail: log.critical + telegram (5dk dedup).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DEFAULT_URLS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
    "https://solana.drpc.org",
)

_LOCK = threading.Lock()
_son_uyari: dict[str, float] = {}
_DEDUP_SEC = 300.0


def _urls() -> list[str]:
    primary = os.getenv("SOLANA_RPC_URL", "").strip()
    extra = [u.strip() for u in os.getenv("SOLANA_RPC_FALLBACK_URLS", "").split(",")
             if u.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for u in ([primary] if primary else []) + extra + list(_DEFAULT_URLS):
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out or list(_DEFAULT_URLS)


def _uyar(mesaj: str, key: str, kritik: bool = False) -> None:
    now = time.time()
    with _LOCK:
        if now - _son_uyari.get(key, 0) < _DEDUP_SEC:
            return
        _son_uyari[key] = now
    if kritik:
        log.critical("RPC MULTI: %s", mesaj)
        try:
            from hibrit_trader.killswitch import notify
            notify(f"⚠️ RPC HATA: {mesaj}")
        except Exception:
            pass
    else:
        log.warning("RPC MULTI: %s", mesaj)


def rpc_post(method: str, params: list, *, timeout: float = 15.0,
             http: httpx.Client | None = None) -> Any:
    """Primary + fallback URL denemesi. Ilk basariliyi dondurur.
    Fail (503/429/timeout/connect): sirada URL. Hepsi fail: RuntimeError."""
    if os.getenv("RPC_MULTI_DISABLE", "0") == "1":
        urls = [os.getenv("SOLANA_RPC_URL") or _DEFAULT_URLS[0]]
    else:
        urls = _urls()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    hatalar = []
    close_client = False
    if http is None:
        http = httpx.Client(timeout=timeout)
        close_client = True
    try:
        for i, url in enumerate(urls):
            try:
                r = http.post(url, json=payload, timeout=timeout)
                if r.status_code in (429, 502, 503, 504):
                    hatalar.append(f"{url}: HTTP {r.status_code}")
                    if i < len(urls) - 1:
                        _uyar(f"{method} {url[:40]}... HTTP {r.status_code}, fallback denenir",
                              f"http_{r.status_code}:{url}", kritik=False)
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as e:
                hatalar.append(f"{url}: {type(e).__name__}")
                if i < len(urls) - 1:
                    _uyar(f"{method} {url[:40]}... {type(e).__name__}, fallback denenir",
                          f"err:{type(e).__name__}:{url}", kritik=False)
                continue
    finally:
        if close_client:
            try: http.close()
            except Exception: pass
    _uyar(f"{method} — tum {len(urls)} RPC basarisiz: {'; '.join(hatalar[:3])}",
          f"tum_fail:{method}", kritik=True)
    raise RuntimeError(f"tum RPC'ler basarisiz: {hatalar}")


def rpc_url_ilk() -> str:
    """Geri uyumluluk: eski _rpc_url() cagrilarina cevap. Sadece primary URL."""
    return _urls()[0]
