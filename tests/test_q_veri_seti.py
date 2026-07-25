"""q veri seti turetimleri (HAT 2): pay hesaplari ve ilk-olcum kurali."""

import importlib.util
import sys
from pathlib import Path

_yol = Path(__file__).resolve().parents[1] / "scripts" / "q_veri_seti.py"
_spec = importlib.util.spec_from_file_location("q_veri_seti", _yol)
qvs = importlib.util.module_from_spec(_spec)
sys.modules["q_veri_seti"] = qvs
_spec.loader.exec_module(qvs)


def test_holder_pay_hesabi():
    ev = {"ts_ms": 1, "payload": {
        "arz": {"uiAmount": 1000.0},
        "hesaplar": [{"miktar": "500"}, {"miktar": "300"},
                     {"miktar": "100"}, {"miktar": None}]}}
    q = qvs.holder_q(ev)
    assert q["top1_pay"] == 0.5
    assert q["top5_pay"] == 0.9
    assert q["hesap_n"] == 3                      # None miktar sayilmaz


def test_holder_arz_yoksa_pay_yok():
    q = qvs.holder_q({"ts_ms": 1, "payload": {"hesaplar": [
        {"miktar": "5"}]}})
    assert "top1_pay" not in q and q["hesap_n"] == 1


def test_lp_top1_pay():
    ev = {"ts_ms": 1, "payload": {
        "amm": "raydium_v4", "parse_guvenli": True,
        "lp_arz": {"uiAmount": 200.0},
        "lp_hesaplar": [{"miktar": "150"}, {"miktar": "50"}]}}
    q = qvs.lp_q(ev)
    assert q["lp_top1_pay"] == 0.75
    assert q["amm"] == "raydium_v4"


def test_pumpswap_lp_alansiz():
    q = qvs.lp_q({"ts_ms": 1, "payload": {"amm": "pumpswap"}})
    assert q["amm"] == "pumpswap" and "lp_top1_pay" not in q


def test_yaratici_asof_sizintisiz():
    """Tokenin kendi sonucu ve sonraki lansmanlar SAYILMAZ (Duzeltme 1)."""
    lans = {"T0": ["C", 100.0],     # onceki: runner
            "T1": ["C", 200.0],     # onceki: EKG'de yok (dead)
            "HEDEF": ["C", 300.0],  # incelenen token (kendisi runner!)
            "T3": ["C", 400.0]}     # SONRAKI: sayilmamali
    ath = {"T0": 250.0, "HEDEF": 500.0, "T3": 999.0}
    r = qvs.yaratici_asof("HEDEF", lans, ath)
    assert r["lansman_n_asof"] == 2          # T0 + T1
    assert r["runner_n_asof"] == 1           # yalniz T0
    assert r["runner_var_asof"] == 1.0
    assert r["dead_orani_asof"] == 0.5       # T1 izlenmedi
    # ilk lansman: onceki yok -> runner_var None (bilinmiyor), bayrak True
    r0 = qvs.yaratici_asof("T0", lans, ath)
    assert r0["ilk_lansman_mi"] is True
    assert r0["runner_var_asof"] is None
    assert qvs.yaratici_asof("YOK", lans, ath) is None


def test_ilk_olcum_kurali(tmp_path, monkeypatch):
    """Ayni token icin IKINCI holder olcumu yok sayilir (terfi ani)."""
    import json
    gun = tmp_path / "gozlem" / "events" / "20260725"
    gun.mkdir(parents=True)
    with open(gun / "08.sensor.jsonl", "w") as f:
        for ts, pay in ((1, "100"), (2, "999")):
            f.write(json.dumps({
                "kind": "HolderKonsantrasyon", "token": "TOK", "ts_ms": ts,
                "payload": {"arz": {"uiAmount": 1000.0},
                            "hesaplar": [{"miktar": pay}]}}) + "\n")
    ilk = qvs.sensor_ilk_olcumler(tmp_path)
    assert ilk["TOK"]["holder"]["ts_ms"] == 1     # ilk olcum kaldi
