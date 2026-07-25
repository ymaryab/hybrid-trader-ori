"""R2 sonda kapatma (25 Tem kullanici karari) + pasif golge kaydi."""

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.r2_session as r2


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE",
                        tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr(r2, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None:
                        (price, False))
    monkeypatch.setattr(r2, "SONDA_AKTIF", False)
    monkeypatch.setattr(r2, "ERKEN_GUC_AKTIF", False)
    return tmp_path


def _poz(now):
    return {"trade_id": "t1", "pair": "T / SOL", "token_address": "TOK",
            "entry_price": 1.0, "last_price": 1.0, "opened_ts": now - 30,
            "mfe_pct": 0.0, "mae_pct": 0.0}


def test_kapaliyken_sonda_kes_yok_golge_yazilir(tmp_path):
    eng = r2.R2Engine(SimpleNamespace(scan_chains=("solana",)))
    now = time.time()
    p = _poz(now)
    karar = eng._eval_position(p, 0.975, now)      # -2.5 <= kes esigi
    assert karar is None                            # SATIS YOK (grace ici)
    assert p.get("sonda_golge_ts")                  # golge isaretlendi
    satirlar = [json.loads(l) for l in
                open(tmp_path / "r2_sonda_golge.jsonl")]
    assert satirlar[0]["trade_id"] == "t1"
    assert satirlar[0]["pnl_pct"] == pytest.approx(-2.5)
    # ikinci tick tekrar yazmaz
    eng._eval_position(p, 0.97, now + 5)
    assert len(open(tmp_path / "r2_sonda_golge.jsonl").readlines()) == 1


def test_kapaliyken_teyit_gormus_kagida_golge_yok(tmp_path):
    eng = r2.R2Engine(SimpleNamespace(scan_chains=("solana",)))
    now = time.time()
    p = _poz(now)
    p["mfe_pct"] = 3.0                              # teyit seviyesi gorulmus
    eng._eval_position(p, 0.975, now)
    assert not p.get("sonda_golge_ts")
    assert not (tmp_path / "r2_sonda_golge.jsonl").exists()


def test_acikken_davranis_degismedi(monkeypatch, tmp_path):
    monkeypatch.setattr(r2, "SONDA_AKTIF", True)
    eng = r2.R2Engine(SimpleNamespace(scan_chains=("solana",)))
    now = time.time()
    p = _poz(now)
    p["sonda"] = True
    assert eng._eval_position(p, 0.975, now) == "sonda_kes"