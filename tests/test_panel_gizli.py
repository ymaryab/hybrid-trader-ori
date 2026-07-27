"""Panel motor gizleme bayragi: YALNIZ gorunum, backend dokunulmaz."""

import pytest

from hibrit_trader import panel


@pytest.fixture(autouse=True)
def _temiz(monkeypatch):
    monkeypatch.delenv("PANEL_GIZLI_MOTORLAR", raising=False)
    yield


def test_bayrak_bosken_hicbir_sey_degismez():
    assert panel._panel_gizli() == set()
    gorunur = [m for m in panel._FILO_MOTORLAR if not m.get("gizli")]
    idler = {m["id"] for m in gorunur}
    assert {"r1", "r2"} <= idler          # varsayilan: gorunur


def test_bayrak_ayristirma(monkeypatch):
    monkeypatch.setenv("PANEL_GIZLI_MOTORLAR", " r1 , R2 ,")
    assert panel._panel_gizli() == {"r1", "r2"}


def test_gizlenen_motor_karta_girmez(monkeypatch):
    monkeypatch.setenv("PANEL_GIZLI_MOTORLAR", "r1,r2")
    gizli = panel._panel_gizli()
    gorunur = [m for m in panel._FILO_MOTORLAR
               if not m.get("gizli") and m["id"].lower() not in gizli]
    idler = {m["id"] for m in gorunur}
    assert "r1" not in idler and "r2" not in idler
    # geri kalan filo aynen durur (canli dahil)
    assert {"v7", "v7c", "v7d", "v7hizli", "v7new", "v7ht", "yz", "yzn1",
            "v7t", "canlim"} <= idler


def test_backend_uclari_dokunulmaz(monkeypatch, tmp_path):
    """Gizli motorun /api ucu ve defter okumasi calismaya devam eder."""
    monkeypatch.setenv("PANEL_GIZLI_MOTORLAR", "r1,r2")
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    d = panel._equity_series("r1", 0)          # uc hala cevap veriyor
    assert "points" in d and "start_balance" in d
    # motor listesi kaynak konfigde AYNEN durur (silinmedi, gizlendi)
    assert any(m["id"] == "r1" for m in panel._FILO_MOTORLAR)
    assert any(m["id"] == "r2" for m in panel._FILO_MOTORLAR)
