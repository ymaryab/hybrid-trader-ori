"""Kural karnesi: hucre ozetleri ve vaad uyum hesaplari."""

import importlib.util
import sys
from pathlib import Path

_yol = Path(__file__).resolve().parents[1] / "scripts" / "kural_karnesi.py"
_spec = importlib.util.spec_from_file_location("kural_karnesi", _yol)
kk = importlib.util.module_from_spec(_spec)
sys.modules["kural_karnesi"] = kk
_spec.loader.exec_module(kk)


def _t(pnl, mfe=0.0, hold=60.0, usd=1.0):
    return {"pnl_pct": pnl, "mfe_pct": mfe, "hold_sec": hold,
            "pnl_usd": usd}


def test_seviye_kaymasi():
    oz = kk.hucre_ozeti([_t(-8.0), _t(-6.5), _t(-11.0)],
                        kk.VAATLER["stop_6"])
    assert oz["hedef"] == -6.0
    assert oz["kayma_puan"] == -2.0          # medyan -8: esik-delme gorunur
    assert oz["uyum"] == round(2 / 3, 3)     # -8 ve -6.5 tolerans icinde


def test_pozitif_cikis_uyumu():
    oz = kk.hucre_ozeti([_t(0.5), _t(1.2), _t(-0.3)],
                        kk.VAATLER["timeout_karla"])
    assert oz["uyum"] == round(2 / 3, 3)


def test_kucuk_zarar_uyumu():
    oz = kk.hucre_ozeti([_t(-1.0), _t(-2.9), _t(-8.0), _t(0.5)],
                        kk.VAATLER["erken_zayif"])
    assert oz["uyum"] == 0.75                # -8 vaadi bozan tek cikis


def test_mfe_yakalama():
    oz = kk.hucre_ozeti([_t(26.6, mfe=53.7), _t(10.0, mfe=20.0),
                         _t(-0.5, mfe=0.5)],   # mfe<=1 orana girmez
                        kk.VAATLER["runner_trail"])
    assert oz["mfe_yakalama_medyan"] == 0.498   # (0.495+0.5)/2


def test_guven_orneklemle_artar():
    az = kk.hucre_ozeti([_t(1.0)] * 2, kk.VAATLER["timeout_karla"])
    cok = kk.hucre_ozeti([_t(1.0)] * 90, kk.VAATLER["timeout_karla"])
    assert az["guven"] < 0.2 < 0.9 <= cok["guven"]
