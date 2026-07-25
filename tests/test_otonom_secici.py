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


def test_pozitif_esik(monkeypatch):
    """Esik altindaki artis negatif sayilir (24 Tem: vars. 1.5)."""
    sk = {"yz": {"pct": 1.4, "islem": 5}, "r1": {"pct": 0.4, "islem": 3}}
    assert osec.aday_sec(sk, "r1", min_islem=0) is None      # 1.4 < esik 1.5
    sk["yz"]["pct"] = 1.5
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
    ZORLA yalniz SAHIP surecte (pid eslesir); pid'siz/bozuk dosya yetim
    sayilir, zorla ateslemez (25 Tem P0, 24 Tem yetim vakasi)."""
    import os as _os

    import hibrit_trader.canli_session as cs
    assert cs.tasfiye_talebi_var() is False
    assert cs.tasfiye_zorla_aktif() is False
    (tmp_path / cs.TASFIYE_FILE).write_text(json.dumps(
        {"zorla_ts": time.time() + 300, "pid": _os.getpid()}))
    assert cs.tasfiye_talebi_var() is True      # giris blogu hemen
    assert cs.tasfiye_zorla_aktif() is False    # dogal faz: zorlama yok
    (tmp_path / cs.TASFIYE_FILE).write_text(json.dumps(
        {"zorla_ts": time.time() - 1, "pid": _os.getpid()}))
    assert cs.tasfiye_zorla_aktif() is True     # sure doldu + sahibiz: zorla
    (tmp_path / cs.TASFIYE_FILE).write_text(json.dumps(
        {"zorla_ts": time.time() - 1}))         # pid yok = yetim
    assert cs.tasfiye_zorla_aktif() is False
    (tmp_path / cs.TASFIYE_FILE).write_text("eski-format")
    assert cs.tasfiye_zorla_aktif() is False    # bozuk = yetim, zorla yok


def test_rejim_salteri(tmp_path):
    """Hepsi <=0 -> salter iner; pozitif lider -> kalkar (23 Tem karari)."""
    osec._salter_indir("test")
    assert (tmp_path / "CANLI_DUR").exists()
    osec._salter_kaldir()
    assert not (tmp_path / "CANLI_DUR").exists()


def test_kayan_degisim_acik_poz_mtm_dahil(tmp_path):
    """24 Tem karari: acik pozisyonun gerceklesmemis K/Z'si aninda yansir."""
    now = time.time()
    _kur(tmp_path, "yz",
         trades=[{"ts": now - 5 * 60, "trade_id": "A", "pnl_usd": 10.0}],
         equity=[{"ts": now - 16 * 60, "eq": 1000.0}])
    # acik pozisyon: 100$ maliyet, +%20 anlik -> +20$ unrealized
    st = json.loads((tmp_path / "yz_state.json").read_text())
    st["positions"] = [{"entry_price": 1.0, "last_price": 1.2,
                        "cost_usd": 100.0}]
    (tmp_path / "yz_state.json").write_text(json.dumps(st))
    import hibrit_trader.jsonl_onbellek as jo
    jo._ONBELLEK.clear()
    s = osec.kayan_degisim("yz", 15)
    assert s["acik_poz_unreal"] == 20.0
    assert s["equity_now"] == 1030.0        # 1000 + 10 gerceklesen + 20 MTM
    assert abs(s["pct"] - 3.0) < 1e-6       # 1030/1000-1


def test_egim_kurali(monkeypatch):
    """24 Tem: marj icinde egim kazanir; marj disinda seviye; veto."""
    monkeypatch.setattr(osec, "MARJ_PUAN", 1.0)
    sk = {"v7t": {"pct": 1.7, "islem": 3}, "v7": {"pct": 1.5, "islem": 3}}
    eg = {"v7t": -0.1, "v7": 0.3}
    # kullanici ornegi: fark 0.2 (marj ici), v7 yukselen -> v7 secilir
    assert osec.aday_sec(sk, "r9", min_islem=0, egimler=eg) == "v7"
    # fark belirgin: seviye kazanir (v7t 3.0 vs v7 1.5)
    sk2 = {"v7t": {"pct": 3.0, "islem": 3}, "v7": {"pct": 1.5, "islem": 3}}
    assert osec.aday_sec(sk2, "r9", min_islem=0, egimler=eg) == "v7t"
    # veto: mevcut v7 uygunken sonen v7t'ye marj icinden gecilmez
    assert osec.aday_sec(sk, "v7", min_islem=0, egimler=eg) is None
    # egim verisi yoksa (ilk tur) eski davranis: seviye lideri
    assert osec.aday_sec(sk, "r9", min_islem=0, egimler=None) == "v7t"


