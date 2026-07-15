"""V7D SECICI paper motoru testleri: h1 10-20 dar + m5>0 zorunlu + tp+2.5 + felaket -15."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import hibrit_trader.v7d_session as v7d
from hibrit_trader.broker import PaperExecBroker
from hibrit_trader.v7d_session import V7DEngine


@pytest.fixture(autouse=True)
def v7d_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr("hibrit_trader.entry_fresh._watch", {})
    monkeypatch.setattr("hibrit_trader.entry_fresh._start_recheck_thread", lambda: None)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="DP1", token="DT1", price=1.0, liq=200_000.0, h1=12.0, m5=1.0):
    return SimpleNamespace(
        name="D / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(v7d, "fetch_pool_snapshot", lambda c, ch, p: (price, None))
    monkeypatch.setattr(v7d.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / v7d.TRADES_FILE).read_text().splitlines()[-1])


# ---- Sabitler: SECICI konfigi -----------------------------------------------

def test_secici_sabitleri(v7d_data_dir):
    assert v7d.TP_PCT == 2.5
    assert v7d.DISASTER_PCT == -15.0
    assert v7d.LATE_STOP_PCT == -2.0
    assert v7d.GRACE_SEC == 15 * 60
    assert v7d.CEILING_SEC == 20 * 60
    assert v7d.SOL_H1_MIN == 0.5
    assert v7d.CHG_H1_MIN == 10.0
    assert v7d.CHG_H1_MAX == 20.0
    assert v7d.LIQ_MIN_USD == 150_000.0
    assert v7d.H1_SKIP_LO == 10.0
    assert v7d.H1_SKIP_HI == 20.0
    assert v7d.H1_SKIP_M5_KOSUL is True


# ---- SABIT PAPER: BROKER_MODE bagimsiz -------------------------------------

def test_exec_paper_kalir_brokermode_live_iken(v7d_data_dir, monkeypatch):
    monkeypatch.setenv("BROKER_MODE", "live")
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    eng = V7DEngine(_settings())
    assert isinstance(eng._exec, PaperExecBroker)
    assert eng._exec.mode == "paper"
    pos = _open(eng)
    assert "tx_al" not in pos and "canli_miktar" not in pos


# ---- h1 bant skip: m5>0 zorunlu (10-20 bandi tumu) --------------------------

def test_h1_bant_atla_m5_pozitif_gecer():
    assert v7d.h1_bant_atla(12.0, 1.5) is False   # bant ici m5+ -> gecer
    assert v7d.h1_bant_atla(19.0, 0.1) is False   # bant ici m5+ -> gecer


def test_h1_bant_atla_m5_sifir_veya_negatif_elenir():
    assert v7d.h1_bant_atla(12.0, 0.0) is True   # m5<=0 -> atla
    assert v7d.h1_bant_atla(12.0, -1.0) is True  # m5- -> atla
    assert v7d.h1_bant_atla(12.0, None) is True  # m5 yok -> atla (fail-closed)


def test_h1_bant_disi_dokunmaz():
    # bant DISINDA (h1<10 veya h1>20) hicbir zaman skip; zaten CHG_H1_MIN/MAX filtresine takilir
    assert v7d.h1_bant_atla(9.0, 1.0) is False
    assert v7d.h1_bant_atla(25.0, 1.0) is False


# ---- Cikis: tp 2.5 -----------------------------------------------------------

def test_tp_esik_ustu_satar(v7d_data_dir, monkeypatch):
    eng = V7DEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.026, pos["opened_ts"] + 60, monkeypatch)
    assert eng.positions == []
    assert _last(v7d_data_dir)["exit_reason"] == "tp_2"


def test_tp_esik_alti_satmaz(v7d_data_dir, monkeypatch):
    eng = V7DEngine(_settings())
    pos = _open(eng)
    # +2.4% tp esigi altinda, satmaz
    _tick_price(eng, pos, pos["entry_price"] * 1.024, pos["opened_ts"] + 60, monkeypatch)
    assert len(eng.positions) == 1


# ---- Felaket -15 -------------------------------------------------------------

def test_felaket_grace_icinde_bile_satar(v7d_data_dir, monkeypatch):
    eng = V7DEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.80, pos["opened_ts"] + 60, monkeypatch)
    assert eng.positions == []
    assert _last(v7d_data_dir)["exit_reason"] == "stop_felaket"


def test_felaket_esigi_ustu_satmaz(v7d_data_dir, monkeypatch):
    eng = V7DEngine(_settings())
    pos = _open(eng)
    # -14% grace icinde, felaket alti -> acik kalir
    _tick_price(eng, pos, pos["entry_price"] * 0.86, pos["opened_ts"] + 60, monkeypatch)
    assert len(eng.positions) == 1


# ---- Gec stop + timeout ------------------------------------------------------

def test_stop_gec_15dk_sonra_eksi_2(v7d_data_dir, monkeypatch):
    eng = V7DEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.97, pos["opened_ts"] + v7d.GRACE_SEC + 1, monkeypatch)
    assert _last(v7d_data_dir)["exit_reason"] == "stop_gec"


def test_timeout_20dk(v7d_data_dir, monkeypatch):
    eng = V7DEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.005, pos["opened_ts"] + v7d.CEILING_SEC + 1, monkeypatch)
    assert _last(v7d_data_dir)["exit_reason"] == "timeout_20"


# ---- Dosya izolasyonu -------------------------------------------------------

def test_kendi_dosyalarina_yazar(v7d_data_dir, monkeypatch):
    eng = V7DEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    assert (v7d_data_dir / "v7d_trades.jsonl").exists()
    assert (v7d_data_dir / "v7d_state.json").exists()
    # v7 dosyalarina KESINLIKLE dokunmaz
    assert not (v7d_data_dir / "v7_trades.jsonl").exists()
    assert not (v7d_data_dir / "v7c_trades.jsonl").exists()
