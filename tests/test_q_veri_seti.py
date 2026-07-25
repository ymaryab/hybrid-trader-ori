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
