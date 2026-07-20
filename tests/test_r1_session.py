"""R1 giris filtreleri: h1 tavani (20 Tem kanama otopsisi karari)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hibrit_trader.r1_session as r1


@pytest.fixture(autouse=True)
def r1_ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_AGGRESSIVE", "1")
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    return tmp_path


def _pair(name, h1, m5=5.0, liq=50000.0):
    return SimpleNamespace(
        name=name, chain="solana", pool_address="POOL" + name,
        token_address="TOK" + name, price_usd=0.001, liquidity_usd=liq,
        chg_h1=h1, chg_m5=m5, pool_created_at=None)


def _engine(monkeypatch):
    eng = r1.R1Engine(SimpleNamespace(scan_chains=("solana",)))
    monkeypatch.setattr(r1, "check_token",
                        lambda c, ch, t: SimpleNamespace(ok=True, kapi=None, reasons=[]))
    monkeypatch.setattr(r1.aday_paylastir, "iddia_et", lambda t, m, n: (True, None))
    monkeypatch.setattr(r1.R1Engine, "_sol_chg_h1", lambda self, c: 1.0)
    acilan = []
    monkeypatch.setattr(
        r1.R1Engine, "_open_position",
        lambda self, pair, usd, sol_h1=None, client=None: acilan.append(pair.name) or True)
    return eng, acilan


def test_h1_tavan_ustu_reddedilir_etiketli(monkeypatch):
    assert r1.H1_MAX == 150.0  # env default
    kayitlar = []
    monkeypatch.setattr(r1, "safety_reject_kaydet",
                        lambda pr, m, n, d="": kayitlar.append((pr.name, m, n)))
    eng, acilan = _engine(monkeypatch)
    monkeypatch.setattr(r1, "scan_all",
                        lambda chains: [_pair("NORMAL", h1=80.0),
                                        _pair("POMPA", h1=233.0)])
    eng._enter(None)
    assert acilan == ["NORMAL"]
    assert ("POMPA", "R1", "h1_tavan_skip") in kayitlar


def test_h1_tavan_sifir_devre_disi(monkeypatch):
    monkeypatch.setattr(r1, "H1_MAX", 0.0)
    monkeypatch.setattr(r1, "safety_reject_kaydet", lambda *a, **k: None)
    eng, acilan = _engine(monkeypatch)
    monkeypatch.setattr(r1, "scan_all", lambda chains: [_pair("POMPA", h1=233.0)])
    eng._enter(None)
    assert acilan == ["POMPA"]


def test_h1_tavan_bandin_icini_etkilemez(monkeypatch):
    # tavanin altindaki adaylar eski davranisla girer, m5 filtresi de yerinde
    monkeypatch.setattr(r1, "safety_reject_kaydet", lambda *a, **k: None)
    eng, acilan = _engine(monkeypatch)
    monkeypatch.setattr(r1, "scan_all",
                        lambda chains: [_pair("SINIRDA", h1=150.0),
                                        _pair("YORGUN", h1=80.0, m5=0.0)])
    eng._enter(None)
    assert acilan == ["SINIRDA"]


def test_m5_tavan_ustu_reddedilir_etiketli(monkeypatch):
    assert r1.M5_MAX == 75.0  # env default
    kayitlar = []
    monkeypatch.setattr(r1, "safety_reject_kaydet",
                        lambda pr, m, n, d="": kayitlar.append((pr.name, n)))
    eng, acilan = _engine(monkeypatch)
    monkeypatch.setattr(r1, "scan_all",
                        lambda chains: [_pair("NORMAL", h1=80.0, m5=20.0),
                                        _pair("PARABOL", h1=120.0, m5=200.0)])
    eng._enter(None)
    assert acilan == ["NORMAL"]
    assert ("PARABOL", "m5_tavan_skip") in kayitlar


def test_m5_tavan_sifir_devre_disi(monkeypatch):
    monkeypatch.setattr(r1, "M5_MAX", 0.0)
    monkeypatch.setattr(r1, "safety_reject_kaydet", lambda *a, **k: None)
    eng, acilan = _engine(monkeypatch)
    monkeypatch.setattr(r1, "scan_all", lambda chains: [_pair("PARABOL", h1=120.0, m5=200.0)])
    eng._enter(None)
    assert acilan == ["PARABOL"]
