"""Yurutme katmani testleri: quote/parse/hata yollari + live cift kilit + cuzdan korumasi."""

from __future__ import annotations

import json

import httpx
import pytest

import hibrit_trader.broker as broker
from hibrit_trader.broker import (
    DryrunExecBroker,
    ExecOrder,
    LiveExecBroker,
    PaperExecBroker,
    _cuzdan_yukle,
    _golge_worker,
    live_kilit_acik,
    make_exec_broker,
)

TOK = "So11111111111111111111111111111111111111112"


# ---- sahte http istemcisi --------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)


class FakeClient:
    """URL/method dispatch: quote GET, RPC ve swap POST."""

    def __init__(self, *, quote=None, quote_exc=None, decimals=9, dec_fail=False,
                 sim_err=None, swap_tx="dHg="):
        self.quote = quote
        self.quote_exc = quote_exc
        self.decimals = decimals
        self.dec_fail = dec_fail
        self.sim_err = sim_err
        self.swap_tx = swap_tx
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("get", url, params))
        if self.quote_exc is not None:
            raise self.quote_exc
        return FakeResponse(self.quote or {})

    def post(self, url, json=None, timeout=None):
        self.calls.append(("post", url, json))
        method = (json or {}).get("method")
        if method == "getTokenSupply":
            if self.dec_fail:
                return FakeResponse({}, status_code=500)
            return FakeResponse(
                {"result": {"value": {"decimals": self.decimals}}})
        if method == "simulateTransaction":
            return FakeResponse({"result": {"value": {"err": self.sim_err}}})
        # jupiter /swap
        return FakeResponse({"swapTransaction": self.swap_tx})


def _quote_al(out_amount="500000000000", impact="0.0001"):
    # 100 USDC -> 500 token (dec 9) => fiyat 0.2
    return {
        "inAmount": "100000000", "outAmount": out_amount,
        "priceImpactPct": impact,
        "routePlan": [{"swapInfo": {"label": "Orca"}}],
    }


def _quote_sat(out_amount="99000000", impact="0.0001"):
    # 500 token -> 99 USDC => fiyat 0.198
    return {
        "inAmount": "500000000000", "outAmount": out_amount,
        "priceImpactPct": impact,
        "routePlan": [{"swapInfo": {"label": "Raydium"}}],
    }


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DRYRUN_PUBKEY", raising=False)
    monkeypatch.delenv("LIVE_UNLOCKED", raising=False)
    monkeypatch.delenv("SOL_KEYPAIR_PATH", raising=False)
    return tmp_path


def _fills(tmp_path):
    p = tmp_path / "dryrun_fills.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines()]


def _cuzdan_dosyasi(tmp_path, monkeypatch, icerik=None):
    """Gecici gecerli keypair dosyasi (solana-keygen JSON dizi formati) kurar."""
    from solders.keypair import Keypair

    kp = Keypair()
    p = tmp_path / "test_keypair.json"
    p.write_text(icerik if icerik is not None else json.dumps(list(bytes(kp))))
    monkeypatch.setenv("SOL_KEYPAIR_PATH", str(p))
    return kp


# ---- quote parse -----------------------------------------------------------------------

def test_quote_al_parse(data_dir):
    br = DryrunExecBroker(http=FakeClient(quote=_quote_al()))
    q = br.get_quote(TOK, "al", 100.0)
    assert q is not None
    assert q.fiyat == pytest.approx(0.2)
    assert q.miktar_token == pytest.approx(500.0)
    assert q.route == ["Orca"]


def test_quote_sat_parse(data_dir):
    br = DryrunExecBroker(http=FakeClient(quote=_quote_sat()))
    q = br.get_quote(TOK, "sat", 500.0)
    assert q is not None
    assert q.fiyat == pytest.approx(0.198)


def test_quote_decimals_cache(data_dir):
    fake = FakeClient(quote=_quote_al())
    br = DryrunExecBroker(http=fake)
    br.get_quote(TOK, "al", 100.0)
    br.get_quote(TOK, "al", 100.0)
    dec_calls = [c for c in fake.calls
                 if c[0] == "post" and c[2].get("method") == "getTokenSupply"]
    assert len(dec_calls) == 1


# ---- hata yollari ----------------------------------------------------------------------

def test_quote_yok_on_error(data_dir):
    br = DryrunExecBroker(http=FakeClient(quote_exc=RuntimeError("down")))
    q, neden = br._quote(TOK, "al", 100.0, 50)
    assert q is None and neden == "quote_yok"


