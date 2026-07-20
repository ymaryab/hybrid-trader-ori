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
    monkeypatch.setenv("CANLI_KAYNAK_MOTOR", "v6")
    import importlib
    import hibrit_trader.canli_session
    with pytest.raises(RuntimeError, match="destekli degil"):
        importlib.reload(hibrit_trader.canli_session)
    # Modulu geri getir (diger testleri kirmasin)
    monkeypatch.setenv("CANLI_KAYNAK_MOTOR", "r1")
    importlib.reload(hibrit_trader.canli_session)


# ---- V7HIZLI kaynak (20 Tem): TP+%2 tek cikis, stop yok --------------------


def _v7hizli_reload(monkeypatch):
    import importlib
    import hibrit_trader.canli_session as mod
    monkeypatch.setenv("CANLI_KAYNAK_MOTOR", "v7hizli")
    return importlib.reload(mod)


def _r1_geri_yukle(monkeypatch):
    import importlib
    import hibrit_trader.canli_session as mod
    monkeypatch.setenv("CANLI_KAYNAK_MOTOR", "r1")
    importlib.reload(mod)


def test_kaynak_v7hizli_sabitleri_devrali(canli_data_dir, monkeypatch):
    try:
        mod = _v7hizli_reload(monkeypatch)
        import hibrit_trader.v7hizli_session as v7h
        assert mod.KAYNAK_MOTOR == "v7hizli"
        assert mod.TP_PCT == v7h.TP_PCT
        assert mod.CHG_H1_MIN == v7h.CHG_H1_MIN
        assert mod.LIQ_MIN_USD == v7h.LIQ_MIN_USD
        assert mod.MAX_SLOTS == v7h.MAX_SLOTS
        # R1'e ozgu sabitler v7hizli kaynakta None
        assert mod.DISASTER_PCT is None
        assert mod.M5_MIN is None
        assert mod.TP2_PCT is None
        assert mod.TRAIL_PCT is None
    finally:
        _r1_geri_yukle(monkeypatch)


def test_v7hizli_eval_sadece_tp2(canli_data_dir, monkeypatch):
    # v7hizli kaynakta cikis karari: TP+%2 uzeri tp_2, baska hicbir tetik yok
    try:
        mod = _v7hizli_reload(monkeypatch)
        monkeypatch.setattr(mod, "guard_price",
                            lambda pos, price, now, tag, liquidity_usd=None: (price, False))
        eng = mod.CanliEngine(_settings())
        import time as _t
        now = _t.time()

        def poz(entry=1.0, yas_sec=0.0):
            return {"pair": "T / SOL", "entry_price": entry, "last_price": entry,
                    "opened_ts": now - yas_sec, "mfe_pct": 0.0, "mae_pct": 0.0}

        tp_esik = mod.TP_PCT  # v7hizli varsayilan 2.0
        assert eng._eval_position(poz(), 1.0 * (1 + (tp_esik + 0.5) / 100), now) == "tp_2"
        assert eng._eval_position(poz(), 1.0 * (1 + (tp_esik - 0.5) / 100), now) is None
        # stop/felaket yok: -%50 bile satis tetiklemez
        assert eng._eval_position(poz(), 0.5, now) is None
        # zaman asimi yok: 10 saatlik pozisyon da tetiklenmez
        assert eng._eval_position(poz(yas_sec=36000), 1.0, now) is None
    finally:
        _r1_geri_yukle(monkeypatch)


def test_r1_eval_degismedi(canli_data_dir):
    # regresyon: r1 kaynakta felaket freni calisiyor olmali
    eng = CanliEngine(_settings())
    import time as _t
    now = _t.time()
    pos = {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
           "opened_ts": now, "mfe_pct": 0.0, "mae_pct": 0.0}
    import hibrit_trader.canli_session as mod
    fiyat_felaket = 1.0 * (1 + (mod.DISASTER_PCT - 1) / 100)
    assert eng._eval_position(pos, fiyat_felaket, now) == "stop_felaket"


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


# ---- Ana salter (CANLI_DUR): giris durur, cikis surer ----------------------


def test_ana_salter_girisleri_keser_acinca_serbest(canli_data_dir):
    eng = CanliEngine(_settings())
    assert eng._entries_blocked() is None
    (canli_data_dir / "CANLI_DUR").write_text("test")
    assert eng._entries_blocked() == "canli_pause"
    (canli_data_dir / "CANLI_DUR").unlink()
    assert eng._entries_blocked() is None


