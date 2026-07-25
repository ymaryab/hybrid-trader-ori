"""R1/R2 SOL rejim kapisi fail-closed (HAT 3 kalem 2, 25 Tem P0).

Eski davranis fail-open idi: SOL feed kesintisinde (sol_h1 None) negatif
rejimde bile alim yapiliyordu. Yeni: kapi ACIKKEN (SOL_GIRIS_MIN > -999)
veri yoksa giris yok, adaylar "rejim_veri_yok" ile rejects'e duser.
Kapi kapaliyken (<= -999) veri yoklugu giris engellemez.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.r1_session as r1
import hibrit_trader.r2_session as r2


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_AGGRESSIVE", "1")
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    return tmp_path


def _aday(mod):
    return SimpleNamespace(
        name="TEST / SOL", chain="solana", pool_address="POOL1",
        token_address="TOK1", price_usd=1.0,
        liquidity_usd=mod.LIQ_MIN_USD + 1000,
        chg_h1=(mod.CHG_H1_MIN + 5), chg_m5=5.0, pool_created_at=None)


def _kur(monkeypatch, mod, motor_adi, sol_h1):
    """Motoru tek gecerli adayla, sahte feed'le hazirla."""
    kayitlar = []
    monkeypatch.setattr(mod, "scan_all", lambda chains: [_aday(mod)])
    monkeypatch.setattr(mod, "rejim_reject_kaydet",
                        lambda cands, m, s: kayitlar.append(("rejim", m, s)))
    monkeypatch.setattr(mod, "check_token",
                        lambda c, ch, t: SimpleNamespace(
                            ok=False, kapi="safety_red", reasons=["stub"]))
    monkeypatch.setattr(mod, "safety_reject_kaydet",
                        lambda *a, **k: kayitlar.append(("safety", a)))
    monkeypatch.setattr(mod.aday_paylastir, "iddia_et",
                        lambda *a, **k: (True, None))
    eng = mod.R1Engine(SimpleNamespace(scan_chains=("solana",))) \
        if motor_adi == "R1" else \
        mod.R2Engine(SimpleNamespace(scan_chains=("solana",)))
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    monkeypatch.setattr(eng, "_entries_blocked", lambda: False,
                        raising=False)
    return eng, kayitlar


@pytest.mark.parametrize("mod,motor", [(r1, "R1"), (r2, "R2")])
def test_veri_yok_giris_yok(monkeypatch, mod, motor):
    eng, kayitlar = _kur(monkeypatch, mod, motor, sol_h1=None)
    monkeypatch.setattr(mod, "SOL_GIRIS_MIN", 0.0)
    eng._enter(client=None)
    assert eng.positions == []
    assert ("rejim", motor, None) in kayitlar          # rejim_veri_yok yolu
    assert not any(k[0] == "safety" for k in kayitlar)  # kapiyi GECMEDI


@pytest.mark.parametrize("mod,motor", [(r1, "R1"), (r2, "R2")])
def test_veri_varsa_kapi_normal(monkeypatch, mod, motor):
    eng, kayitlar = _kur(monkeypatch, mod, motor, sol_h1=1.0)
    monkeypatch.setattr(mod, "SOL_GIRIS_MIN", 0.0)
    eng._enter(client=None)
    assert eng.positions == []                          # safety_red durdurdu
    assert not any(k[0] == "rejim" for k in kayitlar)
    # kapiyi gecip guvenlik kontrolune ulasti (fail-closed asiri genis degil)


@pytest.mark.parametrize("mod,motor", [(r1, "R1"), (r2, "R2")])
def test_negatif_rejim_hala_kapali(monkeypatch, mod, motor):
    eng, kayitlar = _kur(monkeypatch, mod, motor, sol_h1=-2.0)
    monkeypatch.setattr(mod, "SOL_GIRIS_MIN", 0.0)
    eng._enter(client=None)
    assert ("rejim", motor, -2.0) in kayitlar           # eski davranis korunur


@pytest.mark.parametrize("mod,motor", [(r1, "R1"), (r2, "R2")])
def test_kapi_devre_disiyken_veri_yoklugu_engel_degil(monkeypatch, mod, motor):
    eng, kayitlar = _kur(monkeypatch, mod, motor, sol_h1=None)
    monkeypatch.setattr(mod, "SOL_GIRIS_MIN", -9999.0)
    eng._enter(client=None)
    assert not any(k[0] == "rejim" for k in kayitlar)   # kapi yok, blok yok


def test_rejim_veri_yok_etiketi():
    """entry_fresh: sol_h1 None -> reject nedeni 'rejim_veri_yok'."""
    import inspect

    from hibrit_trader import entry_fresh
    kaynak = inspect.getsource(entry_fresh.rejim_reject_kaydet)
    assert '"rejim_veri_yok" if sol_h1 is None' in kaynak