def test_route_yok_on_400(data_dir):
    resp = FakeResponse({}, status_code=400, text="COULD_NOT_FIND_ANY_ROUTE")
    exc = httpx.HTTPStatusError("400", request=None, response=resp)
    br = DryrunExecBroker(http=FakeClient(quote_exc=exc))
    q, neden = br._quote(TOK, "al", 100.0, 50)
    assert q is None and neden == "route_yok"


def test_decimals_yok(data_dir):
    br = DryrunExecBroker(http=FakeClient(quote=_quote_al(), dec_fail=True))
    q, neden = br._quote(TOK, "al", 100.0, 50)
    assert q is None and neden == "decimals_yok"


def test_execute_fill_yok_motor_cokmez(data_dir):
    br = DryrunExecBroker(http=FakeClient(quote_exc=RuntimeError("down")))
    fill = br.execute(ExecOrder(engine="T", yon="al", token_address=TOK,
                                usd=100.0, ref_fiyat=0.2))
    assert fill.ok is False and fill.neden == "quote_yok"
    assert _fills(data_dir)[0]["neden"] == "quote_yok"


def test_execute_slippage_asimi(data_dir):
    br = DryrunExecBroker(http=FakeClient(quote=_quote_al(impact="0.02")))  # 200 bps
    fill = br.execute(ExecOrder(engine="T", yon="al", token_address=TOK,
                                usd=100.0, ref_fiyat=0.2, slippage_bps=50))
    assert fill.ok is False and fill.neden == "slippage_asimi"


# ---- dryrun execute --------------------------------------------------------------------

def test_dryrun_execute_ok_ve_kiyas_logu(data_dir):
    br = DryrunExecBroker(http=FakeClient(quote=_quote_al()))
    fill = br.execute(ExecOrder(engine="M1", yon="al", token_address=TOK,
                                usd=100.0, ref_fiyat=0.199))
    assert fill.ok is True and fill.tx_id is None
    assert fill.fiyat == pytest.approx(0.2)
    rows = _fills(data_dir)
    assert rows[0]["tur"] == "dryrun"
    assert rows[0]["fark_bps"] == pytest.approx((0.2 / 0.199 - 1) * 1e4, abs=0.5)
    assert rows[0]["route"] == ["Orca"]
    assert rows[0]["sim"] is None  # DRYRUN_PUBKEY yok: simulasyon atlanir


def test_dryrun_simulasyon_ok(data_dir, monkeypatch):
    monkeypatch.setenv("DRYRUN_PUBKEY", "PUB" * 10)
    br = DryrunExecBroker(http=FakeClient(quote=_quote_al()))
    fill = br.execute(ExecOrder(engine="M1", yon="al", token_address=TOK,
                                usd=100.0, ref_fiyat=0.2))
    assert fill.ok is True and fill.tx_id is None
    assert _fills(data_dir)[0]["sim"] == "ok"


def test_dryrun_simulasyon_fail(data_dir, monkeypatch):
    monkeypatch.setenv("DRYRUN_PUBKEY", "PUB" * 10)
    br = DryrunExecBroker(http=FakeClient(quote=_quote_al(),
                                          sim_err={"InstructionError": [0, "x"]}))
    fill = br.execute(ExecOrder(engine="M1", yon="al", token_address=TOK,
                                usd=100.0, ref_fiyat=0.2))
    assert fill.ok is False and fill.neden == "sim_fail"


def test_dryrun_asla_imza_atmaz(data_dir, monkeypatch):
    """Dryrun tam zincirde bile sign_and_send'e ASLA ulasmamali."""
    monkeypatch.setenv("DRYRUN_PUBKEY", "PUB" * 10)

    def _patlat(*a, **k):
        raise AssertionError("dryrun imza atmaya calisti!")

    monkeypatch.setattr("hibrit_trader.jupiter.sign_and_send", _patlat)
    br = DryrunExecBroker(http=FakeClient(quote=_quote_al()))
    fill = br.execute(ExecOrder(engine="M1", yon="al", token_address=TOK,
                                usd=100.0, ref_fiyat=0.2))
    assert fill.ok is True


# ---- live cift kilit: ZORUNLU test -----------------------------------------------------

