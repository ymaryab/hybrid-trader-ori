"""EDGE CANLI SURUCU (26 Tem P0): esleme, salter oncelik, governor,
rollback. Gecis borusu _gecis_uygula mock'lanir (tasfiye/swap gercek
kosulmaz)."""

import json
import time

import pytest

import hibrit_trader.edge.cekirdek as ck
import hibrit_trader.otonom_secici as osec


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOZLEM_DATA_DIR", str(tmp_path / "gozlem"))
    monkeypatch.setattr(osec, "_yazici", None)
    monkeypatch.setattr(osec, "EDGE_CANLI", True)
    monkeypatch.setattr(osec, "_CEKIRDEK", None)
    monkeypatch.setattr(osec, "firsat_var", lambda m, dk=None: (True, 1.0))
    monkeypatch.setattr(ck, "TEYIT_TUR", 1)      # testte tek tur teyit
    monkeypatch.setattr(ck, "COOLDOWN_TUR", 0)
    return tmp_path


def _skorlar(scalp=0.0, runner=0.0, islem=40):
    s = {m: {"pct": scalp, "islem": islem}
         for m in ck.KATALOG["scalp"]["uyeler"]}
    s.update({m: {"pct": runner, "islem": islem}
              for m in ck.KATALOG["runner"]["uyeler"]})
    return s


def _d():
    return {"pencere_dk": 30, "son_gecis_ts": 0.0,
            "user_enabled": True, "system_enabled": True}


def test_edge_canli_aktif_ve_rollback(tmp_path):
    assert osec.edge_canli_aktif() is True
    (tmp_path / osec.EDGE_GERI_AL_DOSYA).write_text("1")
    assert osec.edge_canli_aktif() is False      # tek-komut rollback


def test_cash_karari_salter_indirir_ve_kaldirir(tmp_path, monkeypatch):
    bildirim = []
    monkeypatch.setattr(osec, "_gecis_uygula",
                        lambda *a, **k: pytest.fail("gecis olmamali"))
    r = osec._edge_canli_turu(_skorlar(-2.0, -1.0), "v7new", _d(),
                              bildirim.append)
    assert r == "devam"
    icerik = (tmp_path / "CANLI_DUR").read_text()
    assert icerik.startswith("edge:")
    assert any("CASH" in m for m in bildirim)
    # toparlanma: scalp guclu -> salter kalkar (hedef mevcutsa kal)
    osec._edge_canli_turu(_skorlar(3.0, 0.0), "v7", _d(),
                          bildirim.append)
    assert not (tmp_path / "CANLI_DUR").exists()


def test_cash_kullanici_salterini_ezmez(tmp_path, monkeypatch):
    (tmp_path / "CANLI_DUR").write_text("2026 panel\n")
    monkeypatch.setattr(osec, "_gecis_uygula", lambda *a, **k: "iptal")
    osec._edge_canli_turu(_skorlar(-2.0, -2.0), "v7new", _d(),
                          lambda m: None)
    assert "panel" in (tmp_path / "CANLI_DUR").read_text()  # ezilmedi
    # kal karari da kullanici salterini KALDIRMAZ
    osec._edge_canli_turu(_skorlar(3.0, 0.0), "v7", _d(), lambda m: None)
    assert (tmp_path / "CANLI_DUR").exists()


def test_gecis_karari_boruya_gider(monkeypatch):
    cagri = {}

    def sahte_gecis(mevcut, aday, *a, **k):
        cagri["cift"] = (mevcut, aday)
        return "tamam"
    monkeypatch.setattr(osec, "_gecis_uygula", sahte_gecis)
    r = osec._edge_canli_turu(_skorlar(0.2, 3.0), "v7new", _d(),
                              lambda m: None)
    assert r == "restart"
    assert cagri["cift"][0] == "v7new"
    assert cagri["cift"][1] in ck.KATALOG["runner"]["uyeler"]


def test_governor_kayip_freni(tmp_path, monkeypatch):
    monkeypatch.setattr(osec, "_canli_gun_pnl", lambda: -55.0)
    monkeypatch.setattr(osec, "_gecis_uygula",
                        lambda *a, **k: pytest.fail("gecis olmamali"))
    bildirim = []
    r = osec._edge_canli_turu(_skorlar(0.2, 3.0), "v7new", _d(),
                              bildirim.append)
    assert r == "devam"
    assert "governor:" in (tmp_path / "CANLI_DUR").read_text()
    assert any("GOVERNOR" in m for m in bildirim)
    # edge, governor salterini KALDIRAMAZ
    assert osec._edge_salter_kaldir() is False
    assert (tmp_path / "CANLI_DUR").exists()


def test_governor_gecis_tavani(tmp_path, monkeypatch):
    monkeypatch.setattr(osec, "_canli_gun_pnl", lambda: 0.0)
    osec._gov_sayac_yaz({"gun": time.strftime("%Y-%m-%d", time.gmtime()),
                         "gecis_n": osec.GOV_GUNLUK_GECIS_MAX,
                         "kayip_bildirildi": False})
    monkeypatch.setattr(osec, "_gecis_uygula",
                        lambda *a, **k: pytest.fail("gecis olmamali"))
    r = osec._edge_canli_turu(_skorlar(0.2, 3.0), "v7new", _d(),
                              lambda m: None)
    assert r == "devam"                          # tavan: gecis yok, kal


def test_gecis_sayaci_persist(tmp_path):
    osec._gov_gecis_kaydet()
    osec._gov_gecis_kaydet()
    s = json.loads((tmp_path / "gov_sayac.json").read_text())
    assert s["gecis_n"] == 2
