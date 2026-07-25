"""Edge mimari iskeleti (HAT 2) testleri: arsiv, simulator, tahsis, motor."""

import json

from hibrit_trader.edge.edge_motoru import EdgeMotoru
from hibrit_trader.edge.kosullama import TekKatman
from hibrit_trader.edge.simulator import (degerlendir, runner_politikasi,
                                          tp_politikasi)
from hibrit_trader.edge.tahsis import HepsiLidere
from hibrit_trader.edge.yol_arsivi import GozlemYolArsivi, Yol, YolArsivi


def _ekg_yaz(tmp_path, seriler):
    p = tmp_path / "kosucu_ekg.jsonl"
    with open(p, "w") as fh:
        for token, ticks in seriler.items():
            for ts, fiyat in ticks:
                fh.write(json.dumps({"token_address": token, "ts": ts,
                                     "price_usd": fiyat}) + "\n")
    return tmp_path


def test_arsiv_okur_ve_eler(tmp_path):
    veri = _ekg_yaz(tmp_path, {
        "AAA": [(100, 1.0), (160, 1.1), (220, 1.3)],
        "KISA": [(100, 1.0)],                      # min_tick alti
    })
    a = YolArsivi(veri)
    s = a.sayim()
    assert s == {"token_n": 2, "yeterli_n": 1, "elenen_n": 1, "min_tick": 3}
    yol = a.yol("AAA")
    assert yol.ilk_fiyat == 1.0
    assert round(yol.ath_pct) == 30
    assert yol.yasam_dk == 2.0
    assert a.yol("KISA") is None


def test_simulator_tp_stop_timeout():
    yol = Yol("T", [(0, 1.0), (60, 1.01), (120, 1.06), (180, 0.5)])
    tp = degerlendir(yol, tp_politikasi(tp_pct=5, timeout_dk=30))
    assert tp["cikis"] == "tp" and tp["pnl_pct"] == 5
    stop = degerlendir(yol, tp_politikasi(tp_pct=50, timeout_dk=30,
                                          stop_pct=-6))
    assert stop["cikis"] == "stop" and stop["pnl_pct"] == -50
    kisa = degerlendir(yol, tp_politikasi(tp_pct=50, timeout_dk=1.5))
    assert kisa["cikis"] == "timeout"


def test_simulator_runner_trail():
    yol = Yol("T", [(0, 1.0), (60, 1.3), (120, 1.6), (180, 1.4)])
    r = degerlendir(yol, runner_politikasi(kilit_pct=25, trail_pct=10,
                                           timeout_dk=999))
    assert r["cikis"] == "trail"
    assert r["pnl_pct"] == 40                      # tepe 60 - dusus
    assert r["mfe"] == 60


def test_edge_motoru_tek_katman(tmp_path):
    veri = _ekg_yaz(tmp_path, {
        "KAZANAN": [(100, 1.0), (160, 1.03), (220, 1.08)],
        "KAYBEDEN": [(100, 1.0), (160, 0.97), (220, 0.90)],
    })
    em = EdgeMotoru(YolArsivi(veri), TekKatman())
    e = em.edge(tp_politikasi(tp_pct=5, timeout_dk=999))
    assert e["katman"] == "hepsi" and e["n"] == 2
    assert e["kazanma_orani"] == 0.5
    assert e["cikislar"] == {"tp": 1, "seri_sonu": 1}


def test_gozlem_yogun_arsiv(tmp_path):
    gun = tmp_path / "gozlem" / "events" / "20260725"
    gun.mkdir(parents=True)
    with open(gun / "08.anlik.jsonl", "w") as fh:
        for ts_ms, fiyat in ((1000, "1.0"), (16000, "1.05"),
                             (31000, "1.10"), (16000, "1.06")):
            fh.write(json.dumps({"kind": "Snapshot", "token": "TOK",
                                 "ts_ms": ts_ms,
                                 "payload": {"priceUsd": fiyat}}) + "\n")
        fh.write(json.dumps({"kind": "MarketContext", "ts_ms": 5,
                             "payload": {}}) + "\n")
        fh.write(json.dumps({"kind": "Snapshot", "token": "TOK",
                             "ts_ms": 40000,
                             "payload": {"priceUsd": None}}) + "\n")
    a = GozlemYolArsivi(tmp_path)
    yol = next(a.yollar())
    assert yol.token == "TOK"
    assert len(yol.ticks) == 3                 # ayni ts son fiyatla tekil
    assert yol.ticks[1] == (16.0, 1.06)        # son gorulen kazandi
    assert a.sayim()["yeterli_n"] == 1
    # gun filtresi: eslesmeyen onek bos doner
    assert list(GozlemYolArsivi(tmp_path,
                                gun_onek=["20260101"]).yollar()) == []


def test_tahsis_hepsi_lidere():
    t = HepsiLidere()
    assert t.dagit({"r1": 2.0, "v7": 3.5}) == {"v7": 1.0}
    assert t.dagit({"r1": -1.0, "v7": -0.2}) == {}          # salter iner
    paylar = t.dagit({"a": 1.0, "b": 1.0})
    assert paylar == {"a": 1.0} and sum(paylar.values()) == 1.0