def test_live_kilit_env_yoksa_acilmaz(data_dir):
    assert live_kilit_acik() is False
    with pytest.raises(RuntimeError, match="kilidi kapali"):
        make_exec_broker("live")


def test_live_kilit_sadece_env_yetmez(data_dir, monkeypatch):
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    assert live_kilit_acik() is False  # LIVE_ONAY dosyasi yok
    with pytest.raises(RuntimeError):
        make_exec_broker("live")


def test_live_kilit_yanlis_icerik_yetmez(data_dir, monkeypatch):
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    (data_dir / "LIVE_ONAY").write_text("evet\n", encoding="utf-8")
    assert live_kilit_acik() is False
    with pytest.raises(RuntimeError):
        make_exec_broker("live")


def test_live_kilit_sadece_dosya_yetmez(data_dir, monkeypatch):
    monkeypatch.delenv("LIVE_UNLOCKED", raising=False)
    (data_dir / "LIVE_ONAY").write_text("canli-islem-onayliyorum",
                                        encoding="utf-8")
    assert live_kilit_acik() is False
    with pytest.raises(RuntimeError):
        make_exec_broker("live")


def test_live_execute_kilit_dusunce_reddeder(data_dir, monkeypatch):
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    onay = data_dir / "LIVE_ONAY"
    onay.write_text("canli-islem-onayliyorum", encoding="utf-8")
    _cuzdan_dosyasi(data_dir, monkeypatch)
    br = make_exec_broker("live", http=FakeClient(quote=_quote_al()))
    assert isinstance(br, LiveExecBroker)
    onay.unlink()  # kilit calisirken kapanirsa
    fill = br.execute(ExecOrder(engine="T", yon="al", token_address=TOK,
                                usd=10.0, ref_fiyat=0.2))
    assert fill.ok is False and fill.neden == "kilit_kapali"


def test_live_cuzdan_yoksa_fill_yok(data_dir, monkeypatch):
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    (data_dir / "LIVE_ONAY").write_text("canli-islem-onayliyorum",
                                        encoding="utf-8")
    _cuzdan_dosyasi(data_dir, monkeypatch)
    br = make_exec_broker("live", http=FakeClient(quote=_quote_al()))
    (data_dir / "test_keypair.json").unlink()  # kurulduktan sonra cuzdan kaybolur
    fill = br.execute(ExecOrder(engine="T", yon="al", token_address=TOK,
                                usd=10.0, ref_fiyat=0.2))
    assert fill.ok is False and fill.neden == "cuzdan_yok"


# ---- cuzdan korumasi -------------------------------------------------------------------

def test_cuzdan_dryrun_modda_yuklenemez(data_dir):
    with pytest.raises(RuntimeError, match="sadece live"):
        _cuzdan_yukle("dryrun")
    with pytest.raises(RuntimeError, match="sadece live"):
        _cuzdan_yukle("paper")


def test_cuzdan_repo_ici_yol_reddedilir(data_dir, monkeypatch):
    repo_ici = str(broker.Path(broker.__file__).resolve().parents[2] / "key.txt")
    monkeypatch.setenv("SOL_KEYPAIR_PATH", repo_ici)
    with pytest.raises(RuntimeError, match="repo icinde olamaz"):
        _cuzdan_yukle("live")


# ---- cuzdan formatlari (12 Tem InvalidChar(91) otopsisi) ---------------------------------

def test_load_keypair_json_dizi_formati(data_dir):
    from solders.keypair import Keypair

    from hibrit_trader.jupiter import load_keypair

    kp = Keypair()
    yuklenen = load_keypair(json.dumps(list(bytes(kp))))
    assert yuklenen.pubkey() == kp.pubkey()


def test_load_keypair_base58_formati(data_dir):
    from solders.keypair import Keypair

    from hibrit_trader.jupiter import load_keypair

    kp = Keypair()
    yuklenen = load_keypair(str(kp))
    assert yuklenen.pubkey() == kp.pubkey()


def test_load_keypair_kisa_json_dizi_reddedilir(data_dir):
    from hibrit_trader.jupiter import load_keypair

    with pytest.raises(ValueError, match="64 bayt"):
        load_keypair("[1, 2, 3]")


def test_cuzdan_bozuk_format_cuzdan_yok_hatasi(data_dir, monkeypatch):
    _cuzdan_dosyasi(data_dir, monkeypatch, icerik="[1, 2, 3]")
    with pytest.raises(RuntimeError, match="cuzdan_yok: keypair cozumlenemedi"):
        _cuzdan_yukle("live")


