"""Merkezi kota yonetimi: host basi token-bucket + oncelik sinifi tabanlari.

14 Tem 429 firtinasi dersi: rate limit HOST basinadir; tarama kotayi tuketince
sol_h1 fetch'i de 429 yiyip rejimi fail-closed kapatti (sistem acik piyasada
kor kaldi). Cozum: her host icin tek kova, her tuketici sinifi kovanin ancak
kendi TABANININ ustundeyken harcama yapabilir. Yuksek oncelik dusuk tabana
sahiptir; tarama kovayi en fazla kendi tabanina kadar bosaltabilir, satis ve
rejim her zaman pay bulur.

Oncelik sirasi (dusuk taban = yuksek oncelik):
    satis  : canli satis, HER ZAMAN gecer (kova eksiye dusebilir)
    alim   : canli alim
    rejim  : sol_h1 / rejim verisi
    feed   : taze fiyat teyidi, hizli feed
    tarama : trending, recheck, kesif taramalari

Motor kurallarina dokunmaz; yalniz HTTP tuketicileri izin sorar.
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

# Kova tabanlari: sinifin harcayabilmesi icin kovada bu ORANIN ustunde token olmali
SINIF_TABAN = {
    "satis": None,   # tabansiz: her zaman gecer, kova eksiye dusebilir
    "alim": 0.02,
    "rejim": 0.10,
    "feed": 0.25,
    "tarama": 0.50,
}

# Host kapasiteleri (istek/dakika). Env ile ayarlanabilir.
_HOST_RPM_VARSAYILAN = {
    "geckoterminal": 30.0,
    "dexscreener": 240.0,
    "jupiter": 55.0,
}


def _host_rpm(host: str) -> float:
    env = os.getenv(f"KOTA_{host.upper()}_RPM", "")
    try:
        v = float(env) if env else _HOST_RPM_VARSAYILAN[host]
    except ValueError:
        v = _HOST_RPM_VARSAYILAN[host]
    return max(v, 1.0)


class _Kova:
    def __init__(self, kapasite: float):
        self.kapasite = kapasite
        self.dolum_hizi = kapasite / 60.0  # rpm -> token/sn
        self.seviye = kapasite
        self.son_ts = time.monotonic()
        self.lock = threading.Lock()

    def _doldur(self, now: float) -> None:
        gecen = max(now - self.son_ts, 0.0)
        self.seviye = min(self.kapasite, self.seviye + gecen * self.dolum_hizi)
        self.son_ts = now

    def tuket(self, sinif: str, maliyet: float) -> bool:
        taban_oran = SINIF_TABAN[sinif]
        now = time.monotonic()
        with self.lock:
            self._doldur(now)
            if taban_oran is None:  # satis: kosulsuz, eksiye dusebilir
                self.seviye -= maliyet
                return True
            taban = taban_oran * self.kapasite
            if self.seviye - maliyet >= taban:
                self.seviye -= maliyet
                return True
            return False

    def bosalt(self) -> None:
        with self.lock:
            self._doldur(time.monotonic())
            self.seviye = min(self.seviye, 0.0)

    def durum(self) -> tuple[float, float]:
        with self.lock:
            self._doldur(time.monotonic())
            return self.seviye, self.kapasite


_kova_lock = threading.Lock()
_kovalar: dict[str, _Kova] = {}

# tarama sagligi: (host bagimsiz) son basarili trending taramasi ve backoff izi
_saglik_lock = threading.Lock()
_son_tarama_basari_ts = 0.0
_backoff_aktif_kadar = 0.0

TARAMA_KOR_SEC = float(os.getenv("KOTA_TARAMA_KOR_SEC", "300"))


def _kova(host: str) -> _Kova:
    with _kova_lock:
        k = _kovalar.get(host)
        if k is None:
            k = _Kova(_host_rpm(host))
            _kovalar[host] = k
        return k


def izin(host: str, sinif: str, maliyet: float = 1.0) -> bool:
    """Istek oncesi izin. False donerse cagiran bu turu atlamalidir."""
    if sinif not in SINIF_TABAN:
        raise ValueError(f"bilinmeyen kota sinifi: {sinif}")
    ok = _kova(host).tuket(sinif, maliyet)
    if not ok:
        log.debug("kota reddi: %s/%s", host, sinif)
    return ok


def ceza_429(host: str) -> None:
    """429 gorulunce kova bosaltilir: dusuk oncelik dolum tabanini asana kadar
    bekler, yuksek oncelik (rejim/alim/satis) daha erken pay bulur."""
    _kova(host).bosalt()


def durum(host: str) -> tuple[float, float]:
    return _kova(host).durum()


def tarama_basarisi_kaydet() -> None:
    global _son_tarama_basari_ts, _backoff_aktif_kadar
    with _saglik_lock:
        _son_tarama_basari_ts = time.monotonic()
        _backoff_aktif_kadar = 0.0


def tarama_backoff_kaydet(bitis_monotonic: float) -> None:
    global _backoff_aktif_kadar
    with _saglik_lock:
        _backoff_aktif_kadar = bitis_monotonic


def tarama_sagligi() -> str:
    """Panel rozeti: normal / kisitli / kor."""
    now = time.monotonic()
    with _saglik_lock:
        son_ok = _son_tarama_basari_ts
        backoff = _backoff_aktif_kadar
    if son_ok == 0.0:
        # boot sonrasi ilk tarama gelmediyse: backoff varsa kisitli, yoksa normal
        return "kisitli" if now < backoff else "normal"
    if now - son_ok > TARAMA_KOR_SEC:
        return "kor"
    seviye, kapasite = durum("geckoterminal")
    if now < backoff or seviye < SINIF_TABAN["tarama"] * kapasite:
        return "kisitli"
    return "normal"


def _reset() -> None:
    """Testler icin: tum kovalari ve saglik izini sifirla."""
    global _son_tarama_basari_ts, _backoff_aktif_kadar
    with _kova_lock:
        _kovalar.clear()
    with _saglik_lock:
        _son_tarama_basari_ts = 0.0
        _backoff_aktif_kadar = 0.0
