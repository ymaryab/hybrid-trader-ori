"""Golge defter (rejim disi) testleri: kovalar, filtre, sanal v7 kurallari, ozet."""

from __future__ import annotations

import json
import time

import pytest

import hibrit_trader.golge_rejim_disi as gr
from hibrit_trader.golge_rejim_disi import GolgeDefter, esik_kovalari, satir_uygun


@pytest.fixture(autouse=True)
def gr_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    return tmp_path


def _row(sol_h1=0.38, ts=None, **kw):
    r = {
        "type": "reject", "reason": "rejim_reject", "engine": "V7",
        "pair": "ABC / SOL", "chain": "solana",
        "pool_address": "POOL1", "token_address": "TOK1",
        "sol_chg_h1": sol_h1, "ts": time.time() if ts is None else ts,
    }
    r.update(kw)
    return r


# ---- esik kovalari -------------------------------------------------------------

def test_esik_kovalari_ornek_038():
    # sol_h1 0.38: 0.2 ve 0.35 esiklerinde girerdi, 0.4'te girmezdi
    assert esik_kovalari(0.38) == [0.2, 0.35]


def test_esik_kovalari_sinirlar():
    assert esik_kovalari(0.15) == []
    assert esik_kovalari(0.2) == [0.2]
    assert esik_kovalari(0.45) == [0.2, 0.35, 0.4]
    assert esik_kovalari(None) == []


# ---- satir filtresi ------------------------------------------------------------

def test_satir_uygun_yalniz_v7_rejim_reject():
    now = time.time()
    assert satir_uygun(_row(), now) is True
    assert satir_uygun(_row(reason="taze_fiyat_kacti"), now) is False
    assert satir_uygun(_row(engine="V6"), now) is False
    assert satir_uygun(_row(type="recheck_30m"), now) is False


def test_satir_uygun_kovasiz_ve_eski_satir_atlanir():
    now = time.time()
    assert satir_uygun(_row(sol_h1=0.1), now) is False   # hicbir kovaya girmez
    assert satir_uygun(_row(sol_h1=None), now) is False  # rejim_veri_yok esdegeri
    assert satir_uygun(_row(ts=now - gr.SATIR_MAX_YAS_SEC - 5), now) is False


# ---- defter: acilis kurallari ---------------------------------------------------

def test_aday_ekle_ve_cooldown():
    d = GolgeDefter()
    now = time.time()
    pos = d.aday_ekle(_row(), 1.0, 35.0, now)
    assert pos is not None and pos["esik_kovalar"] == [0.2, 0.35]
    assert pos["bilet_usd"] == 35.0
    # ayni token tekrar: acikken de, kapandiktan sonra 60dk boyunca da eklenmez
    assert d.aday_ekle(_row(), 1.0, 35.0, now + 10) is None
    d.acik.clear()
    assert d.aday_ekle(_row(), 1.0, 35.0, now + 100) is None
    assert d.aday_ekle(_row(), 1.0, 35.0, now + gr.COOLDOWN_SEC + 1) is not None


def test_aday_ekle_tavan_ve_gecersiz_fiyat():
    d = GolgeDefter()
    now = time.time()
    for i in range(gr.MAX_ACIK):
        assert d.aday_ekle(_row(token_address=f"T{i}", pool_address=f"P{i}"),
                           1.0, None, now) is not None
    assert d.aday_ekle(_row(token_address="TX", pool_address="PX"), 1.0, None, now) is None
    d2 = GolgeDefter()
    assert d2.aday_ekle(_row(), 0.0, None, now) is None


# ---- sanal v7 kurallari ----------------------------------------------------------

def _tek_poz(d: GolgeDefter, now: float, **kw):
    return d.aday_ekle(_row(**kw), 1.0, 40.0, now)


def test_tick_tp_2():
    d = GolgeDefter()
    now = time.time()
    _tek_poz(d, now)
    rows = d.tick({"POOL1": 1.021}, now + 60)
    assert len(rows) == 1 and rows[0]["sonuc"] == "tp_2"
    assert rows[0]["pnl_pct"] == pytest.approx(2.1)
    assert rows[0]["esik_kovalar"] == [0.2, 0.35]
    assert rows[0]["tavan_pct"] == pytest.approx(2.1)
    assert d.acik == []