def test_cuzdan_json_dizi_dosyadan_yuklenir(data_dir, monkeypatch):
    kp = _cuzdan_dosyasi(data_dir, monkeypatch)
    assert _cuzdan_yukle("live").pubkey() == kp.pubkey()


def test_live_init_cuzdan_probu_pubkey_loglar(data_dir, monkeypatch, caplog):
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    (data_dir / "LIVE_ONAY").write_text("canli-islem-onayliyorum", encoding="utf-8")
    kp = _cuzdan_dosyasi(data_dir, monkeypatch)
    br = make_exec_broker("live", http=FakeClient())
    assert isinstance(br, LiveExecBroker)
    assert str(kp.pubkey()) in caplog.text   # pubkey boot'ta loglanir
    assert str(kp) not in caplog.text        # gizli anahtar ASLA loglanmaz


def test_live_init_cuzdan_bozuksa_kurulamaz(data_dir, monkeypatch):
    # kalici konfig hatasi ilk trade'de degil boot'ta patlar -> motor exec_arizali olur
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    (data_dir / "LIVE_ONAY").write_text("canli-islem-onayliyorum", encoding="utf-8")
    _cuzdan_dosyasi(data_dir, monkeypatch, icerik="[1, 2, 3]")
    with pytest.raises(RuntimeError, match="cuzdan_yok"):
        make_exec_broker("live", http=FakeClient())


# ---- fabrika ---------------------------------------------------------------------------

def test_fabrika_varsayilan_paper(data_dir, monkeypatch):
    monkeypatch.delenv("BROKER_MODE", raising=False)
    assert isinstance(make_exec_broker(), PaperExecBroker)


def test_fabrika_dryrun(data_dir):
    assert isinstance(make_exec_broker("dryrun", http=FakeClient()), DryrunExecBroker)


def test_paper_execute_ref_fiyat(data_dir):
    fill = PaperExecBroker().execute(
        ExecOrder(engine="T", yon="al", token_address=TOK, usd=100.0, ref_fiyat=0.5))
    assert fill.ok and fill.fiyat == 0.5 and fill.miktar_token == pytest.approx(200.0)


# ---- golge olcum -----------------------------------------------------------------------

def test_golge_worker_kiyas_yazar(data_dir):
    br = DryrunExecBroker(http=FakeClient(quote=_quote_al()))
    _golge_worker("M1", "al", TOK, 0.199, 100.0, None, broker=br)
    rows = _fills(data_dir)
    assert rows[0]["tur"] == "golge" and rows[0]["engine"] == "M1"
    assert rows[0]["jup_fiyat"] == pytest.approx(0.2)
    assert rows[0]["fark_bps"] == pytest.approx((0.2 / 0.199 - 1) * 1e4, abs=0.5)


def test_golge_worker_hata_yutulur(data_dir):
    br = DryrunExecBroker(http=FakeClient(quote_exc=RuntimeError("down")))
    _golge_worker("M2", "sat", TOK, 1.0, None, 500.0, broker=br)
    assert _fills(data_dir)[0]["neden"] == "quote_yok"


def test_golge_olcum_kapaliyken_thread_acmaz(data_dir, monkeypatch):
    monkeypatch.setenv("BROKER_GOLGE_OLCUM", "0")
    acilan = []
    monkeypatch.setattr(broker.threading, "Thread",
                        lambda *a, **k: acilan.append(1))
    broker.golge_olcum("M1", "al", TOK, 1.0, usd=100.0)
    assert acilan == []


# ---- live SOL swap secimi (ASAMA 0, 11 Tem: kasa SOL, alim SOL->token) ------------------

