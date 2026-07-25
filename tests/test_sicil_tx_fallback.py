"""Yaratici sicili tx-fallback: kalici cache, tavan, hata devresi."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import hibrit_trader.gozlem.yaratici_sicil as ys


def test_fallback_cache_ve_tavan(tmp_path, monkeypatch):
    (tmp_path / "gozlem").mkdir(parents=True)
    monkeypatch.setattr(ys, "TX_TAVAN", 2)
    monkeypatch.setattr(ys.time, "sleep", lambda s: None)
    # parser'dan bagimsiz birim test: S1 loglari cozulur, digerleri None
    monkeypatch.setattr(
        ys, "create_ayristir",
        lambda logs: ({"mint": "M1pump", "yaratici": "Y1"}
                      if logs == ["S1-log"] else None))
    cagri = []

    def tx_getir(sig):
        cagri.append(sig)
        return ["S1-log"] if sig == "S1" else ["Program log: bos"]

    basarisiz = [("S1", 3.0), ("S2", 2.0), ("S3", 1.0)]
    cache, oz = ys._tx_fallback(basarisiz, tmp_path, tx_getir)
    assert oz["fetch_n"] == 2                 # tavan 2: S3 ertesi geceye
    assert cache["S1"]["mint"] == "M1pump"
    assert cache["S2"] is None                # negatif de KALICI
    assert "S3" not in cache
    # ikinci kosu: cache'liler fetch edilmez, S3 tamamlanir
    cagri.clear()
    cache2, oz2 = ys._tx_fallback(basarisiz, tmp_path, tx_getir)
    assert cagri == ["S3"] and oz2["fetch_n"] == 1
    disk = json.loads((tmp_path / "gozlem" / "create_tx_cache.json")
                      .read_text())
    assert set(disk) == {"S1", "S2", "S3"}


def test_ag_hatasi_cachelenmez_ve_devre(tmp_path, monkeypatch):
    (tmp_path / "gozlem").mkdir(parents=True)
    monkeypatch.setattr(ys.time, "sleep", lambda s: None)

    def patlak(sig):
        raise OSError("rpc down")

    basarisiz = [(f"S{i}", float(i)) for i in range(30)]
    cache, oz = ys._tx_fallback(basarisiz, tmp_path, patlak)
    assert cache == {}                        # hata cache'lenmedi
    assert oz["hata_n"] == 10                 # devre: geceyi bosa harcamaz
    assert oz["fetch_n"] == 0