def test_ana_salter_cikis_kararini_etkilemez(canli_data_dir):
    # salter kapaliyken bile cikis degerlendirmesi normal calisir
    (canli_data_dir / "CANLI_DUR").write_text("test")
    eng = CanliEngine(_settings())
    import time as _t
    now = _t.time()
    pos = {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
           "opened_ts": now, "mfe_pct": 0.0, "mae_pct": 0.0}
    import hibrit_trader.canli_session as mod
    fiyat_felaket = 1.0 * (1 + (mod.DISASTER_PCT - 1) / 100)
    assert eng._eval_position(pos, fiyat_felaket, now) == "stop_felaket"


def test_canli_pause_api_dosya_ve_telegram(canli_data_dir, monkeypatch):
    import hibrit_trader.panel as panel
    mesajlar = []
    monkeypatch.setattr("hibrit_trader.killswitch.notify",
                        lambda m, **k: mesajlar.append(m))
    r = panel.api_canli_pause_kapat()
    assert r == {"canli_pause": True}
    assert (canli_data_dir / "CANLI_DUR").exists()
    r = panel.api_canli_pause_ac()
    assert r == {"canli_pause": False}
    assert not (canli_data_dir / "CANLI_DUR").exists()
    assert len(mesajlar) == 2 and "SALTER" in mesajlar[0]


def test_canli_poz_broker_live_degilse_sanal_kapanis_yok(canli_data_dir, monkeypatch):
    # 20 Tem HBULL vakasi: LIVE_ONAY dususte fallback paper exec sanal
    # kapanis yapiyordu; artik pozisyon bekletilir, defter dokunulmaz
    uyarilar = []
    monkeypatch.setattr("hibrit_trader.canli_session.kritik_uyari",
                        lambda *a, **k: uyarilar.append(a))
    eng = CanliEngine(_settings())
    import time as _t
    now = _t.time()
    pos = {"trade_id": "t1", "pair": "T / SOL", "chain": "solana",
           "token_address": "TOK", "pool_address": "POOL",
           "entry_price": 1.0, "last_price": 1.1, "amount_token": 10.0,
           "cost_usd": 10.0, "opened_ts": now, "opened_at": "x",
           "chg_m5": 0, "chg_h1": 0, "liq_entry": 1000.0,
           "mfe_pct": 0.0, "mae_pct": 0.0, "canli_miktar": 10.0}
    eng.positions = [pos]
    assert eng._exec.mode == "paper"  # fallback senaryosu
    eng._close_position(pos, 1.1, "tp_2", now)
    assert eng.positions == [pos]  # pozisyon YERINDE
    assert pos.get("_sat_bekle_ts", 0) > now  # cooldown kuruldu
    assert not (canli_data_dir / canli.TRADES_FILE).exists()  # satir yazilmadi
    assert len(uyarilar) == 1


def test_paper_poz_normal_kapanis_calisiyor(canli_data_dir, monkeypatch):
    # canli_miktar olmayan (saf sanal) pozisyon paper modda normal kapanir
    monkeypatch.setattr("hibrit_trader.canli_session.get_feed", lambda: None)
    eng = CanliEngine(_settings())
    import time as _t
    now = _t.time()
    pos = {"trade_id": "t2", "pair": "P / SOL", "chain": "solana",
           "token_address": "TOK2", "pool_address": "POOL2",
           "entry_price": 1.0, "last_price": 1.1, "amount_token": 10.0,
           "cost_usd": 10.0, "opened_ts": now, "opened_at": "x",
           "chg_m5": 0, "chg_h1": 0, "liq_entry": 100000.0,
           "mfe_pct": 0.0, "mae_pct": 0.0}
    eng.positions = [pos]
    eng._close_position(pos, 1.1, "timeout_120", now)
    assert eng.positions == []
    assert (canli_data_dir / canli.TRADES_FILE).exists()


def test_canli_swap_api_dogrulama(canli_data_dir, monkeypatch):
    # desteklenmeyen kaynak 400; acik canli pozisyonda 409 (script hic kosmaz)
    import hibrit_trader.panel as panel
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        panel.api_canli_swap(motor="v6")
    assert e.value.status_code == 400
    (canli_data_dir / "canli_state.json").write_text(json.dumps({
        "positions": [{"pair": "X / SOL", "canli_miktar": 5.0}]}))
    with pytest.raises(HTTPException) as e:
        panel.api_canli_swap(motor="r1")
    assert e.value.status_code == 409
    assert "X / SOL" in e.value.detail
