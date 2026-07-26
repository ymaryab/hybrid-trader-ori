"""Edge karar cekirdegi v2 (CRITICAL 1-2-5) + salter tek-yazar testleri."""

import pytest

import hibrit_trader.edge.cekirdek as ck
import hibrit_trader.otonom_secici as osec


def _skor(pct, islem=20):
    return {"pct": pct, "islem": islem}


def _skorlar(scalp=0.0, runner=0.0, islem=20):
    s = {m: _skor(scalp, islem) for m in ck.KATALOG["scalp"]["uyeler"]}
    s.update({m: _skor(runner, islem)
              for m in ck.KATALOG["runner"]["uyeler"]})
    return s


@pytest.fixture(autouse=True)
def _sabitler(monkeypatch):
    monkeypatch.delenv("EDGE_ARIZA_SIM", raising=False)
    monkeypatch.setattr(ck, "AILE_MARJ", 0.75)
    monkeypatch.setattr(ck, "TEYIT_TUR", 2)
    monkeypatch.setattr(ck, "COOLDOWN_TUR", 4)
    monkeypatch.setattr(ck, "LCB_K", 1.0)


def test_hepsi_negatifse_cash():
    c = ck.Cekirdek()
    k = c.karar(_skorlar(scalp=-2.0, runner=-1.0))
    assert k["aile"] == "cash" and k["dagilim"] == {}
    assert k["katman"] == "cekirdek"
    assert c.temsilci(_skorlar()) is None


def test_teyit_ve_gecis():
    c = ck.Cekirdek()
    s = _skorlar(scalp=0.2, runner=3.0, islem=40)
    k1 = c.karar(s)
    assert k1["aile"] == "cash"                 # 1. tur: teyit bekler
    assert k1["bekleyen_aday"] == {"runner": 1}
    k2 = c.karar(s)
    assert k2["aile"] == "runner"               # 2. tur: teyitli gecis
    assert c.temsilci(s) in ck.KATALOG["runner"]["uyeler"]


def test_cooldown_flip_flop_engeli():
    c = ck.Cekirdek()
    r = _skorlar(scalp=0.2, runner=3.0, islem=40)
    s = _skorlar(scalp=3.0, runner=0.2, islem=40)
    c.karar(r); c.karar(r)                      # runner'a gecti (tur 2)
    c.karar(s); k = c.karar(s)                  # teyit tamam AMA cooldown
    assert k["aile"] == "runner"                # tur 4 < 2+4: gecemez
    c.karar(s); k2 = c.karar(s)                 # tur 6: cooldown doldu
    assert k2["aile"] == "scalp"


def test_marj_icinde_kalir():
    c = ck.Cekirdek()
    c.karar(_skorlar(scalp=2.0, runner=0.0, islem=40))
    c.karar(_skorlar(scalp=2.0, runner=0.0, islem=40))
    assert c.karar_aile == "scalp"
    # runner marj icinde onde: kalinir, aday sayaci bile acilmaz
    k = c.karar(_skorlar(scalp=2.0, runner=2.5, islem=40))
    assert k["aile"] == "scalp" and k["bekleyen_aday"] == {}


def test_girdi_yok_fallback():
    c = ck.Cekirdek()
    k = c.karar({})
    assert k["katman"] == "girdi_yok"
    assert k["aile"] == "cash"                  # onceki karar yoksa CASH


def test_ariza_sim_tatbikat(monkeypatch):
    monkeypatch.setenv("EDGE_ARIZA_SIM", "1")
    c = ck.Cekirdek()
    with pytest.raises(RuntimeError):
        c.karar(_skorlar())


def test_lcb_az_islemi_cezalandirir():
    cok = ck.Cekirdek.aile_skoru([_skor(2.0, 100), _skor(2.2, 100)])
    az = ck.Cekirdek.aile_skoru([_skor(2.0, 1), _skor(2.2, 1)])
    assert cok["lcb"] > az["lcb"]


def test_salter_tek_yazar(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    p = tmp_path / "CANLI_DUR"
    # sistem koyar, sistem kaldirir
    osec._salter_indir("test")
    assert "otonom:" in p.read_text()
    osec._salter_kaldir()
    assert not p.exists()
    # kullanici (panel) koyarsa sistem NE EZER NE KALDIRIR
    p.write_text("2026-07-26T10:00:00+00:00 panel\n")
    osec._salter_indir("sistem-denemesi")
    assert "panel" in p.read_text()             # ezilmedi
    osec._salter_kaldir()
    assert p.exists()                           # kaldirilmadi
