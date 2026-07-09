"""makas_probe: motorsuz round-trip makas olcumu dogrulama."""

from __future__ import annotations

import json
from types import SimpleNamespace

from hibrit_trader import makas_probe


class _FakeBroker:
    """al: 1.01'den doldurur, sat: 1.00'den; round-trip ~100 bps."""

    def __init__(self, sat_yok: set[str] | None = None):
        self.sat_yok = sat_yok or set()
        self.calls: list[tuple] = []

    def _quote(self, addr, yon, miktar, bps):
        self.calls.append((addr, yon, miktar, bps))
        if yon == "al":
            return SimpleNamespace(fiyat=1.01, miktar_token=miktar / 1.01,
                                   route=["Orca"]), None
        if addr in self.sat_yok:
            return None, "likidite yok"
        return SimpleNamespace(fiyat=1.00, miktar_token=miktar,
                               route=["Raydium"]), None


def _evren_yaz(tmp_path, semboller):
    (tmp_path / "m1_universe.json").write_text(json.dumps({
        "tokens": [{"symbol": s, "token_address": f"Mint{s}"} for s in semboller],
    }))


def test_probe_turu_yazar(monkeypatch, tmp_path):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    _evren_yaz(tmp_path, ["AAA", "BBB"])
    fake = _FakeBroker()
    monkeypatch.setattr("hibrit_trader.broker._get_golge_broker", lambda: fake)
    monkeypatch.setattr("hibrit_trader.makas_probe.time.sleep", lambda s: None)

    assert makas_probe.probe_turu() == 2

    rows = [json.loads(l) for l in
            (tmp_path / "dryrun_fills.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    for row in rows:
        assert row["tur"] == "probe"
        assert row["engine"] == "PROBE"
        assert row["fark_bps"] == 100.0
        assert row["al_fiyat"] == 1.01
        assert row["sat_fiyat"] == 1.00
        assert row["usd"] == makas_probe.PROBE_USD
    # sat quote'u al'in dondurdugu token miktariyla istenmis
    sat_call = [c for c in fake.calls if c[1] == "sat"][0]
    assert abs(sat_call[2] - makas_probe.PROBE_USD / 1.01) < 1e-9


def test_probe_bos_evren_sifir(monkeypatch, tmp_path):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.makas_probe.time.sleep", lambda s: None)
    assert makas_probe.probe_turu() == 0
    assert not (tmp_path / "dryrun_fills.jsonl").exists()


def test_probe_quote_gelmeyen_token_atlanir(monkeypatch, tmp_path):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    _evren_yaz(tmp_path, ["AAA", "BOZUK"])
    fake = _FakeBroker(sat_yok={"MintBOZUK"})
    monkeypatch.setattr("hibrit_trader.broker._get_golge_broker", lambda: fake)
    monkeypatch.setattr("hibrit_trader.makas_probe.time.sleep", lambda s: None)

    assert makas_probe.probe_turu() == 1

    rows = [json.loads(l) for l in
            (tmp_path / "dryrun_fills.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAA"
