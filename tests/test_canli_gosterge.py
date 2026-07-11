"""canli_gosterge testleri: mtm hesabi, egri kaydi, fiyat/bakiye yoksa tur atlama."""
from __future__ import annotations

import json

import pytest

from hibrit_trader import canli_gosterge


@pytest.fixture(autouse=True)
def _temiz(monkeypatch, tmp_path):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(canli_gosterge, "_son", None)


def test_olc_mtm_sol_arti_canli_pozisyon(monkeypatch, tmp_path):
    (tmp_path / "v7_state.json").write_text(json.dumps({"positions": [
        {"canli_miktar": 23.8, "last_price": 1.2},
        {"amount_token": 100.0, "last_price": 2.0},  # canli_miktar yok: sayilmaz
    ]}))
    (tmp_path / "v7_trades.jsonl").write_text(
        json.dumps({"pair": "A", "signature": "imza1"}) + "\n"
        + json.dumps({"pair": "B"}) + "\n")
    monkeypatch.setattr(canli_gosterge, "_sol_bakiye", lambda c: 1.5)
    monkeypatch.setattr("hibrit_trader.jupiter.fetch_sol_price_usd",
                        lambda c, *, fallback=0.0: 80.0)
    snap = canli_gosterge.olc(None)
    assert snap["mtm"] == pytest.approx(1.5 * 80 + 23.8 * 1.2)  # 148.56
    assert snap["sol"] == 1.5 and snap["sol_fiyat"] == 80.0
    assert snap["poz_usd"] == pytest.approx(28.56)
    assert snap["acik_poz"] == 1
    assert snap["islem_n"] == 1  # sadece signature'li satirlar canli islem sayilir
    assert canli_gosterge.son() == snap
    satirlar = (tmp_path / "canli_equity.jsonl").read_text().splitlines()
    son_kayit = json.loads(satirlar[-1])
    assert son_kayit["eq"] == snap["mtm"] and son_kayit["ts"] == snap["ts"]


def test_fiyat_alinamazsa_tur_atlanir(monkeypatch, tmp_path):
    monkeypatch.setattr(canli_gosterge, "_sol_bakiye", lambda c: 1.5)
    monkeypatch.setattr("hibrit_trader.jupiter.fetch_sol_price_usd",
                        lambda c, *, fallback=0.0: 0.0)
    assert canli_gosterge.olc(None) is None
    assert canli_gosterge.son() is None
    assert not (tmp_path / "canli_equity.jsonl").exists()


def test_bakiye_alinamazsa_tur_atlanir(monkeypatch, tmp_path):
    monkeypatch.setattr(canli_gosterge, "_sol_bakiye", lambda c: None)
    assert canli_gosterge.olc(None) is None
    assert not (tmp_path / "canli_equity.jsonl").exists()


def test_baz_usd_env(monkeypatch):
    monkeypatch.delenv("CANLI_BAZ_USD", raising=False)
    assert canli_gosterge.baz_usd() == 119.59
    monkeypatch.setenv("CANLI_BAZ_USD", "150")
    assert canli_gosterge.baz_usd() == 150.0
    monkeypatch.setenv("CANLI_BAZ_USD", "bozuk")
    assert canli_gosterge.baz_usd() == 119.59
