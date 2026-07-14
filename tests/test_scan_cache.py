"""Paylasimli tarama testleri: cache paylasimi + telafi + 429 backoff + motor baglari."""

from __future__ import annotations

import httpx
import pytest

import hibrit_trader.scanner as sc
from hibrit_trader.scanner import Pair


@pytest.fixture(autouse=True)
def temiz_cache(monkeypatch):
    monkeypatch.setattr(sc, "_scan_cache", {})


def _pair(pool="CP1"):
    return Pair(
        chain="solana", dex="raydium", pool_address=pool, token_address="CT1",
        name="C / SOL", price_usd=1.0, liquidity_usd=150_000.0,
        vol_m5=0.0, vol_h1=0.0, vol_h24=0.0,
        chg_m5=1.0, chg_h1=15.0, chg_h24=0.0, txns_h1=10,
    )


# ---- cache: dongu basina tek tarama -------------------------------------------------

def test_ikinci_cagri_taramayi_tekrarlamaz(monkeypatch):
    sayac = []
    monkeypatch.setattr(sc, "scan_all", lambda chains=None: sayac.append(1) or [_pair()])
    r1 = sc.scan_all_cached(("solana",))
    r2 = sc.scan_all_cached(("solana",))
    assert len(sayac) == 1
    assert [p.pool_address for p in r1] == [p.pool_address for p in r2] == ["CP1"]


def test_cache_kopya_doner_paylasilan_liste_bozulmaz(monkeypatch):
    monkeypatch.setattr(sc, "scan_all", lambda chains=None: [_pair()])
    r1 = sc.scan_all_cached(("solana",))
    r1.clear()
    assert len(sc.scan_all_cached(("solana",))) == 1


def test_ttl_gecince_yeniden_tarar(monkeypatch):
    sayac = []
    monkeypatch.setattr(sc, "scan_all", lambda chains=None: sayac.append(1) or [_pair()])
    monkeypatch.setattr(sc, "SCAN_CACHE_SEC", 0.0)
    sc.scan_all_cached(("solana",))
    sc.scan_all_cached(("solana",))
    assert len(sayac) == 2


# ---- telafi: tarama bos/hatali donerse son iyi sonuc --------------------------------

def test_bos_tarama_son_iyi_sonucla_telafi(monkeypatch):
    sonuc = [[_pair()], []]
    monkeypatch.setattr(sc, "scan_all", lambda chains=None: sonuc.pop(0))
    monkeypatch.setattr(sc, "SCAN_CACHE_SEC", 0.0)
    sc.scan_all_cached(("solana",))
    r = sc.scan_all_cached(("solana",))
    assert [p.pool_address for p in r] == ["CP1"]


def test_tarama_exception_da_telafi(monkeypatch):
    ilk = [True]

    def _tarama(chains=None):
        if ilk.pop() if ilk else False:
            return [_pair()]
        raise RuntimeError("network yok")

    monkeypatch.setattr(sc, "scan_all", _tarama)
    monkeypatch.setattr(sc, "SCAN_CACHE_SEC", 0.0)
    sc.scan_all_cached(("solana",))
    r = sc.scan_all_cached(("solana",))
    assert [p.pool_address for p in r] == ["CP1"]


def test_stale_siniri_gecmis_sonuc_kullanilmaz(monkeypatch):
    sonuc = [[_pair()], []]
    monkeypatch.setattr(sc, "scan_all", lambda chains=None: sonuc.pop(0))
    monkeypatch.setattr(sc, "SCAN_CACHE_SEC", 0.0)
    monkeypatch.setattr(sc, "SCAN_STALE_MAX_SEC", 0.0)
    sc.scan_all_cached(("solana",))
    assert sc.scan_all_cached(("solana",)) == []


# ---- 429 ustel backoff: aninda retry yok, pencere boyu istek yok --------------------

class _Resp:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"http {self.status_code}")

    def json(self):
        return {"data": []}


class _Client:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        return _Resp(self.statuses.pop(0))


def test_429_aninda_retry_yok(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda s: pytest.fail("sleep olmamali"))
    cl = _Client([429])
    assert sc.fetch_trending(cl, "solana") == []
    assert cl.calls == 1  # tek istek, retry yok
    assert sc._backoff_sec == sc.BACKOFF_429_BASLANGIC_SEC


def test_429_backoff_penceresinde_istek_atilmaz():
    cl = _Client([429, 200])
    sc.fetch_trending(cl, "solana")
    assert sc.fetch_trending(cl, "solana") == []
    assert cl.calls == 1  # pencere icinde HTTP yok


def test_429_backoff_ustel_buyur_tavana_dayanir(monkeypatch):
    # kota kapisini izole et: ceza_429 kovayi bosaltinca izin reddi backoff
    # buyumesini golgeleyecegi icin burada saf backoff mantigi sinanir
    from hibrit_trader import kota

    monkeypatch.setattr(kota, "izin", lambda host, sinif, maliyet=1.0: True)
    cl = _Client([429] * 8)
    gorulen = []
    for _ in range(8):
        monkeypatch.setattr(sc, "_backoff_bitis", 0.0)  # pencereyi zorla ac
        sc.fetch_trending(cl, "solana")
        gorulen.append(sc._backoff_sec)
    assert gorulen == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 60.0]


def test_basarili_tarama_backoffu_sifirlar(monkeypatch):
    from hibrit_trader import kota

    monkeypatch.setattr(kota, "izin", lambda host, sinif, maliyet=1.0: True)
    cl = _Client([429, 200])
    sc.fetch_trending(cl, "solana")
    monkeypatch.setattr(sc, "_backoff_bitis", 0.0)
    sc.fetch_trending(cl, "solana")
    assert cl.calls == 2
    assert sc._backoff_sec == 0.0


def test_429_yoksa_tek_istek(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda s: pytest.fail("backoff olmamali"))
    cl = _Client([200])
    sc.fetch_trending(cl, "solana")
    assert cl.calls == 1


def test_kota_reddi_istek_atmaz(monkeypatch):
    from hibrit_trader import kota

    monkeypatch.setattr(kota, "izin", lambda host, sinif, maliyet=1.0: False)
    cl = _Client([200])
    assert sc.fetch_trending(cl, "solana") == []
    assert cl.calls == 0


# ---- motor baglari: aktif filo paylasimli taramada ----------------------------------

def test_aktif_motorlar_paylasimli_taramayi_kullanir():
    import hibrit_trader.kosucu_ekg as ekg
    import hibrit_trader.v6_session as v6
    import hibrit_trader.v7_session as v7
    import hibrit_trader.x1_session as x1

    for mod in (v6, v7, x1, ekg):
        assert mod.scan_all is sc.scan_all_cached, mod.__name__
