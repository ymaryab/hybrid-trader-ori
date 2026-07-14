"""V7 senaryo motoru testleri: v6 birebir + TEK fark -%10 felaket freni."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v7_session as v7
from hibrit_trader.v7_session import V7Engine


@pytest.fixture(autouse=True)
def v7_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    # fast feed testte kapali: giris teyidi gercek thread/HTTP acmasin
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    # rejim_reject_kaydet: paylasilan kuyruk temiz, gercek daemon thread acilmasin
    monkeypatch.setattr("hibrit_trader.entry_fresh._watch", {})
    monkeypatch.setattr("hibrit_trader.entry_fresh._start_recheck_thread", lambda: None)
    monkeypatch.delenv("BROKER_MODE", raising=False)  # testte daima paper exec
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="ZP1", token="ZT1", price=1.0, liq=150_000.0, h1=15.0, m5=-2.0):
    return SimpleNamespace(
        name="Z / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(v7, "scan_all", lambda chains: pairs)
    monkeypatch.setattr(
        v7, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(v7.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


# ---- v6 bandi korunuyor: h1 10..50 ------------------------------------------------

def test_entry_rejects_h1_above_50(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    # RMG tipi dikey pump: v6 gibi v7 de girmez
    assert _enter(eng, monkeypatch, _pair(h1=459658.0)) == []
    assert _enter(eng, monkeypatch, _pair(h1=50.1)) == []


def test_entry_accepts_h1_at_50(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(h1=50.0))) == 1


def test_entry_rejects_h1_below_10(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=9.9)) == []


# ---- Golge'den korunanlar ----------------------------------------------------------

def test_entry_golge_rules_preserved(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=99_000)) == []          # liq >= 100k
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []       # rejim negatif kapali
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.34) == []       # cift ayar: 0.35 alti kapali
    assert len(_enter(eng, monkeypatch, _pair(h1=45.0, m5=-5.0), sol_h1=0.35)) == 1  # esik dahil, m5 sarti yok


def test_rejim_kapaliyken_reject_kaydi(v7_data_dir, monkeypatch):
    import hibrit_trader.entry_fresh as ef
    from hibrit_trader.momentum_session import REJECTS_FILE
    eng = V7Engine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.2) == []
    rows = [json.loads(x) for x in
            (v7_data_dir / REJECTS_FILE).read_text().splitlines()]
    assert rows[-1]["reason"] == "rejim_reject"
    assert rows[-1]["engine"] == "V7"
    assert rows[-1]["sol_chg_h1"] == 0.2
    assert "ZP1" in ef._watch


def test_candidates_sorted_highest_h1_first(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    low = _pair(pool="PL", token="TL", h1=12.0)
    high = _pair(pool="PH", token="TH", h1=45.0)
    positions = _enter(eng, monkeypatch, [low, high])
    assert positions[0]["pool_address"] == "PH"


# ---- 13 Tem cift ayar: rejim 0.35 + h1 20-40 bant kacinma ---------------------------

def test_cift_ayar_varsayilanlar():
    assert v7.SOL_H1_MIN == 0.35
    assert v7.H1_SKIP_LO == 20.0
    assert v7.H1_SKIP_HI == 40.0


def test_rejim_sinir_035_kabul_034_red_none_kapali(v7_data_dir, monkeypatch):
    assert _enter(V7Engine(_settings()), monkeypatch, _pair(), sol_h1=0.34) == []
    assert _enter(V7Engine(_settings()), monkeypatch, _pair(), sol_h1=None) == []  # fail-closed
    assert len(_enter(V7Engine(_settings()), monkeypatch, _pair(), sol_h1=0.35)) == 1


def test_h1_bant_sinirlari(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    _enter(eng, monkeypatch, [
        _pair(pool="P199", token="T199", h1=19.9),
        _pair(pool="P20", token="T20", h1=20.0),
        _pair(pool="P40", token="T40", h1=40.0),
        _pair(pool="P401", token="T401", h1=40.1),
    ])
    pools = {p["pool_address"] for p in eng.positions}
    assert pools == {"P199", "P401"}  # 19.9 ve 40.1 kabul, 20 ve 40 red


def test_h1_bant_skip_kaydi_ve_dedup(v7_data_dir, monkeypatch):
    from hibrit_trader.momentum_session import REJECTS_FILE
    eng = V7Engine(_settings())
    monkeypatch.setattr(v7, "sol_h1_son_olcum", lambda: (0.42, time.time()))
    assert _enter(eng, monkeypatch, _pair(h1=30.0)) == []
    rows = [json.loads(x) for x in
            (v7_data_dir / REJECTS_FILE).read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["reason"] == "h1_bant_skip"
    assert r["engine"] == "V7"
    assert r["chg_h1"] == 30.0
    assert r["chg_m5"] == -2.0  # A2 analizi icin m5 kayitta
    assert r["sol_chg_h1"] == 0.42
    assert r["pool_address"] == "ZP1"
    # 30dk dedup: ayni havuz ikinci tickte tekrar yazilmaz
    assert _enter(eng, monkeypatch, _pair(h1=25.0)) == []
    rows = (v7_data_dir / REJECTS_FILE).read_text().splitlines()
    assert len(rows) == 1


def test_h1_bant_m5_sinirlari(v7_data_dir, monkeypatch):
    # A2: bant ici aday yalniz m5 <= 0 ise atlanir; m5 0.0 sinirda ATLA, 0.1 GEC
    eng = V7Engine(_settings())
    _enter(eng, monkeypatch, [
        _pair(pool="PM0", token="TM0", h1=30.0, m5=0.0),
        _pair(pool="PM1", token="TM1", h1=30.0, m5=0.1),
    ])
    pools = {p["pool_address"] for p in eng.positions}
    assert pools == {"PM1"}  # m5 0.0 atlandi, 0.1 girdi


def test_h1_bant_m5_pozitif_skip_kaydi_yazilmaz(v7_data_dir, monkeypatch):
    from hibrit_trader.momentum_session import REJECTS_FILE
    eng = V7Engine(_settings())
    assert len(_enter(eng, monkeypatch, _pair(h1=30.0, m5=2.5))) == 1
    assert not (v7_data_dir / REJECTS_FILE).exists()  # skip satiri yok


def test_h1_bant_disi_adayda_m5_degerlendirilmez():
    # bant disi: m5 ne olursa olsun atlama fonksiyonu False doner
    assert v7.h1_bant_atla(45.0, -5.0) is False
    assert v7.h1_bant_atla(19.9, 0.0) is False
    assert v7.h1_bant_atla(40.1, -99.0) is False


def test_h1_bant_m5_bilinmiyorsa_eski_davranis():
    # m5 verisi yoksa (None) kosulsuz atla: koruma tarafinda kal
    assert v7.h1_bant_atla(30.0) is True
    assert v7.h1_bant_atla(30.0, None) is True


def test_h1_bant_m5_kosulu_env_ile_kapatilir(monkeypatch):
    import importlib
    monkeypatch.setenv("V7_H1_SKIP_M5_KOSUL", "0")
    try:
        importlib.reload(v7)
        assert v7.H1_SKIP_M5_KOSUL is False
        assert v7.h1_bant_atla(30.0, 5.0) is True  # eski kosulsuz skip
    finally:
        monkeypatch.delenv("V7_H1_SKIP_M5_KOSUL")
        importlib.reload(v7)
    assert v7.H1_SKIP_M5_KOSUL is True
    assert v7.h1_bant_atla(30.0, 5.0) is False


def test_h1_bant_lo_esit_hi_kacinma_kapali(v7_data_dir, monkeypatch):
    monkeypatch.setattr(v7, "H1_SKIP_LO", 20.0)
    monkeypatch.setattr(v7, "H1_SKIP_HI", 20.0)
    assert len(_enter(V7Engine(_settings()), monkeypatch, _pair(h1=30.0))) == 1


def test_cift_ayar_env_override(monkeypatch):
    import importlib
    monkeypatch.setenv("V7_SOL_H1_MIN", "0.5")
    monkeypatch.setenv("V7_H1_SKIP_LO", "25")
    monkeypatch.setenv("V7_H1_SKIP_HI", "35")
    try:
        importlib.reload(v7)
        assert v7.SOL_H1_MIN == 0.5
        assert v7.H1_SKIP_LO == 25.0 and v7.H1_SKIP_HI == 35.0
        assert v7.h1_bant_atla(30.0) is True
        assert v7.h1_bant_atla(24.9) is False
        assert v7.h1_bant_atla(35.1) is False
    finally:
        monkeypatch.delenv("V7_SOL_H1_MIN")
        monkeypatch.delenv("V7_H1_SKIP_LO")
        monkeypatch.delenv("V7_H1_SKIP_HI")
        importlib.reload(v7)
    assert v7.SOL_H1_MIN == 0.35
    assert v7.H1_SKIP_LO == 20.0 and v7.H1_SKIP_HI == 40.0


def test_entry_keeps_cooldown(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    eng._cooldown_until["ZT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- Cikislar (golge birebir) -------------------------------------------------------

def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 100.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(v7, "fetch_pool_snapshot", lambda c, ch, p: (price, None))
    monkeypatch.setattr(v7.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / v7.TRADES_FILE).read_text().splitlines()[-1])


def test_tp_2(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.02, t0 + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert t["exit_reason"] == "tp_2"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + v7.COOLDOWN_EXIT_SEC
    )


def test_patience_holds_above_brake(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    # -%9.9: fren tetiklenmez, sabir tutar (kurtarma bolgesi yasiyor)
    _tick_price(eng, pos, pos["entry_price"] * 0.901, pos["opened_ts"] + v7.GRACE_SEC - 1, monkeypatch)
    assert eng.positions == [pos]


def test_brake_fires_at_minus_10(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    # TEK fark: ilk 30dk icinde -%11 -> sabir iptal, aninda stop_felaket
    _tick_price(eng, pos, pos["entry_price"] * 0.89, t0 + 60, monkeypatch)
    assert eng.positions == []
    t = _last(v7_data_dir)
    assert t["exit_reason"] == "stop_felaket"
    # kayip cikisi: 60dk cooldown
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + v7.COOLDOWN_LOSS_SEC
    )


def test_late_stop_after_30min(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.97, t0 + v7.GRACE_SEC + 1, monkeypatch)
    t = _last(v7_data_dir)
    assert t["exit_reason"] == "stop_gec"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + v7.GRACE_SEC + 1 + v7.COOLDOWN_LOSS_SEC
    )


def test_timeout_60(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.005, pos["opened_ts"] + v7.CEILING_SEC + 1, monkeypatch)
    assert _last(v7_data_dir)["exit_reason"] == "timeout_60"


# ---- sol_h1 kaydi + tam set + izolasyon (v6 ile ayni) --------------------------------

def test_sol_h1_recorded_in_trade(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    assert pos["sol_chg_h1"] == 0.77
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert t["sol_chg_h1"] == 0.77
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_h1", "liq_entry", "mfe_pct", "mae_pct", "friction_pct"):
        assert k in t, k


def test_writes_only_v7_files(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in v7_data_dir.iterdir())
    assert all(f.startswith("v7_") for f in files), files
    state = json.loads((v7_data_dir / v7.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0


# ---- Kill-switch tek-seferlik log (M1 paterni) ---------------------------------------

def test_kill_switch_tek_seferlik_log(v7_data_dir, monkeypatch, caplog):
    import logging
    eng = V7Engine(_settings())
    monkeypatch.setattr(v7, "kill_is_active", lambda: True)
    with caplog.at_level(logging.WARNING, logger="hibrit_trader.v7_session"):
        eng._enter(client=SimpleNamespace())
        eng._enter(client=SimpleNamespace())
        assert sum("kill-switch AKTIF" in r.message for r in caplog.records) == 1
        monkeypatch.setattr(v7, "kill_is_active", lambda: False)
        monkeypatch.setattr(v7, "scan_all", lambda chains: [])
        eng._enter(client=SimpleNamespace())
        assert sum("kill-switch kalkti" in r.message for r in caplog.records) == 1


# ---- Gunluk zarar kesicisi (M1 paterni; varsayilan 0 = kapali) -----------------------

def test_daily_loss_varsayilan_kapali(v7_data_dir, monkeypatch):
    assert v7.DAILY_LOSS_LIMIT_USD == 0.0
    eng = V7Engine(_settings())
    eng._day_realized = -10_000.0
    assert len(_enter(eng, monkeypatch, _pair())) == 1  # limit kapali, giris serbest


def test_daily_loss_limit_asilinca_giris_yok_tek_log(v7_data_dir, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(v7, "DAILY_LOSS_LIMIT_USD", 50.0)
    eng = V7Engine(_settings())
    eng._day_realized_add(-50.0, time.time())
    with caplog.at_level(logging.CRITICAL, logger="hibrit_trader.v7_session"):
        assert _enter(eng, monkeypatch, _pair()) == []
        assert _enter(eng, monkeypatch, _pair()) == []
    assert sum("zarar limiti" in r.message for r in caplog.records) == 1


def test_daily_loss_gun_devri_bloku_kaldirir(v7_data_dir, monkeypatch):
    monkeypatch.setattr(v7, "DAILY_LOSS_LIMIT_USD", 50.0)
    eng = V7Engine(_settings())
    eng._day_key = "2000-01-01"  # dun asilan limit bugunu bloklamasin
    eng._day_realized = -500.0
    assert len(_enter(eng, monkeypatch, _pair())) == 1
    assert eng._day_realized == 0.0


def test_daily_loss_kapanista_birikir(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.89, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert t["pnl_usd"] < 0
    assert eng._day_realized == pytest.approx(t["pnl_usd"], abs=1e-3)


def test_daily_loss_restartta_trades_dosyasindan_yuklenir(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.89, pos["opened_ts"] + 60, monkeypatch)
    kayip = _last(v7_data_dir)["pnl_usd"]
    eng2 = V7Engine(_settings())
    assert eng2._day_realized == pytest.approx(kayip)


# ---- Yurutme katmani kablolamasi (ASAMA 0: fill'ler exec broker uzerinden) ---------

from hibrit_trader.broker import ExecFill  # noqa: E402


class _StubExec:
    def __init__(self, mode, fills=None):
        self.mode = mode
        self.orders = []
        self._fills = list(fills or [])

    def execute(self, order):
        self.orders.append(order)
        if self._fills:
            return self._fills.pop(0)
        return ExecFill(ok=False, neden="stub")


def test_exec_varsayilan_paper_ve_muhasebe_ayni(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    assert eng._exec.mode == "paper" and eng._exec_arizali is False
    pos = _open(eng)
    # paper muhasebe birebir: miktar = usd / eff_price, tx alani yok
    assert pos["amount_token"] == pytest.approx(100.0 / pos["entry_price"])
    assert "tx_al" not in pos
    _tick_price(eng, pos, pos["entry_price"] * 1.05, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert "signature" not in t and "signature_al" not in t
    # paper kayitta canli kolonlari YOK: panel canli satiri imzasiz kayittan uretmez
    assert "canli_pnl_usd" not in t and "canli_miktar" not in t


def test_exec_dryrun_hatasi_yarisi_etkilemez(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    eng._exec = _StubExec("dryrun")  # her execute ok=False doner
    pos = _open(eng)
    assert pos and "tx_al" not in pos  # paper muhasebe aynen surdu
    o = eng._exec.orders[0]
    assert o.yon == "al" and o.usd == 100.0 and o.slippage_bps == 50
    assert o.ref_fiyat == pytest.approx(pos["entry_price"])
    _tick_price(eng, pos, pos["entry_price"] * 1.05, pos["opened_ts"] + 60, monkeypatch)
    assert eng.positions == []  # dryrun satis hatasi cikisi engellemez
    assert "signature" not in _last(v7_data_dir)


def test_exec_live_alim_basarisiz_giris_yok(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    eng._exec = _StubExec("live")
    assert eng._open_position(_pair(), 100.0, sol_h1=0.77) is False
    assert eng.positions == []
    assert eng.balance == v7.START_BALANCE  # bakiye mutasyonu fill'den sonra


def test_exec_live_fill_fiyat_imza_baglayici_muhasebe_paper_boyut(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    al = ExecFill(ok=True, fiyat=1.05, miktar_token=95.0, tx_id="SIGAL")
    sat = ExecFill(ok=True, fiyat=1.20, miktar_token=95.0, tx_id="SIGSAT")
    eng._exec = _StubExec("live", [al, sat])
    pos = _open(eng)
    assert pos["entry_price"] == 1.05
    # muhasebe paper boyutta: usd / fill fiyati; cuzdan miktari ayri saklanir
    assert pos["amount_token"] == pytest.approx(100.0 / 1.05)
    assert pos["canli_miktar"] == 95.0
    assert pos["tx_al"] == "SIGAL"
    _tick_price(eng, pos, 1.30, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert t["exit_price"] == 1.20  # canli satis fiyati, paper slip degil
    assert t["signature"] == "SIGSAT" and t["signature_al"] == "SIGAL"
    o_sat = eng._exec.orders[1]
    assert o_sat.yon == "sat" and o_sat.amount_token == 95.0  # gercek miktar satilir


def test_karar_fiyatlari_kayitta_fill_ezmez(v7_data_dir, monkeypatch):
    # B1: canli fill entry/exit fiyatini ezer (baglayici) ama karar fiyatlari
    # ayri kolonlarda kalir; prim analizi fill vs karar kiyasini buradan yapar
    eng = V7Engine(_settings())
    al = ExecFill(ok=True, fiyat=1.05, miktar_token=95.0, tx_id="SIGAL")
    sat = ExecFill(ok=True, fiyat=1.20, miktar_token=95.0, tx_id="SIGSAT")
    eng._exec = _StubExec("live", [al, sat])
    pos = _open(eng)
    karar = pos["karar_fiyat"]
    assert 0.99 < karar < 1.01  # taze fiyat + slip, fill (1.05) DEGIL
    assert pos["entry_price"] == 1.05
    _tick_price(eng, pos, 1.30, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert t["karar_fiyat"] == karar
    assert 1.29 < t["karar_cikis"] < 1.31  # paper slipli karar, fill (1.20) DEGIL
    assert t["exit_price"] == 1.20
    assert t["karar_pnl_pct"] == pytest.approx(
        (t["karar_cikis"] / karar - 1) * 100, abs=0.01)


def test_karar_fiyat_paper_modda_entry_ile_ayni(v7_data_dir, monkeypatch):
    # paper: fill ezmesi yok, karar kolonlari mevcut kayitla birebir
    eng = V7Engine(_settings())
    pos = _open(eng)
    assert pos["karar_fiyat"] == pos["entry_price"]
    _tick_price(eng, pos, 1.30, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert t["karar_cikis"] == t["exit_price"]
    assert t["karar_pnl_pct"] == t["pnl_pct"]


def test_karar_fiyat_eski_pozisyonda_yoksa_none(v7_data_dir, monkeypatch):
    # deploy sirasinda acik kalan eski pozisyonlar karar_fiyat tasimaz: kapanis patlamaz
    eng = V7Engine(_settings())
    pos = _open(eng)
    pos.pop("karar_fiyat")
    _tick_price(eng, pos, 1.30, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7_data_dir)
    assert t["karar_fiyat"] is None
    assert t["karar_pnl_pct"] is None
    assert t["karar_cikis"] > 0


def test_exec_live_biletli_fill_satis_gercek_miktari_kullanir(v7_data_dir, monkeypatch):
    # canli bilet MTM x oran senaryosu: broker $25'lik alir (23.8 token @ 1.05),
    # paper muhasebe $100 boyutta surer, satis cuzdandaki 23.8'i satar.
    eng = V7Engine(_settings())
    al = ExecFill(ok=True, fiyat=1.05, miktar_token=23.8, tx_id="SIGAL")
    sat = ExecFill(ok=True, fiyat=1.20, miktar_token=23.8, tx_id="SIGSAT")
    eng._exec = _StubExec("live", [al, sat])
    pos = _open(eng)
    assert pos["cost_usd"] == 100.0  # paper bilet degismedi
    assert pos["amount_token"] == pytest.approx(100.0 / 1.05)
    assert pos["canli_miktar"] == 23.8
    _tick_price(eng, pos, 1.30, pos["opened_ts"] + 60, monkeypatch)
    o_sat = eng._exec.orders[1]
    assert o_sat.amount_token == 23.8
    t = _last(v7_data_dir)
    # PnL paper boyutta hesaplanir (yaris etkilenmez)
    assert t["pnl_usd"] == pytest.approx(
        (100.0 / 1.05) * 1.20 - 100.0 - 0.002, abs=0.01)
    # gercek cuzdan pnl ayri kolonlarda: fill fiyat farki x zincir miktari
    assert t["canli_miktar"] == 23.8
    assert t["canli_pnl_usd"] == pytest.approx((1.20 - 1.05) * 23.8, abs=1e-6)


def test_exec_live_satis_basarisiz_pozisyon_acik_kalir(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    al = ExecFill(ok=True, fiyat=1.0, miktar_token=100.0, tx_id="S1")
    eng._exec = _StubExec("live", [al])  # satis icin fill yok -> ok=False
    pos = _open(eng)
    _tick_price(eng, pos, 1.10, pos["opened_ts"] + 60, monkeypatch)
    assert len(eng.positions) == 1  # kapanmadi, sonraki kadansta tekrar
    assert not (v7_data_dir / v7.TRADES_FILE).exists()


def test_exec_arizali_girisleri_kapatir_cikislar_surer(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    eng._exec_arizali = True
    assert eng._entries_blocked() == "exec_arizali"
    assert _enter(eng, monkeypatch, _pair(pool="ZP9", token="ZT9")) == [pos]
    _tick_price(eng, pos, pos["entry_price"] * 1.05, pos["opened_ts"] + 60, monkeypatch)
    assert eng.positions == []  # cikis calisti


# ---- Canli asimetri B1: kademeli satis toleransi (12 Tem) ---------------------------

def test_satis_bps_tp_150_alim_50_kalir(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    eng._exec = _StubExec("dryrun")
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    assert eng._exec.orders[0].yon == "al" and eng._exec.orders[0].slippage_bps == 50
    o_sat = eng._exec.orders[-1]
    assert o_sat.yon == "sat" and o_sat.slippage_bps == 150
    t = _last(v7_data_dir)
    assert t["exit_reason"] == "tp_2"
    # poll yolunda olcum kolonlari: kaynak poll, gecikme yok
    assert t["price_source"] == "poll" and t["tetik_gecikme_sec"] is None


def test_satis_bps_stop_felaket_1000_dryrun_tek_deneme(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    eng._exec = _StubExec("dryrun")
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.88, pos["opened_ts"] + 60, monkeypatch)
    assert _last(v7_data_dir)["exit_reason"] == "stop_felaket"
    assert eng._exec.orders[-1].slippage_bps == 1000
    assert len(eng._exec.orders) == 2  # al + tek satis: tekrar sadece live'da


def test_satis_bps_stop_gec_300(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    eng._exec = _StubExec("dryrun")
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.97,
                pos["opened_ts"] + v7.GRACE_SEC + 1, monkeypatch)
    assert _last(v7_data_dir)["exit_reason"] == "stop_gec"
    assert eng._exec.orders[-1].slippage_bps == 300


def test_satis_bps_timeout_150(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    eng._exec = _StubExec("dryrun")
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.005,
                pos["opened_ts"] + v7.CEILING_SEC + 1, monkeypatch)
    assert _last(v7_data_dir)["exit_reason"] == "timeout_60"
    assert eng._exec.orders[-1].slippage_bps == 150


def test_stop_yolunda_satis_tekrari_basarili(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    al = ExecFill(ok=True, fiyat=1.0, miktar_token=100.0, tx_id="S1")
    fail = ExecFill(ok=False, neden="slippage")
    sat = ExecFill(ok=True, fiyat=0.88, miktar_token=100.0, tx_id="S2")
    eng._exec = _StubExec("live", [al, fail, fail, sat])
    pos = _open(eng)
    uykular: list[float] = []
    monkeypatch.setattr(v7.time, "sleep", lambda s: uykular.append(s))
    _tick_price(eng, pos, 0.88, pos["opened_ts"] + 60, monkeypatch)
    assert eng.positions == []
    assert [o.yon for o in eng._exec.orders] == ["al", "sat", "sat", "sat"]
    assert all(o.slippage_bps == 1000 for o in eng._exec.orders[1:])
    assert uykular == [v7.STOP_RETRY_SEC, v7.STOP_RETRY_SEC]
    t = _last(v7_data_dir)
    assert t["exit_reason"] == "stop_felaket" and t["signature"] == "S2"


def test_stop_yolunda_tum_tekrarlar_basarisiz_pozisyon_acik(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    al = ExecFill(ok=True, fiyat=1.0, miktar_token=100.0, tx_id="S1")
    eng._exec = _StubExec("live", [al])  # satis fill'i yok: 3 deneme de ok=False
    pos = _open(eng)
    uykular: list[float] = []
    monkeypatch.setattr(v7.time, "sleep", lambda s: uykular.append(s))
    _tick_price(eng, pos, 0.88, pos["opened_ts"] + 60, monkeypatch)
    assert len(eng.positions) == 1  # SATIS ERTELENDI, sonraki kadansta tekrar
    assert len(eng._exec.orders) == 4  # al + 3 satis denemesi
    assert uykular == [v7.STOP_RETRY_SEC, v7.STOP_RETRY_SEC]
    assert not (v7_data_dir / v7.TRADES_FILE).exists()


def test_tp_yolunda_live_satis_tek_deneme(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    al = ExecFill(ok=True, fiyat=1.0, miktar_token=100.0, tx_id="S1")
    eng._exec = _StubExec("live", [al])
    pos = _open(eng)
    uykular: list[float] = []
    monkeypatch.setattr(v7.time, "sleep", lambda s: uykular.append(s))
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    assert len(eng.positions) == 1  # basarisiz tp satisi ertelenir, tekrar YOK
    assert len(eng._exec.orders) == 2  # al + tek satis denemesi
    assert uykular == []


# ---- Canli asimetri B2: hizli goz (fast_exit_tick + feed kablolamasi) ---------------

class _FakeFeed:
    def __init__(self, prices=None):
        self.prices = dict(prices or {})
        self.eklenen: list[str] = []
        self.cikan: list[str] = []

    def get_price(self, pool, max_age_sec=10.0):
        return self.prices.get(pool)

    def add_pool(self, pool):
        self.eklenen.append(pool)

    def remove_pool(self, pool):
        self.cikan.append(pool)


def test_open_position_havuzu_feede_ekler(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    feed = _FakeFeed()
    monkeypatch.setattr(v7, "get_feed", lambda: feed)
    pos = _open(eng)
    assert feed.eklenen == [pos["pool_address"]]


def test_fast_exit_tick_feed_fiyatiyla_kapatir(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    now = pos["opened_ts"] + 60
    feed = _FakeFeed({pos["pool_address"]: (pos["entry_price"] * 0.88, now - 1.0)})
    monkeypatch.setattr(v7, "get_feed", lambda: feed)
    monkeypatch.setattr(v7.time, "time", lambda: now)
    eng.fast_exit_tick()
    assert eng.positions == []
    t = _last(v7_data_dir)
    assert t["exit_reason"] == "stop_felaket"
    assert t["price_source"] == "fast"
    assert t["tetik_gecikme_sec"] == pytest.approx(1.0)
    assert feed.cikan == [pos["pool_address"]]  # kapanan havuz feed'ten cikti


def test_fast_exit_tick_feed_kapaliyken_dokunmaz(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    monkeypatch.setattr(v7, "get_feed", lambda: None)
    eng.fast_exit_tick()
    assert eng.positions == [pos]


def test_fast_exit_tick_taze_kayit_yoksa_dokunmaz(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    feed = _FakeFeed()  # havuz icin kayit yok (bayat/eksik) -> 30s tick kapsar
    monkeypatch.setattr(v7, "get_feed", lambda: feed)
    monkeypatch.setattr(v7.time, "time", lambda: pos["opened_ts"] + 60)
    eng.fast_exit_tick()
    assert eng.positions == [pos]


def test_manage_exits_feed_oncelikli_poll_cagrilmaz(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    now = pos["opened_ts"] + 60
    feed = _FakeFeed({pos["pool_address"]: (pos["entry_price"] * 1.02, now - 0.5)})
    monkeypatch.setattr(v7, "get_feed", lambda: feed)

    def _poll_yasak(client, chain, pool):
        raise AssertionError("feed varken fetch_pool_snapshot cagrilmamali")

    monkeypatch.setattr(v7, "fetch_pool_snapshot", _poll_yasak)
    monkeypatch.setattr(v7.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())
    t = _last(v7_data_dir)
    assert t["exit_reason"] == "tp_2" and t["price_source"] == "fast"
    assert t["tetik_gecikme_sec"] == pytest.approx(0.5)


# ---- Kor fiyat alarmi (14 Tem taramasi R1) ------------------------------------------

def test_kor_fiyat_esik_altinda_sessiz(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    monkeypatch.setattr(v7, "get_feed", lambda: None)
    monkeypatch.setattr(v7, "fetch_pool_snapshot", lambda c, ch, p: (None, None))
    monkeypatch.setattr(v7.time, "time", lambda: t0 + v7.KOR_FIYAT_SEC - 1)
    eng._manage_exits(client=SimpleNamespace())
    assert "kor_fiyat" not in pos


def test_kor_fiyat_alarmi_ve_toparlanma(v7_data_dir, monkeypatch, caplog):
    import logging

    eng = V7Engine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    monkeypatch.setattr(v7, "get_feed", lambda: None)
    monkeypatch.setattr(v7, "fetch_pool_snapshot", lambda c, ch, p: (None, None))
    now = t0 + v7.KOR_FIYAT_SEC + 1
    monkeypatch.setattr(v7.time, "time", lambda: now)
    with caplog.at_level(logging.CRITICAL):
        eng._manage_exits(client=SimpleNamespace())
        # ayni an ikinci tur: alarm 60s araliginda tekrarlanmaz
        eng._manage_exits(client=SimpleNamespace())
    assert pos["kor_fiyat"] is True
    krit = [r for r in caplog.records if "KOR FIYAT" in r.getMessage()]
    assert len(krit) == 1
    # pozisyon kapatilmadi: degerleme donuk ama islem tetiklenmedi
    assert eng.positions == [pos]
    # taze fiyat geri gelince bayrak temizlenir
    _tick_price(eng, pos, pos["entry_price"], now + 10, monkeypatch)
    assert "kor_fiyat" not in pos and "_kor_alarm_ts" not in pos
    assert pos["_taze_fiyat_ts"] == now + 10


# ---- R2-alim: belirsiz alim mutabakati (broker sonucu motora devri) -----------------

class _BelirsizStubExec(_StubExec):
    """Ilk alim islem_belirsiz doner; belirsiz_sonuc scripti sirayla tuketilir."""

    def __init__(self, sonuclar):
        super().__init__("live", [ExecFill(ok=False, neden="islem_belirsiz")])
        self.sonuclar = list(sonuclar)

    def belirsiz_sonuc(self, engine):
        assert engine == "V7"
        return self.sonuclar.pop(0)


def test_belirsiz_alim_zincirde_gerceklesti_pozisyon_acilir(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    detay = {"token_address": "ZT1", "fiyat": 0.25, "miktar_token": 40.0,
             "tx_id": "SIGX", "usd_exec": 10.0}
    eng._exec = _BelirsizStubExec([("bekliyor", None), ("gerceklesti", detay)])
    assert eng._open_position(_pair(), 100.0, sol_h1=0.77) is False
    # sonuc gelmeden pozisyon YAZILMAZ, bakiye dokunulmaz; aday saklanir
    assert eng.positions == [] and eng.balance == v7.START_BALANCE
    assert eng._belirsiz_aday is not None
    karar = eng._belirsiz_aday["karar_fiyat"]
    eng._belirsiz_takip()  # bekliyor: aday korunur
    assert eng._belirsiz_aday is not None and eng.positions == []
    eng._belirsiz_takip()  # gerceklesti: pozisyon benimsenir
    assert eng._belirsiz_aday is None
    pos = eng.positions[0]
    assert pos["entry_price"] == 0.25          # zincir gercegi baglayici
    assert pos["karar_fiyat"] == karar         # karar fiyati korunur (B1)
    assert pos["canli_miktar"] == 40.0 and pos["tx_al"] == "SIGX"
    assert pos["amount_token"] == pytest.approx(100.0 / 0.25)  # muhasebe paper boyut
    assert pos["belirsiz_mutabakat"] is True
    gas = v7.GAS_COST_USD.get("solana", 0.1)
    assert eng.balance == pytest.approx(v7.START_BALANCE - 100.0 - gas)


def test_belirsiz_alim_zincirde_yoksa_kayit_yazilmaz(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    eng._exec = _BelirsizStubExec([("yok", None)])
    assert eng._open_position(_pair(), 100.0, sol_h1=0.77) is False
    eng._belirsiz_takip()
    assert eng._belirsiz_aday is None
    assert eng.positions == [] and eng.balance == v7.START_BALANCE
    assert not (v7_data_dir / v7.TRADES_FILE).exists()  # hicbir kayit yok


def test_belirsiz_cozulemezse_aday_dusulur_pozisyon_yok(v7_data_dir, monkeypatch):
    # kilit broker tarafinda kapali kalir (tekrar alim yok); motor pozisyon yazmaz
    eng = V7Engine(_settings())
    eng._exec = _BelirsizStubExec([("cozulemedi", None)])
    assert eng._open_position(_pair(), 100.0, sol_h1=0.77) is False
    eng._belirsiz_takip()
    assert eng._belirsiz_aday is None
    assert eng.positions == [] and eng.balance == v7.START_BALANCE


def test_belirsiz_takip_paper_modda_calismaz(v7_data_dir, monkeypatch):
    eng = V7Engine(_settings())
    eng._belirsiz_aday = {"pair": "X"}  # savunma: paper exec'te sonuc sorgusu yok
    eng._belirsiz_takip()
    assert eng._belirsiz_aday is None
