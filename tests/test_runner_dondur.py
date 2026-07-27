"""Runner dondurma tek-anahtari: canli disi birakma, panel onbellek,
paper/gozlem davranisinin DEGISMEDIGI."""

import pytest

import hibrit_trader.edge.cekirdek as ck
import hibrit_trader.otonom_secici as osec
import hibrit_trader.runner_dondur as rd


@pytest.fixture(autouse=True)
def _temiz(monkeypatch):
    monkeypatch.delenv("RUNNER_DONDUR", raising=False)
    monkeypatch.delenv("EDGE_CANLI_AILE_YASAK", raising=False)
    rd._onbellek.clear()
    yield
    rd._onbellek.clear()


def test_anahtar_kapaliyken_hicbir_sey_degismez(monkeypatch):
    assert rd.aktif() is False
    assert rd.donduruldu("r1") is False
    assert osec._canli_yasak_aileler() == set()
    suzuk, yasak = osec._canli_skor_suz({m: {"pct": 1.0, "islem": 5}
                                         for m in ck.KATALOG["runner"]["uyeler"]})
    assert yasak is None and len(suzuk) == 2


def test_anahtar_acikken_runner_canli_disi(monkeypatch):
    monkeypatch.setenv("RUNNER_DONDUR", "1")
    assert rd.aktif() is True
    assert osec._canli_yasak_aileler() == {"runner"}
    skor = {m: {"pct": 9.0, "islem": 40} for m in ck.KATALOG["runner"]["uyeler"]}
    skor.update({m: {"pct": 1.0, "islem": 40}
                 for m in ck.KATALOG["scalp"]["uyeler"]})
    suzuk, yasak = osec._canli_skor_suz(skor)
    assert yasak == ["runner"]
    assert not set(suzuk) & set(ck.KATALOG["runner"]["uyeler"])
    assert set(ck.KATALOG["scalp"]["uyeler"]) <= set(suzuk)


def test_eski_env_ile_birlesir(monkeypatch):
    monkeypatch.setenv("RUNNER_DONDUR", "1")
    monkeypatch.setenv("EDGE_CANLI_AILE_YASAK", "scalp")
    assert osec._canli_yasak_aileler() == {"scalp", "runner"}


def test_panel_onbellek_yalniz_dondurulmus_motorda(monkeypatch):
    monkeypatch.setenv("RUNNER_DONDUR", "1")
    sayac = {"n": 0}

    def uret():
        sayac["n"] += 1
        return {"points": [[1, 2]]}

    # dondurulmus motor: ikinci cagri diskten okumaz
    for _ in range(3):
        assert rd.panel_onbellek("eq:r1:60", uret) == {"points": [[1, 2]]}
    assert sayac["n"] == 1
    # TTL dolunca yeniden uretir
    monkeypatch.setattr(rd, "PANEL_TTL_SN", -1)
    rd.panel_onbellek("eq:r1:60", uret)
    assert sayac["n"] == 2
    # dondurulmamis motor onbellege girmez (panel tarafi kontrolu)
    assert rd.donduruldu("v7new") is False
    assert rd.donduruldu("r2") is True


def test_panel_equity_dondurulmamis_motoru_atlar(monkeypatch, tmp_path):
    """Canli/scalp uclari onbelleksiz: her cagri taze hesaplanir."""
    monkeypatch.setenv("RUNNER_DONDUR", "1")
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    from hibrit_trader import panel
    cagri = {"n": 0}
    gercek = panel._equity_series_hesapla

    def sahte(prefix, minutes, equity_jsonl=True):
        cagri["n"] += 1
        return gercek(prefix, minutes, equity_jsonl)
    monkeypatch.setattr(panel, "_equity_series_hesapla", sahte)
    for _ in range(3):
        panel._equity_series("v7new", 60)
    assert cagri["n"] == 3           # onbellek YOK
    for _ in range(3):
        panel._equity_series("r1", 60)
    assert cagri["n"] == 4           # onbellek VAR (tek hesap)


def test_paper_ve_gozlem_dokunulmamis(monkeypatch):
    """Dondurma paper motor listesini veya kadansi DEGISTIRMEZ."""
    monkeypatch.setenv("RUNNER_DONDUR", "1")
    # KATALOG (aile uyelikleri) aynen durur: counterfactual icin sart
    assert set(ck.KATALOG["runner"]["uyeler"]) == {"r1", "r2"}
    # tam-evren cekirdegi runner'i hala degerlendirir (GO kaydi)
    c = ck.Cekirdek()
    skor = {m: {"pct": 9.0, "islem": 40} for m in ck.KATALOG["runner"]["uyeler"]}
    skor.update({m: {"pct": 0.1, "islem": 40}
                 for m in ck.KATALOG["scalp"]["uyeler"]})
    for _ in range(ck.TEYIT_TUR + 1):        # histerezis teyidi
        sonuc = c.karar(skor)
    assert sonuc["aile"] == "runner"
