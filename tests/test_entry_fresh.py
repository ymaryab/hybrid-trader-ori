"""Giris taze-fiyat teyidi testleri: uc dal + fail-open + esik sinirlari + recheck."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.entry_fresh as ef
import hibrit_trader.v6_session as v6
import hibrit_trader.v7_session as v7
import hibrit_trader.x1_session as x1
from hibrit_trader.fast_price import FastPriceFeed
from hibrit_trader.momentum_session import REJECTS_FILE
from hibrit_trader.v6_session import V6Engine
from hibrit_trader.v7_session import V7Engine
from hibrit_trader.x1_session import X1Engine


@pytest.fixture(autouse=True)
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr(ef, "_watch", {})
    # gercek recheck thread'i testte acilmasin
    monkeypatch.setattr(ef, "_start_recheck_thread", lambda: None)
    return tmp_path


def _pair(pool="FP1", token="FT1", price=1.0, liq=150_000.0, h1=15.0, m5=1.0):
    return SimpleNamespace(
        name="F / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _feed(pool, price, age=0.5):
    f = FastPriceFeed()
    f._prices[pool] = (price, time.time() - age)
    return f


def _rejects(tmp_path):
    p = tmp_path / REJECTS_FILE
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


# ---- uc dal: kacti-iptal / dustu-taze / normal ------------------------------------

def test_kacti_iptal_ve_reject_kaydi(fresh_env, monkeypatch):
    pair = _pair()
    monkeypatch.setattr(ef, "get_feed", lambda: _feed("FP1", 1.021))
    s = ef.taze_teyit(pair, "V6")
    assert s.iptal is True
    assert s.kaynak == "fast"
    assert s.fark_pct == pytest.approx(2.1)
    rows = _rejects(fresh_env)
    assert len(rows) == 1
    r = rows[0]
    assert r["type"] == "reject"
    assert r["reason"] == "taze_fiyat_kacti"
    assert r["engine"] == "V6"
    assert r["price_usd"] == 1.0
    assert r["fresh_price"] == pytest.approx(1.021)
    # recheck kuyruguna girdi
    assert "FP1" in ef._watch
    assert ef._watch["FP1"]["price_at_reject"] == pytest.approx(1.021)


def test_dustu_taze_fiyattan_giris(fresh_env, monkeypatch):
    monkeypatch.setattr(ef, "get_feed", lambda: _feed("FP1", 0.90))
    s = ef.taze_teyit(_pair(), "V7")
    assert s.iptal is False
    assert s.fiyat == pytest.approx(0.90)
    assert s.kaynak == "fast"
    assert s.fark_pct == pytest.approx(-10.0)
    assert _rejects(fresh_env) == []


def test_normal_arada_taze_kullanilir(fresh_env, monkeypatch):
    monkeypatch.setattr(ef, "get_feed", lambda: _feed("FP1", 1.01))
    s = ef.taze_teyit(_pair(), "X1")
    assert s.iptal is False
    assert s.fiyat == pytest.approx(1.01)
    assert s.fark_pct == pytest.approx(1.0)


# ---- esik sinirlari ----------------------------------------------------------------

def test_esik_tam_sinirda_iptal_yok(fresh_env, monkeypatch):
    # tam +%2: "fazla" degil, giris taze fiyattan devam eder
    monkeypatch.setattr(ef, "get_feed", lambda: _feed("FP1", 1.02))
    s = ef.taze_teyit(_pair(), "V6")
    assert s.iptal is False
    assert s.fark_pct == pytest.approx(2.0)


def test_esik_hemen_ustu_iptal(fresh_env, monkeypatch):
    monkeypatch.setattr(ef, "get_feed", lambda: _feed("FP1", 1.0201))
    s = ef.taze_teyit(_pair(), "V6")
    assert s.iptal is True


def test_esik_env_ile_degisir(fresh_env, monkeypatch):
    monkeypatch.setattr(ef, "FRESH_MAX_PCT", 5.0)
    monkeypatch.setattr(ef, "get_feed", lambda: _feed("FP1", 1.03))
    s = ef.taze_teyit(_pair(), "V6")
    assert s.iptal is False
    assert s.fiyat == pytest.approx(1.03)


# ---- kaynak zinciri: fast -> fetch -> scan (fail-open) -----------------------------

def test_fail_open_kaynak_yokken_tarama_fiyati(fresh_env, monkeypatch):
    monkeypatch.setattr(ef, "get_feed", lambda: None)
    s = ef.taze_teyit(_pair(), "V6", client=None)
    assert s.iptal is False
    assert s.kaynak == "scan"
    assert s.fiyat == 1.0
    assert s.fark_pct is None


def test_fail_open_fetch_hatasinda(fresh_env, monkeypatch):
    monkeypatch.setattr(ef, "get_feed", lambda: None)

    def _boom(client, chain, pool):
        raise RuntimeError("network yok")

    monkeypatch.setattr(ef, "fetch_pool_price", _boom)
    s = ef.taze_teyit(_pair(), "V6", client=SimpleNamespace())
    assert s.iptal is False
    assert s.kaynak == "scan"


def test_fetch_fallback_feed_yokken(fresh_env, monkeypatch):
    monkeypatch.setattr(ef, "get_feed", lambda: None)
    monkeypatch.setattr(ef, "fetch_pool_price", lambda c, ch, p: 1.005)
    s = ef.taze_teyit(_pair(), "V6", client=SimpleNamespace())
    assert s.kaynak == "fetch"
    assert s.fiyat == pytest.approx(1.005)


def test_bayat_fast_kaydi_fetch_e_duser(fresh_env, monkeypatch):
    # feed kaydi 3sn'den eski: fast sayilmaz, fetch devreye girer
    monkeypatch.setattr(ef, "get_feed", lambda: _feed("FP1", 1.5, age=10.0))
    monkeypatch.setattr(ef, "fetch_pool_price", lambda c, ch, p: 1.001)
    s = ef.taze_teyit(_pair(), "V6", client=SimpleNamespace())
    assert s.kaynak == "fetch"
    assert s.fiyat == pytest.approx(1.001)


# ---- recheck kuyrugu: kacirilan olculur ---------------------------------------------

def test_recheck_tick_suresi_gelince_fiyatlar(fresh_env, monkeypatch):
    monkeypatch.setattr(ef, "get_feed", lambda: _feed("FP1", 1.05))
    ef.taze_teyit(_pair(), "V6")
    assert "FP1" in ef._watch
    monkeypatch.setattr(ef, "fetch_pool_price", lambda c, ch, p: 1.575)
    ef._recheck_tick(SimpleNamespace(), now=time.time() + 31 * 60)
    rows = _rejects(fresh_env)
    assert rows[-1]["type"] == "recheck_30m"
    assert rows[-1]["reason"] == "taze_fiyat_kacti"
    assert rows[-1]["chg_30m_pct"] == pytest.approx(50.0)
    assert ef._watch == {}


def test_recheck_tick_suresi_gelmeden_dokunmaz(fresh_env, monkeypatch):
    monkeypatch.setattr(ef, "get_feed", lambda: _feed("FP1", 1.05))
    ef.taze_teyit(_pair(), "V6")
    ef._recheck_tick(SimpleNamespace(), now=time.time() + 60)
    assert "FP1" in ef._watch
    assert len(_rejects(fresh_env)) == 1  # sadece ilk reject satiri


# ---- motor entegrasyonu: v6/v7/x1 giris yolu ----------------------------------------

def _settings():
    return SimpleNamespace(scan_chains=("solana",))


@pytest.mark.parametrize("mod,eng_cls,motor", [
    (v6, V6Engine, "V6"), (v7, V7Engine, "V7"), (x1, X1Engine, "X1"),
])
def test_motor_iptal_pozisyon_acmaz(fresh_env, monkeypatch, mod, eng_cls, motor):
    eng = eng_cls(_settings())
    monkeypatch.setattr(
        mod, "taze_teyit",
        lambda pair, m, client=None: ef.TazeSonuc(1.03, "fast", 3.0, True),
    )
    assert eng._open_position(_pair(), 100.0, sol_h1=0.77) is False
    assert eng.positions == []
    assert eng.balance == 1000.0  # bakiyeye dokunulmadi


@pytest.mark.parametrize("mod,eng_cls,motor", [
    (v6, V6Engine, "V6"), (v7, V7Engine, "V7"), (x1, X1Engine, "X1"),
])
def test_motor_taze_fiyat_ve_kaynak_kaydi(fresh_env, monkeypatch, mod, eng_cls, motor):
    eng = eng_cls(_settings())
    monkeypatch.setattr(
        mod, "taze_teyit",
        lambda pair, m, client=None: ef.TazeSonuc(0.95, "fast", -5.0, False),
    )
    assert eng._open_position(_pair(), 100.0, sol_h1=0.77)
    pos = eng.positions[0]
    assert pos["entry_price_source"] == "fast"
    assert pos["entry_fresh_fark_pct"] == -5.0
    # giris taze fiyattan: 0.95 * (1 + slip), tarama 1.0 degil
    assert pos["entry_price"] < 1.0


def test_motor_fail_open_scan_kaynagi(fresh_env, monkeypatch):
    # client yok + feed kapali: fail-open, giris tarama fiyatindan
    eng = V6Engine(_settings())
    assert eng._open_position(_pair(), 100.0, sol_h1=0.77)
    pos = eng.positions[0]
    assert pos["entry_price_source"] == "scan"
    assert pos["entry_fresh_fark_pct"] is None


def test_motor_trade_satirina_yazilir(fresh_env, monkeypatch):
    eng = V6Engine(_settings())
    monkeypatch.setattr(
        v6, "taze_teyit",
        lambda pair, m, client=None: ef.TazeSonuc(1.01, "fetch", 1.0, False),
    )
    assert eng._open_position(_pair(), 100.0, sol_h1=0.77)
    pos = eng.positions[0]
    monkeypatch.setattr(v6, "fetch_pool_snapshot", lambda c, ch, p: (pos["entry_price"] * 1.03, None))
    monkeypatch.setattr(v6.time, "time", lambda: pos["opened_ts"] + 60)
    eng._manage_exits(client=SimpleNamespace())
    row = json.loads((fresh_env / v6.TRADES_FILE).read_text().splitlines()[-1])
    assert row["entry_price_source"] == "fetch"
    assert row["entry_fresh_fark_pct"] == 1.0


# ---- rejim_reject enstrumantasyonu ---------------------------------------------------

def test_rejim_reject_kaydi_ve_kuyruk(fresh_env):
    ef.rejim_reject_kaydet([_pair(price=1.5)], "V6", 0.12)
    rows = _rejects(fresh_env)
    assert len(rows) == 1
    r = rows[0]
    assert r["type"] == "reject"
    assert r["reason"] == "rejim_reject"
    assert r["engine"] == "V6"
    assert r["sol_chg_h1"] == 0.12
    assert r["price_usd"] == 1.5
    assert "FP1" in ef._watch
    assert ef._watch["FP1"]["reason"] == "rejim_reject"
    assert ef._watch["FP1"]["price_at_reject"] == 1.5


def test_rejim_veri_yok_ayri_etiket_sol_h1_none(fresh_env):
    # M1 paterni: veri-yok (fail-closed) negatif-rejimden ayri nedende
    ef.rejim_reject_kaydet([_pair()], "V7", None)
    rows = _rejects(fresh_env)
    assert rows[0]["reason"] == "rejim_veri_yok"
    assert rows[0]["sol_chg_h1"] is None
    assert rows[0]["engine"] == "V7"
    assert ef._watch["FP1"]["reason"] == "rejim_veri_yok"


def test_rejim_reject_kuyruktayken_tekrar_yazilmaz(fresh_env):
    ef.rejim_reject_kaydet([_pair()], "V6", 0.1)
    ef.rejim_reject_kaydet([_pair()], "V6", 0.2)
    assert len(_rejects(fresh_env)) == 1  # dedup: ayni havuz kuyruktayken atlanir


def test_rejim_reject_dedupe_motor_boyutlu(fresh_env):
    # Ayni havuz: her motor kendi kaydini yazar, kuyrukta TEK giris kalir
    ef.rejim_reject_kaydet([_pair()], "V6", 0.1)
    ef.rejim_reject_kaydet([_pair()], "V7", 0.1)
    ef.rejim_reject_kaydet([_pair()], "V7", 0.2)  # ayni motor tekrar: yazilmaz
    rows = _rejects(fresh_env)
    assert [r["engine"] for r in rows] == ["V6", "V7"]
    assert len(ef._watch) == 1
    assert ef._watch["FP1"]["engines"] == {"V6", "V7"}
    assert ef._watch["FP1"]["engine"] == "V6"  # recheck satiri ilk motora yazilir


def test_rejim_reject_taze_kuyrugundaki_havuza_da_motor_kaydi(fresh_env, monkeypatch):
    # Havuz taze_fiyat_kacti ile kuyruktayken rejim reddi gelirse kayit yazilir
    monkeypatch.setattr(ef, "get_feed", lambda: _feed("FP1", 1.05))
    ef.taze_teyit(_pair(), "V6")
    ef.rejim_reject_kaydet([_pair()], "V7", 0.1)
    rows = _rejects(fresh_env)
    assert rows[-1]["reason"] == "rejim_reject"
    assert rows[-1]["engine"] == "V7"
    assert len(ef._watch) == 1  # kuyruk sismedi


# ---- safety reject kaydi: sessiz continue yerine olcum ------------------------------

def test_safety_reject_kaydi_red(fresh_env):
    ef.safety_reject_kaydet(_pair(), "V7", "safety_red", "honeypot; mint yetkisi açık")
    rows = _rejects(fresh_env)
    assert len(rows) == 1
    r = rows[0]
    assert r["type"] == "reject"
    assert r["reason"] == "safety_red"
    assert r["engine"] == "V7"
    assert r["detay"] == "honeypot; mint yetkisi açık"
    assert ef._watch == {}  # recheck kuyruguna girmez


def test_safety_reject_kaydi_hata(fresh_env):
    ef.safety_reject_kaydet(_pair(), "X1", "safety_hata", "ReadTimeout")
    r = _rejects(fresh_env)[0]
    assert r["reason"] == "safety_hata"
    assert r["engine"] == "X1"
    assert r["detay"] == "ReadTimeout"


@pytest.mark.parametrize("mod,eng_cls,motor,pair_kw", [
    (v6, V6Engine, "V6", {}),
    (v7, V7Engine, "V7", {}),
    (x1, X1Engine, "X1", {"h1": 80.0, "m5": 5.0, "liq": 60_000.0}),
])
def test_motor_safety_red_ve_hata_kaydi(fresh_env, monkeypatch, mod, eng_cls, motor, pair_kw):
    eng = eng_cls(_settings())
    monkeypatch.setattr(mod, "scan_all", lambda chains: [_pair(**pair_kw)])
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: 1.0)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    def _boom(client, chain, token):
        raise RuntimeError("api")

    monkeypatch.setattr(mod, "check_token", _boom)
    eng._enter(client=SimpleNamespace())
    assert eng.positions == []

    monkeypatch.setattr(
        mod, "check_token",
        lambda client, chain, token: SimpleNamespace(ok=False, reasons=["honeypot"], kapi=""),
    )
    eng._enter(client=SimpleNamespace())
    assert eng.positions == []

    monkeypatch.setattr(
        mod, "check_token",
        lambda client, chain, token: SimpleNamespace(
            ok=False, reasons=["holder verisi alinamadi: ConnectError"], kapi="holder_hata",
        ),
    )
    eng._enter(client=SimpleNamespace())
    assert eng.positions == []

    rows = [
        r for r in _rejects(fresh_env)
        if r["reason"].startswith("safety_") or r["reason"] == "holder_hata"
    ]
    assert [(r["reason"], r["engine"]) for r in rows] == [
        ("safety_hata", motor), ("safety_red", motor), ("holder_hata", motor),
    ]
    assert rows[0]["detay"] == "RuntimeError"
    assert rows[1]["detay"] == "honeypot"
    assert rows[2]["detay"] == "holder verisi alinamadi: ConnectError"


def test_rejim_reject_tick_basina_en_cok_5(fresh_env):
    pairs = [_pair(pool=f"P{i}", token=f"T{i}") for i in range(8)]
    ef.rejim_reject_kaydet(pairs, "V6", 0.1)
    assert len(_rejects(fresh_env)) == 5
    assert len(ef._watch) == 5


def test_rejim_reject_recheck_satiri(fresh_env, monkeypatch):
    ef.rejim_reject_kaydet([_pair(price=1.0)], "V6", 0.1)
    monkeypatch.setattr(ef, "fetch_pool_price", lambda c, ch, p: 1.25)
    ef._recheck_tick(SimpleNamespace(), now=time.time() + 31 * 60)
    rows = _rejects(fresh_env)
    assert rows[-1]["type"] == "recheck_30m"
    assert rows[-1]["reason"] == "rejim_reject"
    assert rows[-1]["chg_30m_pct"] == pytest.approx(25.0)
    assert ef._watch == {}


# ---- HuniSayac: gunluk giris-filtre hunisi -------------------------------------------

def test_huni_sayac_gun_donunce_ozet_loglar(caplog):
    h = ef.HuniSayac("V6")
    t0 = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1))
    h.ekle(30, 3, 1, now=t0)
    h.ekle(25, 2, 0, now=t0 + 60)
    with caplog.at_level("WARNING"):
        h.ekle(10, 1, 1, now=t0 + 86400)  # gun dondu: onceki gun ozeti loglanir
    assert any(
        "V6 HUNI OZET 2026-07-10: tick=2 aday=55 liq_ok=5 h1_band_ok=1" in m
        for m in caplog.messages
    )


def test_huni_sayac_ilk_gun_ozet_yok(caplog):
    h = ef.HuniSayac("V7")
    with caplog.at_level("WARNING"):
        h.ekle(5, 1, 0)
    assert not any("HUNI OZET" in m for m in caplog.messages)


def test_x1_yarim_satis_satirina_da_yazilir(fresh_env, monkeypatch):
    eng = X1Engine(_settings())
    monkeypatch.setattr(
        x1, "taze_teyit",
        lambda pair, m, client=None: ef.TazeSonuc(1.0, "fast", 0.5, False),
    )
    assert eng._open_position(_pair(h1=80.0, m5=5.0, liq=60_000.0), 60.0, sol_h1=0.5)
    pos = eng.positions[0]
    monkeypatch.setattr(x1, "fetch_pool_snapshot", lambda c, ch, p: (pos["entry_price"] * 1.20, None))
    monkeypatch.setattr(x1.time, "time", lambda: pos["opened_ts"] + 60)
    eng._manage_exits(client=SimpleNamespace())
    rows = [json.loads(x) for x in (fresh_env / x1.TRADES_FILE).read_text().splitlines()]
    yarim = [r for r in rows if r["exit_reason"] == "tp_yarim_15"]
    assert yarim and yarim[0]["entry_price_source"] == "fast"
    assert yarim[0]["entry_fresh_fark_pct"] == 0.5
