"""Kacis payi: rug siniflamasi ve gecikme senaryolari."""

import importlib.util
import sys
from pathlib import Path

_kok = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_kok / "src"))

_spec = importlib.util.spec_from_file_location(
    "kacis_payi", _kok / "scripts" / "kacis_payi.py")
kp = importlib.util.module_from_spec(_spec)
sys.modules["kacis_payi"] = kp
_spec.loader.exec_module(kp)

from hibrit_trader.edge.yol_arsivi import Yol  # noqa: E402


def test_rug_kacisi_ve_gecikme():
    # cikis 100'de 1.02; 15sn sonra 0.90, 40sn sonra 0.10 (rug)
    yol = Yol("T", [(80, 1.0), (115, 0.90), (140, 0.10), (200, 0.08)])
    t = {"ts": 100.0, "exit_price": 1.02, "entry_price": 1.0,
         "pnl_pct": 2.0, "token_address": "T"}
    r = kp.islem_analiz(t, yol)
    assert r["rug_kacisi"] is True
    assert r["dusus_pct"] < -90
    # 15sn gecikme: ilk tick ts>=115 -> 0.90: pnl -10 olurdu, delta -12
    assert r["gecikme_delta"]["15"] == -12.0
    assert r["gecikme_delta"]["30"] == round(100 * (0.10 / 1.0 - 1)
                                             - 2.0, 3)


def test_normal_cikis_rug_degil():
    yol = Yol("T", [(80, 1.0), (120, 1.01), (200, 0.98), (400, 0.95)])
    t = {"ts": 100.0, "exit_price": 1.02, "entry_price": 1.0,
         "pnl_pct": 2.0, "token_address": "T"}
    r = kp.islem_analiz(t, yol)
    assert r["rug_kacisi"] is False
    assert r["gecikme_delta"]["15"] == round(100 * (1.01 - 1) - 2.0, 3)


def test_kapsam_yoklugu_none():
    t = {"ts": 100.0, "exit_price": 1.0, "entry_price": 1.0,
         "pnl_pct": 0.0}
    assert kp.islem_analiz(t, None) is None
    kisa = Yol("T", [(80, 1.0), (105, 1.0)])
    assert kp.islem_analiz(t, kisa) is None