def test_live_execute_sol_swap_al_ve_sat(data_dir, monkeypatch):
    monkeypatch.delenv("LIVE_MAX_USD", raising=False)
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    (data_dir / "LIVE_ONAY").write_text("canli-islem-onayliyorum", encoding="utf-8")
    _cuzdan_dosyasi(data_dir, monkeypatch)
    br = make_exec_broker("live", http=FakeClient())
    monkeypatch.setattr(broker, "_cuzdan_yukle", lambda mode: object())
    monkeypatch.setattr(br, "_decimals", lambda mint: 9)
    cagri = []

    def sahte_al(http, rpc, kp, mint, usd, bps):
        cagri.append(("sol_to_token", usd, bps))
        return {"signature": "SIGAL", "in_amount": 1_250_000_000,
                "out_amount": 500 * 10 ** 9, "cost_usd": 100.0,
                "sol_price_usd": 80.0}

    def sahte_sat(http, rpc, kp, mint, amount_raw, bps):
        cagri.append(("token_to_sol", amount_raw, bps))
        return {"signature": "SIGSAT", "in_amount": amount_raw,
                "out_amount": 1_237_500_000, "proceeds_usd": 99.0,
                "sol_price_usd": 80.0}

    monkeypatch.setattr("hibrit_trader.jupiter.swap_sol_to_token", sahte_al)
    monkeypatch.setattr("hibrit_trader.jupiter.swap_token_to_sol", sahte_sat)

    al = br.execute(ExecOrder(engine="V7", yon="al", token_address=TOK,
                              usd=100.0, ref_fiyat=0.2))
    assert al.ok and al.tx_id == "SIGAL"
    assert al.miktar_token == pytest.approx(500.0)
    assert al.fiyat == pytest.approx(0.2)  # cost_usd / miktar

    sat = br.execute(ExecOrder(engine="V7", yon="sat", token_address=TOK,
                               amount_token=500.0, ref_fiyat=0.2))
    assert sat.ok and sat.tx_id == "SIGSAT"
    assert sat.fiyat == pytest.approx(99.0 / 500.0)  # proceeds_usd / miktar
    assert cagri == [("sol_to_token", 100.0, 50),
                     ("token_to_sol", 500 * 10 ** 9, 50)]


def test_live_max_usd_tavani_sadece_alimi_kirpar(data_dir, monkeypatch):
    monkeypatch.setenv("LIVE_MAX_USD", "25")
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    (data_dir / "LIVE_ONAY").write_text("canli-islem-onayliyorum", encoding="utf-8")
    _cuzdan_dosyasi(data_dir, monkeypatch)
    br = make_exec_broker("live", http=FakeClient())
    monkeypatch.setattr(broker, "_cuzdan_yukle", lambda mode: object())
    monkeypatch.setattr(br, "_decimals", lambda mint: 9)
    cagri = []

    def sahte_al(http, rpc, kp, mint, usd, bps):
        cagri.append(("sol_to_token", usd, bps))
        return {"signature": "SIGAL", "in_amount": 312_500_000,
                "out_amount": 125 * 10 ** 9, "cost_usd": 25.0,
                "sol_price_usd": 80.0}

    def sahte_sat(http, rpc, kp, mint, amount_raw, bps):
        cagri.append(("token_to_sol", amount_raw, bps))
        return {"signature": "SIGSAT", "in_amount": amount_raw,
                "out_amount": 309_375_000, "proceeds_usd": 24.75,
                "sol_price_usd": 80.0}

    monkeypatch.setattr("hibrit_trader.jupiter.swap_sol_to_token", sahte_al)
    monkeypatch.setattr("hibrit_trader.jupiter.swap_token_to_sol", sahte_sat)

    al = br.execute(ExecOrder(engine="V7", yon="al", token_address=TOK,
                              usd=215.0, ref_fiyat=0.2))
    assert al.ok and al.miktar_token == pytest.approx(125.0)
    assert al.fiyat == pytest.approx(0.2)  # birim fiyat tavandan etkilenmez

    # satis tavana takilmaz: cuzdandaki gercek miktar aynen satilir
    sat = br.execute(ExecOrder(engine="V7", yon="sat", token_address=TOK,
                               amount_token=125.0, ref_fiyat=0.2))
    assert sat.ok and sat.fiyat == pytest.approx(24.75 / 125.0)
    assert cagri == [("sol_to_token", 25.0, 50),
                     ("token_to_sol", 125 * 10 ** 9, 50)]


def test_live_max_usd_bos_veya_sifir_tavan_yok(monkeypatch):
    monkeypatch.delenv("LIVE_MAX_USD", raising=False)
    assert broker._live_max_usd() == 0.0
    monkeypatch.setenv("LIVE_MAX_USD", "0")
    assert broker._live_max_usd() == 0.0
    monkeypatch.setenv("LIVE_MAX_USD", "bozuk")
    assert broker._live_max_usd() == 0.0
    monkeypatch.setenv("LIVE_MAX_USD", "25")
    assert broker._live_max_usd() == 25.0