def test_tasfiye_dogal_fazda_zayif_kagit_aninda(tmp_path):
    """24 Tem: dogal fazda zayif kagit beklemez; umutlu kagit bekler."""
    import hibrit_trader.canli_session as cs
    (tmp_path / cs.TASFIYE_FILE).write_text(json.dumps(
        {"zorla_ts": time.time() + 600}))
    assert cs.tasfiye_talebi_var() and not cs.tasfiye_zorla_aktif()
    class Sahte:  # yalniz _eval_position'in tasfiye dalini test ediyoruz
        _eval_position = cs.CanliEngine._eval_position
    e = Sahte()
    now = time.time()
    zayif = {"entry_price": 1.0, "mfe_pct": 0.5}
    assert e._eval_position(zayif, 0.99, now) == "otonom_tasfiye"   # ekside+gucsuz
    umutlu = {"entry_price": 1.0, "mfe_pct": 4.0, "mae_pct": 0.0,
              "last_price": 1.0, "opened_ts": now - 60, "pair": "T / SOL"}
    # +2'de, guc gostermis: dogal faza birakilir (kaynak kurali calisir)
    try:
        r = e._eval_position(umutlu, 1.02, now)
    except AttributeError:
        r = "kaynaga_devredildi"   # delegasyona ulasti = tasfiye kesmedi
    assert r != "otonom_tasfiye"


def test_zombi_ve_cuce_kasa_koruma(monkeypatch):
    """24 Tem sabah fixi: sifir egimli zombi egim kazanamaz; islemsiz ve
    cuce kasali motor liderlige aday olamaz (R1 05:57 vakasi)."""
    monkeypatch.setattr(osec, "MIN_KASA_USD", 150.0)
    sk = {"yzn1": {"pct": 1.32, "islem": 3, "equity_now": 400.0},
          "v7hizli": {"pct": 1.05, "islem": 2, "equity_now": 630.0},
          "r1": {"pct": 1.03, "islem": 0, "equity_now": 72.0}}
    eg = {"yzn1": -0.498, "v7hizli": -0.495, "r1": 0.0}
    # r1: islem=0 VE kasa<150: elenir; kimse pozitif egimli degil ->
    # seviye lideri yzn1 secilir (eski hata: r1 seciliyordu)
    assert osec.aday_sec(sk, "v7ht", min_islem=1, egimler=eg, esik=1.0) == "yzn1"
    # pozitif egimli varsa o kazanir (kullanici ornegi korunuyor)
    sk2 = {"a": {"pct": 1.7, "islem": 2, "equity_now": 500.0},
           "b": {"pct": 1.5, "islem": 2, "equity_now": 500.0}}
    assert osec.aday_sec(sk2, "x", min_islem=1, esik=1.0,
                         egimler={"a": -0.1, "b": 0.3}) == "b"


def test_firsat_sarti(tmp_path):
    """24 Tem: taze girisi olmayan motora gecis atlanir."""
    import hibrit_trader.jsonl_onbellek as jo
    jo._ONBELLEK.clear()
    now = time.time()
    _kur(tmp_path, "r2",
         trades=[{"ts": now - 300, "trade_id": "P-%d" % (now - 400),
                  "pnl_usd": 2.0}])
    var, yas = osec.firsat_var("r2", dk=10)
    assert var and yas < 600                    # 400 sn once giris: taze
    jo._ONBELLEK.clear()
    _kur(tmp_path, "yz",
         trades=[{"ts": now - 3000, "trade_id": "E-%d" % (now - 3100),
                  "pnl_usd": 1.0}])
    var, yas = osec.firsat_var("yz", dk=10)
    assert not var and yas > 600                # 52 dk once: bayat
    # acik pozisyon da firsat sayilir
    st = json.loads((tmp_path / "yz_state.json").read_text())
    st["positions"] = [{"opened_ts": now - 120}]
    (tmp_path / "yz_state.json").write_text(json.dumps(st))
    var, yas = osec.firsat_var("yz", dk=10)
    assert var
