"""Gozlemci RSS budamalari (HAT 3 kalem 3, 25 Tem P0).

Sinirsiz buyudugu tespit edilen RAM yapilari: DurumOnbellek.son_snapshot
(izlenmeyen tokenler birikir) ve KararUretici._uretilen (ctx dedup
kumesi). Budama + FIFO tavan; adetler ObserverHealth.nesneler ile izlenir.
"""

from types import SimpleNamespace

from hibrit_trader.gozlem.karar import KararUretici
from hibrit_trader.gozlem.ortak import DurumOnbellek


def test_snapshot_budamasi():
    ob = DurumOnbellek()
    for i in range(10):
        ob.guncelle({"kind": "Snapshot", "token": f"T{i}", "ts_ms": i})
    assert len(ob.son_snapshot) == 10
    atilan = ob.buda({"T1", "T3"})
    assert atilan == 8
    assert set(ob.son_snapshot) == {"T1", "T3"}
    assert ob.buda({"T1", "T3"}) == 0            # idempotent


def test_ctx_uretilen_fifo_tavani(monkeypatch):
    k = KararUretici(bus=SimpleNamespace(), onbellek=DurumOnbellek())
    monkeypatch.setattr(KararUretici, "URETILEN_TAVAN", 5)
    for i in range(12):
        k._uretilen_ekle(f"cid{i}")
    assert len(k._uretilen) == 5
    assert len(k._uretilen_sira) == 5
    assert "cid11" in k._uretilen                # en yeniler kalir
    assert "cid0" not in k._uretilen             # en eskiler atilir
    assert set(k._uretilen_sira) == k._uretilen  # iki yapi senkron
