"""Gozlem katmani P0 testleri: yazici, karar uretici, musluk farki, replay."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hibrit_trader.gozlem.karar import KararUretici, ctx_id_uret
from hibrit_trader.gozlem.musluk import Musluk
from hibrit_trader.gozlem.ortak import Bus, DurumOnbellek, Sayaclar
from hibrit_trader.gozlem.yazici import OlayYazici, SegmentYazici


def test_yazici_zarf_ve_seq(tmp_path):
    y = SegmentYazici(tmp_path, "test")
    e1 = y.yaz("Deneme", {"a": 1}, token="TOK", src="t")
    e2 = y.yaz("Deneme", {"a": 2})
    assert e1["seq"] == 1 and e2["seq"] == 2
    assert e1["v"] == 1 and e1["token"] == "TOK" and e1["ts_ms"] > 0
    y.kapat()
    # ayni saat icinde yeniden acilis: seq devralinir
    y2 = SegmentYazici(tmp_path, "test")
    e3 = y2.yaz("Deneme", {"a": 3})
    assert e3["seq"] == 3
    y2.kapat()
    dosyalar = list(tmp_path.rglob("*.jsonl"))
    assert len(dosyalar) == 1
    satirlar = [json.loads(x) for x in
                dosyalar[0].read_text().splitlines()]
    assert [s["seq"] for s in satirlar] == [1, 2, 3]


def test_yazici_fsync_kind(tmp_path):
    y = SegmentYazici(tmp_path, "motor")
    y.yaz("EngineEntryFilled", {"engine": "YZ"})
    y.kapat()


def _bus_kur(tmp_path):
    yazici = OlayYazici(tmp_path)
    ob = DurumOnbellek()
    bus = Bus(yazici, ob, Sayaclar())
    bus.karar = KararUretici(bus, ob)
    return bus, ob, yazici


def test_karar_uretici_ve_replay(tmp_path):
    async def kos():
        bus, ob, yazici = _bus_kur(tmp_path)
        g = asyncio.create_task(bus.dagitici())
        # once snapshot ve mctx gelir
        await bus.yayinla("anlik", "Snapshot", {"priceUsd": "1.5"},
                          token="TOKA", src="dexs")
        await bus.yayinla("anlik", "MarketContext", {"lansman_1h": 7},
                          src="mctx")
        await bus.q.join()
        # sonra giris
        await bus.yayinla("motor", "EngineEntryFilled",
                          {"engine": "YZ", "trade_id": "P-1", "x": 1},
                          token="TOKA", src="state:YZ")
        await bus.q.join()
        g.cancel()
        yazici.kapat()
    asyncio.run(kos())
    karar = list(tmp_path.rglob("*.karar.jsonl"))
    assert len(karar) == 1
    ev = json.loads(karar[0].read_text().splitlines()[0])
    p = ev["payload"]
    assert p["ctx_id"] == ctx_id_uret("YZ", "P-1")
    assert p["snapshot"]["payload"] == {"priceUsd": "1.5"}
    assert p["market_context"]["payload"] == {"lansman_1h": 7}
    assert p["giris"]["payload"]["x"] == 1
    # replay yukleyici ayni baglami yeniden kurup dogrulamali
    import gozlem_replay
    orij = gozlem_replay.olay_bul(tmp_path, "anlik", p["snapshot"]["seq"])
    assert orij is not None and orij["payload"] == p["snapshot"]["payload"]


def test_canli_fill_karar(tmp_path):
    async def kos():
        bus, ob, yazici = _bus_kur(tmp_path)
        g = asyncio.create_task(bus.dagitici())
        await bus.yayinla("motor", "CanliFill",
                          {"yon": "alis", "tx": "SIG1"},
                          token="TOKB", src="wal")
        await bus.yayinla("motor", "CanliFill",
                          {"yon": "sat", "tx": "SIG2"},
                          token="TOKB", src="wal")
        await bus.q.join()
        g.cancel()
        yazici.kapat()
    asyncio.run(kos())
    karar = list(tmp_path.rglob("*.karar.jsonl"))
    satirlar = karar[0].read_text().splitlines()
    assert len(satirlar) == 1          # sadece alis ctx uretir
    assert json.loads(satirlar[0])["payload"]["engine"] == "CANLI"


def test_musluk_state_farki_ilk_tur_sessiz(tmp_path):
    veri = tmp_path / "veri"
    gozlem = tmp_path / "g"
    veri.mkdir()
    gozlem.mkdir()
    (veri / "yz_state.json").write_text(json.dumps(
        {"positions": [{"trade_id": "A-1", "token_address": "T1"}]}))
    olaylar = []

    class SahteBus:
        async def yayinla(self, akis, kind, payload, **kw):
            olaylar.append((kind, payload))
    m = Musluk(SahteBus(), veri, gozlem)

    async def kos():
        await m._state_farki()      # ilk tur: sessiz tohum
        assert olaylar == []
        (veri / "yz_state.json").write_text(json.dumps(
            {"positions": [{"trade_id": "A-1", "token_address": "T1"},
                           {"trade_id": "B-2", "token_address": "T2"}]}))
        await m._state_farki()
    asyncio.run(kos())
    assert len(olaylar) == 1
    assert olaylar[0][0] == "EngineEntryFilled"
    assert olaylar[0][1]["trade_id"] == "B-2"


def test_musluk_defter_ofset(tmp_path):
    veri = tmp_path / "veri"
    gozlem = tmp_path / "g"
    veri.mkdir()
    gozlem.mkdir()
    defter = veri / "yz_trades.jsonl"
    defter.write_text(json.dumps({"trade_id": "ESKI", "pnl_usd": 1}) + "\n")
    olaylar = []

    class SahteBus:
        async def yayinla(self, akis, kind, payload, **kw):
            olaylar.append((kind, payload))
    m = Musluk(SahteBus(), veri, gozlem)

    async def kos():
        # ilk gorus: EOF'tan basla, eski satir olay uretmez
        await m._dosya_kuyrugu(str(defter),
                               lambda t: m._defter_satiri("YZ", t))
        assert olaylar == []
        with open(defter, "a") as f:
            f.write(json.dumps({"trade_id": "YENI", "pnl_usd": -2,
                                "token_address": "T9"}) + "\n")
        await m._dosya_kuyrugu(str(defter),
                               lambda t: m._defter_satiri("YZ", t))
    asyncio.run(kos())
    assert len(olaylar) == 1
    assert olaylar[0][0] == "EngineExitFilled"
    assert olaylar[0][1]["trade_id"] == "YENI"


def test_swap_debi_valfi(tmp_path):
    from hibrit_trader.gozlem.swap_r0 import SwapR0
    olaylar = []

    class SahteBus:
        async def yayinla(self, akis, kind, payload, **kw):
            olaylar.append((kind, payload))
    ob = DurumOnbellek()
    ob.izlenen = {"POOL1": {"token": "T1"}}
    s = SwapR0(SahteBus(), ob)
    s.esik = 10

    async def kos():
        for i in range(15):
            await s.isle("POOL1", "r0", {"result": {
                "context": {"slot": 100 + i},
                "value": {"signature": f"S{i}", "err": None,
                          "logs": ["x"]}}})
    asyncio.run(kos())
    kinds = [k for k, _ in olaylar]
    assert kinds.count("SwapObserved") == 10        # esige kadar tam mod
    assert "ThrottleModeChanged" in kinds           # asan mesajla puls moduna
    assert s._mod["POOL1"] == "puls"
    assert s._puls["POOL1"]["adet"] == 5            # kalan 5 birikimde
    assert s._puls["POOL1"]["son_sig"] == "S14"
