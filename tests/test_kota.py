"""Merkezi kota testleri: sinif tabanlari, satis kosulsuzlugu, 429 cezasi, saglik."""

from __future__ import annotations

import pytest

from hibrit_trader import kota


@pytest.fixture(autouse=True)
def temiz_kota():
    kota._reset()
    yield
    kota._reset()


# ---- taban oncelikleri ----------------------------------------------------------


def test_dolu_kovada_tum_siniflar_gecer():
    for sinif in ("satis", "alim", "rejim", "feed", "tarama"):
        assert kota.izin("geckoterminal", sinif)


def test_tarama_kendi_tabaninin_altina_inemez():
    # kapasite 30, tarama tabani %50 = 15: en fazla 15 istek gecer
    gecen = sum(1 for _ in range(30) if kota.izin("geckoterminal", "tarama"))
    assert gecen == 15
    # tarama ac kalirken rejim hala pay bulur (taban %10 = 3)
    assert kota.izin("geckoterminal", "rejim")


def test_oncelik_sirasi_tabanlarla_uygulanir():
    # kovayi tarama ile tabana indir, sonra siniflari sirayla dene
    while kota.izin("geckoterminal", "tarama"):
        pass
    assert not kota.izin("geckoterminal", "tarama")
    # feed tabani %25 = 7.5: 15 - 7.5 = 7 istek daha
    gecen_feed = sum(1 for _ in range(20) if kota.izin("geckoterminal", "feed"))
    assert gecen_feed == 7
    # rejim tabani %10 = 3: 8 - 3 = 5 istek daha
    gecen_rejim = sum(1 for _ in range(20) if kota.izin("geckoterminal", "rejim"))
    assert gecen_rejim == 5
    # alim tabani %2 = 0.6
    gecen_alim = sum(1 for _ in range(20) if kota.izin("geckoterminal", "alim"))
    assert gecen_alim >= 2
    # satis her zaman gecer
    assert kota.izin("geckoterminal", "satis")


def test_satis_kosulsuz_kova_eksiye_dusebilir():
    kota.ceza_429("jupiter")
    seviye, _ = kota.durum("jupiter")
    assert seviye <= 0.01  # bosaltildi (olcum arasi dolum epsilonu toleransli)
    assert kota.izin("jupiter", "satis", maliyet=2.0)
    seviye, _ = kota.durum("jupiter")
    assert seviye < 0.0
    assert not kota.izin("jupiter", "alim", maliyet=2.0)


def test_bilinmeyen_sinif_hata():
    with pytest.raises(ValueError):
        kota.izin("geckoterminal", "yok_boyle_sinif")


# ---- 429 cezasi + dolum ---------------------------------------------------------


def test_ceza_429_kovayi_bosaltir():
    assert kota.izin("geckoterminal", "tarama")
    kota.ceza_429("geckoterminal")
    assert not kota.izin("geckoterminal", "tarama")
    assert not kota.izin("geckoterminal", "rejim")
    assert kota.izin("geckoterminal", "satis")


def test_dolum_zamanla_geri_gelir():
    kota.ceza_429("geckoterminal")
    kova = kota._kova("geckoterminal")
    # 40 sn ileri sar: 30rpm -> 0.5 token/sn -> 20 token, rejim tabani 3'u asar
    kova.son_ts -= 40.0
    assert kota.izin("geckoterminal", "rejim")
    assert kota.izin("geckoterminal", "feed")
    # 20 - 2 = 18 > tarama tabani 15: tarama da doner
    assert kota.izin("geckoterminal", "tarama")


def test_hostlar_bagimsiz():
    kota.ceza_429("geckoterminal")
    assert not kota.izin("geckoterminal", "tarama")
    assert kota.izin("dexscreener", "tarama")
    assert kota.izin("jupiter", "alim")


def test_env_rpm_override(monkeypatch):
    monkeypatch.setenv("KOTA_GECKOTERMINAL_RPM", "10")
    kota._reset()
    _, kapasite = kota.durum("geckoterminal")
    assert kapasite == 10.0


# ---- tarama sagligi rozeti ------------------------------------------------------


def test_saglik_boot_normal():
    assert kota.tarama_sagligi() == "normal"


def test_saglik_basari_sonrasi_normal():
    kota.tarama_basarisi_kaydet()
    assert kota.tarama_sagligi() == "normal"


def test_saglik_backoff_kisitli():
    import time

    kota.tarama_basarisi_kaydet()
    kota.tarama_backoff_kaydet(time.monotonic() + 30.0)
    assert kota.tarama_sagligi() == "kisitli"


def test_saglik_kova_dusukse_kisitli():
    kota.tarama_basarisi_kaydet()
    kota.ceza_429("geckoterminal")
    assert kota.tarama_sagligi() == "kisitli"


def test_saglik_uzun_sessizlik_kor(monkeypatch):
    kota.tarama_basarisi_kaydet()
    monkeypatch.setattr(kota, "_son_tarama_basari_ts", 1.0)
    assert kota.tarama_sagligi() == "kor"
