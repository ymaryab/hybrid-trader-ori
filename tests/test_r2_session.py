"""R2 RUNNER motoru: giris filtreleri + tutunma/cikis mekanigi."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import hibrit_trader.r2_session as r2


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_AGGRESSIVE", "1")
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr(r2, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None: (price, False))
    return tmp_path


def _eng():
    return r2.R2Engine(SimpleNamespace(scan_chains=("solana",)))


def _poz(now, mfe=0.0, yas_sec=0.0, kilit=False):
    p = {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
         "opened_ts": now - yas_sec, "mfe_pct": mfe, "mae_pct": 0.0}
    if kilit:
        p["kilit_alindi"] = True
    return p


def test_felaket_ve_breakeven_kilidi():
    eng = _eng(); now = time.time()
    assert eng._eval_position(_poz(now), 0.84, now) == "stop_felaket"
    # mfe 12 gormus pozisyon +1 tabanina dusunce breakeven
    assert eng._eval_position(_poz(now, mfe=12.0), 1.005, now) == "breakeven_stop"
    # mfe gormemis pozisyon ayni fiyatta durur (grace oncesi)
    assert eng._eval_position(_poz(now, mfe=5.0), 1.005, now) is None


def test_ratchet_trail_kademeleri():
    eng = _eng(); now = time.time()
    # tepe +40 (kademe1 %20): 1.40*0.80=1.12 altinda trail
    p = _poz(now, mfe=40.0); p["runner_peak"] = 1.40
    assert eng._eval_position(p, 1.11, now) == "runner_trail"
    p = _poz(now, mfe=40.0); p["runner_peak"] = 1.40
    assert eng._eval_position(p, 1.15, now) is None
    # tepe +80 (kademe2 %15): 1.80*0.85=1.53
    p = _poz(now, mfe=80.0, kilit=True); p["runner_peak"] = 1.80
    assert eng._eval_position(p, 1.52, now) == "runner_trail"
    # tepe +150 (kademe3 %10): 2.50*0.90=2.25
    p = _poz(now, mfe=150.0, kilit=True); p["runner_peak"] = 2.50
    assert eng._eval_position(p, 2.24, now) == "runner_trail"


def test_iki_asamali_kar_kilidi():
    eng = _eng(); now = time.time()
    # asama 0 + pnl 30: once kilit-1 (+25)
    p = _poz(now, mfe=30.0); p["runner_peak"] = 1.30
    assert eng._eval_position(p, 1.295, now) == "tp_kilit_25"
    # asama 1 + pnl 41: kilit-2 (+40)
    p = _poz(now, mfe=41.0); p["runner_peak"] = 1.41; p["kilit_asama"] = 1
    assert eng._eval_position(p, 1.405, now) == "tp_kilit_40"
    # asama 2: baska kilit yok, trail devrede
    p = _poz(now, mfe=41.0); p["runner_peak"] = 1.41; p["kilit_asama"] = 2
    assert eng._eval_position(p, 1.405, now) is None
    # eski tek-kilit uyumu: kilit_alindi=True -> asama 2 sayilir
    p2 = _poz(now, mfe=41.0, kilit=True); p2["runner_peak"] = 1.41
    assert eng._eval_position(p2, 1.405, now) is None


def test_grace_ve_timeout():
    eng = _eng(); now = time.time()
    assert eng._eval_position(_poz(now, yas_sec=16*60), 0.94, now) == "stop_gec"
    assert eng._eval_position(_poz(now, yas_sec=181*60), 1.005, now) == "timeout_180"


def test_giris_filtreleri(monkeypatch):
    kayitlar = []
    monkeypatch.setattr(r2, "safety_reject_kaydet",
                        lambda pr, m, n, d="": kayitlar.append((pr.name, n)))
    eng = _eng()
    monkeypatch.setattr(r2, "check_token",
                        lambda c, ch, t: SimpleNamespace(ok=True, kapi=None, reasons=[]))
    monkeypatch.setattr(r2.aday_paylastir, "iddia_et", lambda t, m, n: (True, None))
    monkeypatch.setattr(r2.R2Engine, "_sol_chg_h1", lambda self, c: 1.0)
    acilan = []
    monkeypatch.setattr(
        r2.R2Engine, "_open_position",
        lambda self, pair, usd, sol_h1=None, client=None: acilan.append(pair.name) or True)
    now = time.time()

    def pr(name, h1, m5, yas_dk=120):
        return SimpleNamespace(name=name, chain="solana", pool_address="P"+name,
                               token_address="T"+name, price_usd=0.001,
                               liquidity_usd=50000.0, chg_h1=h1, chg_m5=m5,
                               pool_created_at=now - yas_dk*60)
    monkeypatch.setattr(r2, "scan_all", lambda chains: [
        pr("IYI", 90.0, 12.0),
        pr("POMPA", 200.0, 12.0),        # band disi (150 ustu)
        pr("PARABOL", 90.0, 120.0),      # m5 tavani
        pr("BEBEK", 90.0, 12.0, yas_dk=20),  # yas tabani
        pr("YORGUN", 90.0, 1.0),         # m5 alt siniri (3)
    ])
    eng._enter(None)
    assert acilan == ["IYI"]
    assert ("PARABOL", "m5_tavan_skip") in kayitlar
    assert ("BEBEK", "yas_skip") in kayitlar


def test_trail_arming_yolu_peak_kaydeder():
    # 21 Tem Jimhood bug regresyonu: runner_peak onceden YOKKEN arm olan
    # pozisyonda peak kaydedilmeli ve dususte trail tetiklenmeli
    eng = _eng(); now = time.time()
    p = _poz(now, mfe=30.0)  # runner_peak YOK (bug tam burada yasiyordu)
    # arm aninda peak kaydedilir VE erken kilit ateslenir (21 Tem nesteri)
    assert eng._eval_position(p, 1.30, now) == "tp_kilit_25"
    assert p.get("runner_peak") == 1.30
    p["kilit_asama"] = 2
    assert eng._eval_position(p, 1.03, now) == "runner_trail"  # 1.30*0.8=1.04


def test_tavan_runner_icin_de_calisir():
    # 21 Tem Jimhood dersi: runner modundaki pozisyon da 180dk tavana tabi
    eng = _eng(); now = time.time()
    p = _poz(now, mfe=100.0, yas_sec=181 * 60)
    p["runner_peak"] = 2.0
    assert eng._eval_position(p, 1.9, now) == "timeout_180"
