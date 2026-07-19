"""CANLI 10. motor testleri: R1 kural mirasi + kural_degisim + defter izolasyonu."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import hibrit_trader.canli_session as canli
from hibrit_trader.broker import PaperExecBroker
from hibrit_trader.canli_session import CanliEngine


@pytest.fixture(autouse=True)
def canli_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    monkeypatch.setattr("hibrit_trader.entry_fresh._watch", {})
    monkeypatch.setattr("hibrit_trader.entry_fresh._start_recheck_thread", lambda: None)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


# ---- Kural seti R1'den mirasla dogru geldi ---------------------------------


def test_kaynak_r1_sabitleri_devrali(canli_data_dir):
    import hibrit_trader.r1_session as r1
    assert canli.KAYNAK_MOTOR == "r1"
    assert canli.TP_PCT == r1.TP_PCT
    assert canli.DISASTER_PCT == r1.DISASTER_PCT
    assert canli.GRACE_SEC == r1.GRACE_SEC
    assert canli.CEILING_SEC == r1.CEILING_SEC
    assert canli.KISMI_ORAN1 == r1.KISMI_ORAN1
    assert canli.TRAIL_PCT == r1.TRAIL_PCT
    assert canli.MAX_SLOTS == r1.MAX_SLOTS


def test_desteklenmeyen_kaynak_fail_fast(monkeypatch):
    # Modul yeniden import edildiginde env yanlissa RuntimeError firlatir.
    monkeypatch.setenv("CANLI_KAYNAK_MOTOR", "v7hizli")
    import importlib
    import hibrit_trader.canli_session
    with pytest.raises(RuntimeError, match="destekli degil"):
        importlib.reload(hibrit_trader.canli_session)
    # Modulu geri getir (diger testleri kirmasin)
    monkeypatch.setenv("CANLI_KAYNAK_MOTOR", "r1")
    importlib.reload(hibrit_trader.canli_session)


# ---- Broker dispatch: CANLI_MOTOR=canli ise live, aksi halde paper ---------


def test_broker_default_paper(canli_data_dir, monkeypatch):
    # BROKER_MODE unset (paper) -> CANLI motoru da paper alir
    monkeypatch.delenv("CANLI_MOTOR", raising=False)
    monkeypatch.delenv("BROKER_MODE", raising=False)
    eng = CanliEngine(_settings())
    assert isinstance(eng._exec, PaperExecBroker)
    assert eng._exec.mode == "paper"


def test_broker_dispatch_canli_secildiginde_live(canli_data_dir, monkeypatch):
    # CANLI_MOTOR=canli + BROKER_MODE=live + kilit acik -> LiveExecBroker
    monkeypatch.setenv("CANLI_MOTOR", "canli")
    monkeypatch.setenv("BROKER_MODE", "live")
    monkeypatch.setattr("hibrit_trader.broker.live_kilit_acik", lambda: True)
    # RPC/httpx init'i devreye girmesin
    monkeypatch.setattr("hibrit_trader.broker.LiveExecBroker.__init__",
                        lambda self, http=None: setattr(self, "mode", "live"))
    eng = CanliEngine(_settings())
    assert eng._exec.mode == "live"


def test_broker_dispatch_baska_motor_secildiginde_paper(canli_data_dir, monkeypatch):
    # CANLI_MOTOR=r1 iken bile CANLI motoru PaperExec alir (kilit r1'de)
    monkeypatch.setenv("CANLI_MOTOR", "r1")
    monkeypatch.setenv("BROKER_MODE", "live")
    eng = CanliEngine(_settings())
    assert isinstance(eng._exec, PaperExecBroker)


# ---- Kural degisim protokolu -----------------------------------------------


def test_kural_kontrol_ilk_baslangic_kayit_atmaz(canli_data_dir):
    # Defter bostaysa VE kayitli kaynak yoksa: ilk kaynak kaydi atilir
    eng = CanliEngine(_settings())
    eng._kural_kontrol()
    trades = (canli_data_dir / canli.TRADES_FILE)
    assert trades.exists()
    lines = [json.loads(ln) for ln in trades.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["type"] == "kural_degisim"
    assert lines[0]["eski"] is None
    assert lines[0]["yeni"] == "r1"


def test_kural_kontrol_ayni_kaynak_no_op(canli_data_dir):
    eng = CanliEngine(_settings())
    eng._kural_kontrol()  # 1. cagri: ilk_baslangic kaydi
    eng._kural_kontrol()  # 2. cagri: ayni kaynak, tekrar yazmaz
    trades = canli_data_dir / canli.TRADES_FILE
    lines = [ln for ln in trades.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


def test_kural_kontrol_kaynak_degisince_satir_yazar(canli_data_dir, monkeypatch):
    eng = CanliEngine(_settings())
    eng._kural_kontrol()
    # Kaynak motor degisimini simule et
    eng.kaynak_motor = "v7hizli"
    eng._kural_kontrol()
    trades = canli_data_dir / canli.TRADES_FILE
    rows = [json.loads(ln) for ln in trades.read_text().splitlines() if ln.strip()]
    assert len(rows) == 2
    assert rows[1]["eski"] == "r1"
    assert rows[1]["yeni"] == "v7hizli"


def test_restore_day_realized_kural_degisim_satirini_atlar(canli_data_dir):
    # Kural degisim satirinda pnl_usd yok — restore _day_realized'i bozmamali
    eng = CanliEngine(_settings())
    eng._append_trade({"type": "kural_degisim", "eski": None, "yeni": "r1"})
    eng._append_trade({"pnl_usd": 5.5})
    eng._restore_day_realized()
    # Bugun icinde yazildi (gercek zaman), pnl satiri sayilmali
    assert eng._day_realized == 5.5


# ---- Defter izolasyonu: R1 dosyalarina dokunmaz ----------------------------


def test_defter_r1_dosyalarindan_ayri(canli_data_dir):
    eng = CanliEngine(_settings())
    eng._save()
    eng._append_trade({"pair": "TEST", "pnl_usd": 1.0})
    assert (canli_data_dir / "canli_state.json").exists()
    assert (canli_data_dir / "canli_trades.jsonl").exists()
    assert not (canli_data_dir / "r1_state.json").exists()
    assert not (canli_data_dir / "r1_trades.jsonl").exists()


# ---- Sanal bakiye baslangici -----------------------------------------------


def test_baslangic_bakiye_env_ile_override(canli_data_dir, monkeypatch):
    # Modul global'i env okuyor, reload gerek
    monkeypatch.setenv("CANLI_START_BALANCE", "500")
    import importlib
    importlib.reload(canli)
    eng = canli.CanliEngine(_settings())
    assert eng.balance == 500.0
    # Diger testler icin default'a dondur
    monkeypatch.delenv("CANLI_START_BALANCE")
    importlib.reload(canli)


# ---- Engine lock: baska instance varken baslamaz ---------------------------


def test_lock_ikinci_instance_engellenir(canli_data_dir):
    eng1 = CanliEngine(_settings())
    assert eng1._acquire_lock()
    eng2 = CanliEngine(_settings())
    assert not eng2._acquire_lock()
    eng1._lock_fh.close()  # temizle
