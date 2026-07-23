"""Otonom secici v2: state-trigger, cift bayrak, mutabakat, olay omurgasi."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import hibrit_trader.otonom_secici as osec


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOZLEM_DATA_DIR", str(tmp_path / "gozlem"))
    monkeypatch.setattr(osec, "_yazici", None)   # yazici singleton sifirla
    yield tmp_path


def _kur(d: Path, motor: str, start=1000.0, created=0.0,
         trades=(), equity=()):
    (d / f"{motor}_state.json").write_text(json.dumps(
        {"start_balance": start, "created_ts": created}))
    with open(d / f"{motor}_trades.jsonl", "w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")
    if equity:
        with open(d / f"{motor}_equity.jsonl", "w") as f:
            for e in equity:
                f.write(json.dumps(e) + "\n")


def _olaylar(tmp_path):
    out = []
    for yol in sorted((tmp_path / "gozlem").rglob("*.otonom.jsonl")):
        for ln in yol.read_text().splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


def test_kayan_degisim_ham_girdiler(tmp_path):
    """Kullanici ornegi (+%1) ve denetlenebilirlik alanlari."""
    now = time.time()
    _kur(tmp_path, "yz",
         trades=[{"ts": now - 90 * 60, "trade_id": "A", "pnl_usd": 0.0},
                 {"ts": now - 30 * 60, "trade_id": "B", "pnl_usd": 10.0}],
         equity=[{"ts": now - 61 * 60, "eq": 1000.0}])
    s = osec.kayan_degisim("yz", 60)
    assert abs(s["pct"] - 1.0) < 1e-6
    assert s["equity_now"] == 1010.0
    assert s["equity_baseline"] == 1000.0
    assert s["baseline_source"] == "equity_ornek"
    assert abs(s["baseline_ts"] - (now - 61 * 60)) < 2


def test_lider_esitlik_bozma_deterministik():
    sk = {"a": {"pct": 2.0, "islem": 1}, "b": {"pct": 2.0, "islem": 1},
          "c": {"pct": 1.0, "islem": 1}}
    assert osec.lider_bul(sk, "b") == "b"   # esitlikte mevcut kazanir
    assert osec.lider_bul(sk, "c") == "a"   # sonra alfabetik
    assert osec.aday_sec(sk, "b", min_islem=0) is None  # mevcut lider: kal


def test_aday_sec_state_trigger():
    """Lider onceki turla ayni olsa bile mevcut != lider ise aday cikar."""
    sk = {"yz": {"pct": 3.0, "islem": 2}, "r1": {"pct": 0.5, "islem": 1}}
    assert osec.aday_sec(sk, "r1", min_islem=0) == "yz"
    assert osec.aday_sec(sk, "r1", min_islem=0) == "yz"  # tekrarda da ayni
    assert osec.aday_sec(sk, "yz", min_islem=0) is None


def test_pozitif_esik_yuzde1(monkeypatch):
    """23 Tem karari: %1 altindaki artis negatif sayilir."""
    sk = {"yz": {"pct": 0.9, "islem": 5}, "r1": {"pct": 0.4, "islem": 3}}
    assert osec.aday_sec(sk, "r1", min_islem=0) is None      # 0.9 < esik 1.0
    sk["yz"]["pct"] = 1.0
    assert osec.aday_sec(sk, "r1", min_islem=0) == "yz"      # tam esik: gecer
    assert osec.aday_sec(sk, "r1", min_islem=0, esik=2.0) is None


def test_durum_gocu_ve_cift_bayrak(tmp_path):
    (tmp_path / osec.DURUM_DOSYA).write_text(json.dumps(
        {"acik": True, "pencere_dk": 45}))
    d = osec.durum_oku()
    assert d["user_enabled"] is True          # eski format gocu
    assert d["system_enabled"] is True
    assert d["pencere_dk"] == 45
    d["system_enabled"] = False
    osec.durum_yaz(d)
    d2 = osec.durum_oku()
    assert d2["user_enabled"] and not d2["system_enabled"]


def test_mutabakat_completed_ve_failed(tmp_path):
    niyet = {"switch_id": "sw-1", "eval_id": "ev-1", "from": "r1",
             "to": "yz", "bas_ts": time.time() - 30,
             "positions_closed": 2, "tasfiye_sure_sec": 12.5}
    (tmp_path / osec.NIYET_DOSYA).write_text(json.dumps(niyet))
    m = osec.gecis_mutabakati("yz")           # env yeni kaynaga esit
    assert m["success"] is True
    assert not (tmp_path / osec.NIYET_DOSYA).exists()
    (tmp_path / osec.NIYET_DOSYA).write_text(json.dumps(niyet))
    m2 = osec.gecis_mutabakati("r1")          # env degismemis: FAILED
    assert m2["success"] is False
    evs = _olaylar(tmp_path)
    kinds = [e["kind"] for e in evs]
    assert kinds == ["AutonomSwitchCompleted", "AutonomSwitchFailed"]
    p = evs[0]["payload"]
    assert p["switch_id"] == "sw-1" and p["actor"] == "system"
    assert p["git_sha"] and p["positions_closed"] == 2


def test_olay_omurgaya_yazilir_zarfli(tmp_path):
    ev = osec.olay_yaz("AutonomConfigChanged",
                       {"alan": "pencere_dk", "eski": 60, "yeni": 30},
                       actor="user")
    assert ev["seq"] == 1 and ev["kind"] == "AutonomConfigChanged"
    evs = _olaylar(tmp_path)
    assert evs[0]["payload"]["actor"] == "user"
    assert evs[0]["payload"]["git_sha"]


def test_tasfiye_kancasi_hibrit(tmp_path):
    """Dogal fazda giris blogu var ama zorla satis yok; zorla_ts gecince
    zorla satis; eski format (json degil) guvenli tarafta zorla sayilir."""
    import hibrit_trader.canli_session as cs
    assert cs.tasfiye_talebi_var() is False
    assert cs.tasfiye_zorla_aktif() is False
    (tmp_path / cs.TASFIYE_FILE).write_text(json.dumps(
        {"zorla_ts": time.time() + 300}))
    assert cs.tasfiye_talebi_var() is True      # giris blogu hemen
    assert cs.tasfiye_zorla_aktif() is False    # dogal faz: zorlama yok
    (tmp_path / cs.TASFIYE_FILE).write_text(json.dumps(
        {"zorla_ts": time.time() - 1}))
    assert cs.tasfiye_zorla_aktif() is True     # sure doldu: zorla
    (tmp_path / cs.TASFIYE_FILE).write_text("eski-format")
    assert cs.tasfiye_zorla_aktif() is True     # parse edilemez: zorla


def test_rejim_salteri(tmp_path):
    """Hepsi <=0 -> salter iner; pozitif lider -> kalkar (23 Tem karari)."""
    osec._salter_indir("test")
    assert (tmp_path / "CANLI_DUR").exists()
    osec._salter_kaldir()
    assert not (tmp_path / "CANLI_DUR").exists()