def test_tick_grace_icinde_stop_yok():
    d = GolgeDefter()
    now = time.time()
    _tek_poz(d, now)
    # 10. dk -%3: sabir penceresi, kapanis yok
    assert d.tick({"POOL1": 0.97}, now + 600) == []
    # 14 Tem: fren iptal; grace icinde -%11 bile kapanmaz
    assert d.tick({"POOL1": 0.89}, now + 620) == []


def test_tick_stop_gec_ve_tavan():
    d = GolgeDefter()
    now = time.time()
    _tek_poz(d, now)
    rows = d.tick({"POOL1": 0.975}, now + gr.GRACE_SEC + 60)
    assert rows[0]["sonuc"] == "stop_gec"
    d2 = GolgeDefter()
    _tek_poz(d2, now)
    rows2 = d2.tick({"POOL1": 1.005}, now + gr.CEILING_SEC + 1)
    assert rows2[0]["sonuc"] == "timeout_120"
    assert rows2[0]["pnl_pct"] == pytest.approx(0.5)


def test_tick_fiyatsiz_pozisyon_son_fiyatla_degerlenir():
    d = GolgeDefter()
    now = time.time()
    _tek_poz(d, now)
    d.tick({"POOL1": 1.015}, now + 60)     # tavan izi guncellenir, kapanmaz
    rows = d.tick({}, now + gr.CEILING_SEC + 1)  # fiyat gelmedi: son fiyat
    assert rows[0]["sonuc"] == "timeout_120"
    assert rows[0]["tavan_pct"] == pytest.approx(1.5)


# ---- bilet ve kayit --------------------------------------------------------------

def test_bilet_usd_canli_equity_son_satir(gr_data_dir):
    (gr_data_dir / "canli_equity.jsonl").write_text(
        json.dumps({"ts": 1.0, "eq": 100.0}) + "\n"
        + json.dumps({"ts": 2.0, "eq": 140.0}) + "\n")
    assert gr.bilet_usd_oku() == 35.0


def test_bilet_usd_dosya_yoksa_none(gr_data_dir):
    assert gr.bilet_usd_oku() is None


def test_kayit_yaz_dosyaya_ekler(gr_data_dir):
    gr._kayit_yaz({"tur": "golge_rejim_disi", "pnl_pct": 1.0})
    gr._kayit_yaz({"tur": "golge_rejim_disi", "pnl_pct": -2.0})
    satirlar = (gr_data_dir / gr.OUTPUT_FILE).read_text().splitlines()
    assert len(satirlar) == 2
    assert json.loads(satirlar[0])["pnl_pct"] == 1.0


# ---- ozet -------------------------------------------------------------------------

def test_ozet_esik_bazli_pnl(gr_data_dir):
    kayitlar = [
        {"esik_kovalar": [0.2, 0.35], "pnl_pct": 2.0, "bilet_usd": 100.0, "sonuc": "tp_2"},
        {"esik_kovalar": [0.2], "pnl_pct": -2.0, "bilet_usd": 100.0, "sonuc": "stop_gec"},
        {"esik_kovalar": [0.2, 0.35, 0.4], "pnl_pct": 1.0, "bilet_usd": None, "sonuc": "timeout_120"},
    ]
    p = gr_data_dir / gr.OUTPUT_FILE
    p.write_text("".join(json.dumps(k) + "\n" for k in kayitlar))
    s = gr.ozet()
    assert "3 kayit" in s
    # esik 0.2: uc kayit, toplam +1.0%, usd yalniz biletli iki kayittan (+2-2=0)
    assert "esik 0.2: n 3" in s and "toplam pnl +1.00% (~$+0.00 biletle)" in s
    assert "esik 0.35: n 2" in s
    assert "esik 0.4: n 1" in s and "win 1/1" in s


def test_ozet_kayit_yoksa(gr_data_dir):
    assert "kayit yok" in gr.ozet()
