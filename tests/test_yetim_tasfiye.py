"""Yetim tasfiye boot mutabakati (HAT 3 kalem 1, 25 Tem P0).

24 Tem vakasi: zorla restart uctaki tasfiyeyi oksuz birakti, zamanlanmis
yanlis zorla-satis elle durduruldu. Fix iki katman:
1) pid sahipligi: yetim dosya zorla satis ATESLEMEZ (giris blogu surer)
2) secici boot mutabakati: yetim dosya silinir, olay + bildirim yazilir
"""

import json
import os
import time

import pytest

import hibrit_trader.canli_session as cs
import hibrit_trader.otonom_secici as osec


@pytest.fixture
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOZLEM_DATA_DIR", str(tmp_path / "gozlem"))
    monkeypatch.setattr(osec, "_yazici", None)
    monkeypatch.setattr(osec, "notify", lambda *a, **k: None, raising=False)
    return tmp_path


def _tasfiye_yaz(tmp_path, pid, zorla_ts, **ek):
    (tmp_path / cs.TASFIYE_FILE).write_text(json.dumps({
        "switch_id": "sw_test", "from": "r1", "to": "v7",
        "zorla_ts": zorla_ts, "pid": pid, **ek}))


def test_yetim_dosya_zorla_ateslemez(ortam):
    _tasfiye_yaz(ortam, pid=999999, zorla_ts=time.time() - 60)
    assert cs.tasfiye_talebi_var()            # giris blogu SURER
    assert not cs.tasfiye_zorla_aktif()       # ama zorla satis YOK


def test_sahip_surec_zorla_calisir(ortam):
    _tasfiye_yaz(ortam, pid=os.getpid(), zorla_ts=time.time() - 1)
    assert cs.tasfiye_zorla_aktif()
    _tasfiye_yaz(ortam, pid=os.getpid(), zorla_ts=time.time() + 600)
    assert not cs.tasfiye_zorla_aktif()       # dogal faz surer


def test_bozuk_dosya_yetim_sayilir(ortam):
    (ortam / cs.TASFIYE_FILE).write_text("{bozuk json")
    assert cs.tasfiye_talebi_var()
    assert not cs.tasfiye_zorla_aktif()       # eski davranis True idi


def test_boot_mutabakati_yetimi_temizler(ortam):
    _tasfiye_yaz(ortam, pid=999999, zorla_ts=time.time() - 60)
    bildirimler = []
    import hibrit_trader.killswitch as ks
    orijinal = ks.notify
    ks.notify = lambda msg, *a, **k: bildirimler.append(msg)
    try:
        p = osec.yetim_tasfiye_mutabakati()
    finally:
        ks.notify = orijinal
    assert p["switch_id"] == "sw_test"
    assert p["zorla_gecmis_miydi"] is True
    assert not (ortam / cs.TASFIYE_FILE).exists()
    assert any("yetim tasfiye" in m for m in bildirimler)
    assert osec.yetim_tasfiye_mutabakati() is None      # ikinci cagri bos


def test_boot_mutabakati_bozuk_dosyayi_da_siler(ortam):
    (ortam / cs.TASFIYE_FILE).write_text("{bozuk json")
    import hibrit_trader.killswitch as ks
    orijinal = ks.notify
    ks.notify = lambda *a, **k: None
    try:
        p = osec.yetim_tasfiye_mutabakati()
    finally:
        ks.notify = orijinal
    assert p is not None and p["switch_id"] is None
    assert not (ortam / cs.TASFIYE_FILE).exists()


def test_yeni_yazim_pid_ve_bas_ts_icerir():
    """Secici tasfiye dosyasini pid + bas_ts ile yazmali (kaynak kodu
    sozlesmesi: regresyon bekcisi)."""
    import inspect
    kaynak = inspect.getsource(osec.kontrol_dongusu)
    assert '"pid": os.getpid()' in kaynak
    assert '"bas_ts": bas' in kaynak
