"""Sadakat replay eslemesi + q_baglam kaydi testleri."""

import importlib.util
import sys
from pathlib import Path

_kok = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_kok / "src"))


def _yukle(ad):
    spec = importlib.util.spec_from_file_location(
        ad, _kok / "scripts" / f"{ad}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[ad] = mod
    spec.loader.exec_module(mod)
    return mod


sr = _yukle("sadakat_rapor")
qvs = _yukle("q_veri_seti")

from hibrit_trader.edge.simulator import tp_politikasi  # noqa: E402
from hibrit_trader.edge.yol_arsivi import Yol           # noqa: E402


def test_islem_replay_gercek_giristen():
    """Replay, yolun basindan degil GERCEK giris aninden kosar."""
    yol = Yol("T", [(100, 1.0), (200, 2.0), (300, 2.1), (400, 2.06)])
    t = {"ts": 400.0, "hold_sec": 150.0,       # giris 250'de
         "entry_price": 2.0, "token_address": "T"}
    r = sr.islem_replay(t, yol, tp_politikasi(2.0, 60.0, stop_pct=-20.0))
    assert r is not None
    assert r["replay_cikis"] == "tp"           # 2.1/2.0 = +5 >= tp2
    assert r["replay_pnl"] == 2.0
    assert r["tick_n"] == 2                    # yalniz giris sonrasi


def test_islem_replay_kapsam_yoklugu():
    t = {"ts": 400.0, "hold_sec": 10.0, "entry_price": 2.0}
    assert sr.islem_replay(t, None, tp_politikasi(2, 60)) is None
    kisa = Yol("T", [(100, 1.0), (200, 2.0)])
    assert sr.islem_replay({"ts": 400.0, "hold_sec": 10.0,
                            "entry_price": 2.0}, kisa,
                           tp_politikasi(2, 60)) is None


def test_etiket_gruplari():
    assert sr.grup("tp_2") == sr.grup("tp") == "tp"
    assert sr.grup("stop_felaket") == sr.grup("stop") == "stop"
    assert sr.grup("timeout_karla") == "timeout"
    assert sr.grup("runner_trail") == "runner"   # HIGH-6: kademeli grubu


def test_baglam_q_puls_ve_tetik():
    pulslar = [(1000.0, {"lansman_1h": 50, "havuz_1h": 5}),
               (2000.0, {"lansman_1h": 80, "havuz_1h": 8})]
    puls_ts = [p[0] for p in pulslar]
    dogumlar = [500.0, 1500.0, 1900.0, 5000.0]
    b = qvs.baglam_q(1950.0, pulslar, puls_ts, dogumlar)
    assert b["lansman_1h"] == 80               # en yakin puls (2000)
    assert b["ekg_tetik_1h"] == 3              # 500+1500+1900 (3600s pencere)
    assert b["sayim_fix_sonrasi"] is False
    uzak = qvs.baglam_q(9000.0, pulslar, puls_ts, dogumlar)
    assert uzak["lansman_1h"] is None          # 900 sn'den uzak puls
    assert uzak["ekg_tetik_1h"] == 0           # 5000 < 9000-3600
