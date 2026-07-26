"""Rug-imza kara listesi: esik, kalicilik, kanca ve aday filtresi."""

import json
from dataclasses import dataclass

import pytest

from hibrit_trader import token_kara_liste as kl


@pytest.fixture(autouse=True)
def _izole_dosya(tmp_path, monkeypatch):
    monkeypatch.setattr(kl, "DOSYA", tmp_path / "token_kara_liste.json")
    kl._cache.update(mtime=None, tokenler={}, yol=None)
    yield


def test_esik_ve_kalicilik():
    # -24 esigin ustunde: eklenmez; -25 ve alti: eklenir
    assert kl.islem_kontrol("TOKA", -24.0, pair="A / SOL") is False
    assert kl.yasakli("TOKA") is False
    assert kl.islem_kontrol("TOKB", -25.0, pair="B / SOL") is True
    assert kl.islem_kontrol("TOKC", -99.3, pair="C / SOL") is True
    assert kl.yasakli("TOKB") and kl.yasakli("TOKC")
    # idempotent: ikinci kapanis yeniden eklemez
    assert kl.islem_kontrol("TOKB", -80.0) is False
    # disk kaydi kalici ve okunabilir
    veri = json.loads(kl.DOSYA.read_text())
    assert veri["sv"] == 1 and set(veri["tokenler"]) == {"TOKB", "TOKC"}
    assert veri["tokenler"]["TOKC"]["pnl_pct"] == -99.3
    # bos/None token kapanisi sessiz gecer
    assert kl.islem_kontrol(None, -50.0) is False
    assert kl.islem_kontrol("", -50.0) is False


def test_sicak_yenileme_mtime():
    kl.ekle("TOKD", pnl_pct=-30, kaynak="test")
    assert kl.yasakli("TOKD")
    # dosya disaridan degisirse (tohum scripti) cache tazelenir
    kl.DOSYA.write_text(json.dumps({"sv": 1, "tokenler": {"TOKE": {}}}))
    import os
    os.utime(kl.DOSYA, (0, 0))          # mtime kesin degissin
    assert kl.yasakli("TOKE") and not kl.yasakli("TOKD")


def test_aday_filtresi():
    @dataclass
    class P:
        token_address: str
        name: str = ""

    kl.ekle("RUG1", pnl_pct=-99, kaynak="test")
    adaylar = [P("RUG1"), P("TEMIZ1"), P("TEMIZ2")]
    kalan = kl.filtrele(adaylar)
    assert [p.token_address for p in kalan] == ["TEMIZ1", "TEMIZ2"]
    # bos listede dokunmadan doner
    kl._cache.update(mtime=None, tokenler={}, yol=None)
    kl.DOSYA.unlink()
    assert kl.filtrele(adaylar) is adaylar


def test_enrich_kapanis_kancasi(monkeypatch):
    from hibrit_trader import paper

    cagri = {}
    monkeypatch.setattr(kl, "islem_kontrol",
                        lambda tok, pct, **kw: cagri.update(tok=tok, pct=pct))
    pos = paper.Position(pair_name="R / SOL", chain="solana",
                         token_address="RUGTOK", pool_address="pool",
                         entry_price=1.0, amount_token=10.0, cost_usd=40.0,
                         opened_at="", entry_score=0.0, opened_ts=0.0)
    tr = paper.Trade(pair_name="R / SOL", chain="solana", entry_price=1.0,
                     exit_price=0.01, cost_usd=40.0, proceeds_usd=0.4,
                     pnl_usd=-39.6, opened_at="", closed_at="",
                     exit_reason="stop_felaket")
    paper.enrich_trade_from_position(tr, pos)
    assert cagri["tok"] == "RUGTOK" and cagri["pct"] == pytest.approx(-99.0)
