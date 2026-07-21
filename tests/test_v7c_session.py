"""V7C senaryo motoru testleri: v7 kurallari birebir, tek fark major evren + sabit paper."""

from __future__ import annotations

import importlib
import json
import time
from types import SimpleNamespace

import pytest

import hibrit_trader.v7_session as v7
import hibrit_trader.v7c_session as v7c
from hibrit_trader.broker import PaperExecBroker
from hibrit_trader.v7c_session import V7CEngine


@pytest.fixture(autouse=True)
def v7c_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    # fast feed testte kapali: giris teyidi gercek thread/HTTP acmasin
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    # rejim_reject_kaydet: paylasilan kuyruk temiz, gercek daemon thread acilmasin
    monkeypatch.setattr("hibrit_trader.entry_fresh._watch", {})
    monkeypatch.setattr("hibrit_trader.entry_fresh._start_recheck_thread", lambda: None)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="CP1", token="CT1", price=1.0, liq=5_000_000.0, h1=5.0, m5=-2.0):
    return SimpleNamespace(
        name="C / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=m5, chg_h1=h1,
    )


def _enter(eng, monkeypatch, pairs, sol_h1=1.0):
    if not isinstance(pairs, list):
        pairs = [pairs]
    monkeypatch.setattr(eng, "_scan_universe", lambda client: pairs)
    monkeypatch.setattr(
        v7c, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(v7c.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


def _open(eng, **kw):
    assert eng._open_position(_pair(**kw), 200.0, sol_h1=0.77)
    return eng.positions[0]


def _tick_price(eng, pos, price, now, monkeypatch):
    monkeypatch.setattr(v7c, "fetch_pool_snapshot", lambda c, ch, p: (price, None))
    monkeypatch.setattr(v7c.time, "time", lambda: now)
    eng._manage_exits(client=SimpleNamespace())


def _last(data_dir):
    return json.loads((data_dir / v7c.TRADES_FILE).read_text().splitlines()[-1])


# ---- SABIT PAPER: BROKER_MODE zincirinden bagimsiz ------------------------------------

def test_exec_paper_kalir_brokermode_live_iken(v7c_data_dir, monkeypatch):
    monkeypatch.setenv("BROKER_MODE", "live")
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    eng = V7CEngine(_settings())
    assert isinstance(eng._exec, PaperExecBroker)
    assert eng._exec.mode == "paper"
    # muhasebe paper: tx/imza alanlari hicbir zaman olusmaz
    pos = _open(eng)
    assert "tx_al" not in pos and "canli_miktar" not in pos
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7c_data_dir)
    assert "signature" not in t and "signature_al" not in t


# ---- v7 kurallari korunur; farklar: evren esigi + h1 bandi 2..10 -----------------------

def test_v7_sabitleri_birebir(v7c_data_dir):
    # 15 Tem: v7 canli hat 15dk pencere + felaket freni geri; v7c paper eski
    # kurallari (120dk pencere, felaket yok) korur. Ortak sabitler kilitli.
    assert v7c.TP_PCT == v7.TP_PCT == 2.0
    assert v7c.LATE_STOP_PCT == v7.LATE_STOP_PCT == -2.0
    assert v7c.GRACE_SEC == 120 * 60 and v7.GRACE_SEC == 15 * 60
    assert v7c.CEILING_SEC == 120 * 60 and v7.CEILING_SEC == 15 * 60
    assert not hasattr(v7c, "DISASTER_PCT")
    assert v7.DISASTER_PCT == -15.0
    # 13 Tem cift ayar: v7 esigi 0.35'e indi, v7c 0.5'te kaldi (kendi esigi)
    assert v7c.SOL_H1_MIN == 0.5
    assert v7.SOL_H1_MIN == 0.35
    assert v7c.MAX_SLOTS == v7.MAX_SLOTS == 5
    assert v7c.START_BALANCE == 1000.0
    # farklar: evren/giris likidite esigi + majore uygun h1 bandi
    assert v7c.LIQ_MIN_USD == 3_000_000.0
    assert v7c.CHG_H1_MIN == 2.0
    assert v7c.CHG_H1_MAX == 10.0


def test_liq_esigi_env_ile_parametrik(v7c_data_dir, monkeypatch):
    monkeypatch.setenv("V7C_MIN_LIQ_USD", "5000000")
    try:
        importlib.reload(v7c)
        assert v7c.LIQ_MIN_USD == 5_000_000.0
    finally:
        monkeypatch.delenv("V7C_MIN_LIQ_USD")
        importlib.reload(v7c)
    assert v7c.LIQ_MIN_USD == 3_000_000.0


# ---- Giris bandi: h1 2..10, liq >= 3M, rejim sol_h1 >= 0.5 ----------------------------

def test_entry_h1_bandi_2_10(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    assert _enter(eng, monkeypatch, _pair(h1=1.9)) == []
    assert _enter(eng, monkeypatch, _pair(h1=10.1)) == []
    assert _enter(eng, monkeypatch, _pair(h1=15.0)) == []  # eski memecoin bandi artik disarida
    assert len(_enter(eng, monkeypatch, _pair(h1=10.0))) == 1
    eng2 = V7CEngine(_settings())
    assert len(_enter(eng2, monkeypatch, _pair(h1=2.0))) == 1


def test_entry_liq_3m_alti_elenir(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    assert _enter(eng, monkeypatch, _pair(liq=2_900_000)) == []
    assert len(_enter(eng, monkeypatch, _pair(liq=3_000_000))) == 1


def test_rejim_esigi_0_5(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    assert _enter(eng, monkeypatch, _pair(), sol_h1=-0.5) == []
    assert _enter(eng, monkeypatch, _pair(), sol_h1=0.4) == []
    assert len(_enter(eng, monkeypatch, _pair(), sol_h1=0.5)) == 1


def test_entry_cooldown_tutar(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    eng._cooldown_until["CT1"] = time.time() + 3600
    assert _enter(eng, monkeypatch, _pair()) == []


# ---- Cikislar: v7 ile birebir (tp_2 +%2 uzeri / stop_gec / timeout_120) ---------------

def test_tp_2(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 1.021, t0 + 60, monkeypatch)
    t = _last(v7c_data_dir)
    assert t["exit_reason"] == "tp_2"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + 60 + v7c.COOLDOWN_EXIT_SEC
    )


def test_sabir_derin_dususte_tutar(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    pos = _open(eng)
    # 14 Tem: fren iptal; grace icinde -%50 bile satmaz
    _tick_price(eng, pos, pos["entry_price"] * 0.50,
                pos["opened_ts"] + v7c.GRACE_SEC - 1, monkeypatch)
    assert eng.positions == [pos]


def test_derin_dusus_grace_sonrasi_stop_gec(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    pos = _open(eng)
    t0 = pos["opened_ts"]
    _tick_price(eng, pos, pos["entry_price"] * 0.89, t0 + v7c.GRACE_SEC + 1, monkeypatch)
    assert eng.positions == []
    t = _last(v7c_data_dir)
    assert t["exit_reason"] == "stop_gec"
    assert eng._cooldown_until[pos["token_address"]] == pytest.approx(
        t0 + v7c.GRACE_SEC + 1 + v7c.COOLDOWN_LOSS_SEC
    )


def test_gec_stop_120dk_sonra(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 0.97,
                pos["opened_ts"] + v7c.GRACE_SEC + 1, monkeypatch)
    assert _last(v7c_data_dir)["exit_reason"] == "stop_gec"


def test_tavan_120dk(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.005,
                pos["opened_ts"] + v7c.CEILING_SEC + 1, monkeypatch)
    assert _last(v7c_data_dir)["exit_reason"] == "timeout_120"


# ---- Izolasyon: sadece v7c_* dosyalari -------------------------------------------------

def test_sadece_v7c_dosyalari_yazilir(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    pos = _open(eng)
    _tick_price(eng, pos, pos["entry_price"] * 1.02, pos["opened_ts"] + 60, monkeypatch)
    files = sorted(p.name for p in v7c_data_dir.iterdir())
    assert all(f.startswith("v7c_") for f in files), files
    state = json.loads((v7c_data_dir / v7c.STATE_FILE).read_text())
    assert state["start_balance"] == 1000.0


def test_sol_h1_ve_tam_set_kaydi(v7c_data_dir, monkeypatch):
    eng = V7CEngine(_settings())
    pos = _open(eng)
    assert pos["sol_chg_h1"] == 0.77
    _tick_price(eng, pos, pos["entry_price"] * 1.03, pos["opened_ts"] + 60, monkeypatch)
    t = _last(v7c_data_dir)
    assert t["sol_chg_h1"] == 0.77
    for k in ("entry_price", "exit_price", "exit_reason", "pnl_usd", "pnl_pct",
              "chg_h1", "liq_entry", "mfe_pct", "mae_pct", "friction_pct"):
        assert k in t, k


# ---- Evren: kur, esik alti eleme, kalicilik (M1 deseni) --------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """DexScreener token sorgusuna sahte havuz dondurur, GoPlus'a bos (fail-open)."""

    def __init__(self, liq_by_addr):
        self.liq_by_addr = liq_by_addr

    def get(self, url, **kw):
        if "goplus" in url or "token_security" in url:
            return _FakeResp({"result": {}})
        addr = url.rsplit("/", 1)[-1]
        liq = self.liq_by_addr.get(addr)
        if liq is None:
            return _FakeResp({"pairs": []})
        return _FakeResp({"pairs": [{
            "chainId": "solana", "dexId": "raydium",
            "pairAddress": f"POOL_{addr[:6]}",
            "baseToken": {"address": addr, "symbol": "TK"},
            "quoteToken": {"symbol": "USDC"},
            "priceUsd": "1.0",
            "liquidity": {"usd": liq},
            "volume": {}, "priceChange": {"h1": 12.0, "m5": 0.5}, "txns": {},
        }]})


def test_evren_tazeleme_liq_filtresi_ve_kalicilik(v7c_data_dir, monkeypatch):
    monkeypatch.setattr(v7c.time, "sleep", lambda s: None)
    monkeypatch.setattr(v7c, "jupiter_referans_fiyat", lambda addr: 1.0)
    seeds = dict(list(v7c.SEED_TOKENS.items())[:3])
    monkeypatch.setattr(v7c, "SEED_TOKENS", seeds)
    addrs = list(seeds.values())
    liqs = {addrs[0]: 9_000_000.0, addrs[1]: 500_000.0, addrs[2]: 3_100_000.0}
    eng = V7CEngine(_settings())
    eng._refresh_universe(_FakeClient(liqs))
    assert [t["token_address"] for t in eng._universe] == [addrs[0], addrs[2]]
    saved = json.loads((v7c_data_dir / v7c.UNIVERSE_FILE).read_text())
    assert len(saved["tokens"]) == 2
    assert saved["liq_min_usd"] == 3_000_000.0


def test_evren_bayatsa_tarama_tazeler(v7c_data_dir, monkeypatch):
    (v7c_data_dir / v7c.UNIVERSE_FILE).write_text(json.dumps({
        "updated_ts": time.time() - 25 * 3600,
        "tokens": [{"symbol": "SOL", "token_address": "A1",
                    "pool_address": "P1", "liq_usd": 9e6}],
    }))
    eng = V7CEngine(_settings())
    called = []

    def _fake_refresh(client):
        called.append(1)
        eng._universe_ts = time.time()

    monkeypatch.setattr(eng, "_refresh_universe", _fake_refresh)

    class _C:
        def get(self, url, **kw):
            return _FakeResp({"pairs": []})

    eng._scan_universe(_C())
    assert called == [1]


# ---- 21 Tem: 30dk kosulsuz cikis (kullanici karari) ------------------------


def test_tp2_ve_30dk_kosulsuz_cikis(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setattr("hibrit_trader.fast_price.ENABLED", False)
    import hibrit_trader.v7c_session as v7c
    monkeypatch.setattr(v7c, "guard_price",
                        lambda pos, price, now, tag, liquidity_usd=None: (price, False))
    from types import SimpleNamespace
    import time as _t
    eng = v7c.V7CEngine(SimpleNamespace(scan_chains=("solana",)))
    now = _t.time()

    def poz(yas_dk=0.0):
        return {"pair": "T / SOL", "entry_price": 1.0, "last_price": 1.0,
                "opened_ts": now - yas_dk * 60, "mfe_pct": 0.0, "mae_pct": 0.0}

    assert eng._eval_position(poz(), 1.03, now) == "tp_2"
    assert eng._eval_position(poz(yas_dk=29), 0.7, now) is None
    assert eng._eval_position(poz(yas_dk=31), 0.7, now) == "timeout_30"
