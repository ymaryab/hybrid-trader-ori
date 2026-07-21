"""V7HT A/B klonu: v7hizli kurallari + tavanlar + cuval tasfiyesi."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v7ht_session as v7ht


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_AGGRESSIVE", "1")
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    return tmp_path


def test_v7hizli_ile_ayni_cekirdek():
    import hibrit_trader.v7hizli_session as v7h
    assert v7ht.TP_PCT == v7h.TP_PCT
    assert v7ht.CHG_H1_MIN == v7h.CHG_H1_MIN
    assert v7ht.CHG_H1_MAX == v7h.CHG_H1_MAX
    assert v7ht.LIQ_MIN_USD == v7h.LIQ_MIN_USD
    assert v7ht.STATE_FILE == "v7ht_state.json"  # defter izolasyonu


def test_eval_tp2_ve_cuval_tasfiyesi(monkeypatch):
    monkeypatch.setattr(v7ht, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None: (price, False))
    eng = v7ht.V7HTEngine(SimpleNamespace(scan_chains=("solana",)))
    now = time.time()

    def poz(yas_dk=0.0):
        return {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
                "opened_ts": now - yas_dk * 60, "mfe_pct": 0.0, "mae_pct": 0.0}

    assert eng._eval_position(poz(), 1.03, now) == "tp_2"
    assert eng._eval_position(poz(yas_dk=29), 0.7, now) is None  # stop yok
    assert eng._eval_position(poz(yas_dk=31), 0.9, now) == "timeout_cuval"
    assert eng._eval_position(poz(yas_dk=28), 0.9, now) is None


def test_tavanlar_girisi_keser(monkeypatch):
    kayitlar = []
    monkeypatch.setattr(v7ht, "safety_reject_kaydet",
                        lambda pr, m, n, d="": kayitlar.append((pr.name, n)))
    eng = v7ht.V7HTEngine(SimpleNamespace(scan_chains=("solana",)))
    monkeypatch.setattr(v7ht, "check_token",
                        lambda c, ch, t: SimpleNamespace(ok=True, kapi=None, reasons=[]))
    monkeypatch.setattr(v7ht.aday_paylastir, "iddia_et", lambda t, m, n: (True, None))
    monkeypatch.setattr(v7ht.V7HTEngine, "_sol_chg_h1", lambda self, c: 1.0)
    acilan = []
    monkeypatch.setattr(
        v7ht.V7HTEngine, "_open_position",
        lambda self, pair, usd, sol_h1=None, client=None: acilan.append(pair.name) or True)

    def pr(name, h1, m5):
        return SimpleNamespace(name=name, chain="solana", pool_address="P"+name,
                               token_address="T"+name, price_usd=0.001,
                               liquidity_usd=200000.0, chg_h1=h1, chg_m5=m5,
                               pool_created_at=None)
    # v7hizli bandi 5..45: parabolik m5 tavani bandin icinde de yakalanmali
    monkeypatch.setattr(v7ht, "scan_all",
                        lambda chains: [pr("NORMAL", 20.0, 10.0),
                                        pr("PARABOL", 30.0, 120.0)])
    eng._enter(None)
    assert acilan == ["NORMAL"]
    assert ("PARABOL", "m5_tavan_skip") in kayitlar
