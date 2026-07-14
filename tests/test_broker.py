"""Yurutme katmani testleri: quote/parse/hata yollari + live cift kilit + cuzdan korumasi."""

from __future__ import annotations

import json
from types import SimpleNamespace

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
    monkeypatch.delenv("LIVE_TICKET_PCT", raising=False)
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


# ---- sign_and_send onay yolu (12 Tem Signature tip olayi) --------------------------------

class _SahteVT:
    """VersionedTransaction yerine gecer: imza mekanigi test disi."""

    def __init__(self, message=None, keypairs=None):
        self.message = message

    @classmethod
    def from_bytes(cls, raw):
        return cls("mesaj")

    def __bytes__(self):
        return b"imzali-tx"


class _SahteRpc:
    def __init__(self, *, confirm_exc=None, zincirde=False):
        from types import SimpleNamespace

        from solders.signature import Signature

        self._ns = SimpleNamespace
        self.sig = Signature.default()
        self.confirm_exc = confirm_exc
        self.zincirde = zincirde
        self.confirm_arg = None
        self.status_sorgusu = 0

    def send_raw_transaction(self, raw):
        return self._ns(value=self.sig)

    def confirm_transaction(self, sig, commitment=None):
        self.confirm_arg = sig
        if self.confirm_exc is not None:
            raise self.confirm_exc

    def get_signature_statuses(self, sigs):
        self.status_sorgusu += 1
        durum = self._ns(err=None) if self.zincirde else None
        return self._ns(value=[durum])


def _sign_and_send_kur(monkeypatch, rpc):
    from hibrit_trader import jupiter

    monkeypatch.setattr(jupiter, "VersionedTransaction", _SahteVT)
    monkeypatch.setattr(jupiter.time, "sleep", lambda s: None)
    return jupiter


def test_sign_and_send_confirm_signature_nesnesi(data_dir, monkeypatch):
    from solders.signature import Signature

    rpc = _SahteRpc()
    jupiter = _sign_and_send_kur(monkeypatch, rpc)
    sig = jupiter.sign_and_send(rpc, "dHg=", object())
    assert isinstance(rpc.confirm_arg, Signature)  # str degil (12 Tem olayi)
    assert sig == str(rpc.sig)


def test_sign_and_send_onay_hatasi_zincirdeyse_basari(data_dir, monkeypatch):
    rpc = _SahteRpc(confirm_exc=TypeError("onay patladi"), zincirde=True)
    jupiter = _sign_and_send_kur(monkeypatch, rpc)
    assert jupiter.sign_and_send(rpc, "dHg=", object()) == str(rpc.sig)


def test_sign_and_send_onay_hatasi_zincirde_yoksa_belirsiz(data_dir, monkeypatch):
    rpc = _SahteRpc(confirm_exc=TypeError("onay patladi"), zincirde=False)
    jupiter = _sign_and_send_kur(monkeypatch, rpc)
    with pytest.raises(RuntimeError, match="islem_belirsiz"):
        jupiter.sign_and_send(rpc, "dHg=", object())
    assert rpc.status_sorgusu == 3  # karar tek sorguya birakilmaz


# ---- live belirsiz islem kilidi (12 Tem tekrarli alim olayi) ----------------------------

def _live_broker(data_dir, monkeypatch, durum=(40.0, 5.0, 80.0)):
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    (data_dir / "LIVE_ONAY").write_text("canli-islem-onayliyorum", encoding="utf-8")
    _cuzdan_dosyasi(data_dir, monkeypatch)
    # bilet hesabi RPC'ye gitmesin: sahte (mtm, serbest_sol, sol_fiyat)
    # varsayilan MTM $40 x %25 = $10 bilet, serbest bol
    monkeypatch.setattr(broker, "_cuzdan_durum", lambda *a: durum)
    return make_exec_broker("live", http=FakeClient(quote=_quote_al()))


def _al_emri(usd=10.0):
    return ExecOrder(engine="T", yon="al", token_address=TOK,
                     usd=usd, ref_fiyat=0.2)


def test_live_belirsiz_islem_tekrar_denemeyi_yasaklar(data_dir, monkeypatch):
    br = _live_broker(data_dir, monkeypatch)

    def _belirsiz(*a, **k):
        raise RuntimeError("islem_belirsiz:SIGABC")

    monkeypatch.setattr("hibrit_trader.jupiter.swap_sol_to_token", _belirsiz)
    fill = br.execute(_al_emri())
    assert fill.ok is False and fill.neden == "islem_belirsiz"
    # para cikmis olabilir: sonraki emir swap katmanina hic ulasmadan reddedilir
    assert br.execute(_al_emri()).neden == "belirsiz_kilit"


def test_live_islem_hatasi_kilit_acmaz(data_dir, monkeypatch):
    # gonderim oncesi hatalar (quote/preflight) tekrar denemeyi engellemez
    br = _live_broker(data_dir, monkeypatch)

    def _patla(*a, **k):
        raise ValueError("rpc dustu")

    monkeypatch.setattr("hibrit_trader.jupiter.swap_sol_to_token", _patla)
    for _ in range(3):
        assert br.execute(_al_emri()).neden == "islem_hatasi"


# ---- live alim bileti: sabit oran, bilet = MTM x LIVE_TICKET_PCT (12 Tem nihai) ---------

def _swap_yakala(monkeypatch):
    """swap_sol_to_token'a giden usd'yi yakalar, sahte basarili fill doner."""
    gorulen = {}

    def _swap(http, rpc, keypair, token, usd, slippage):
        gorulen["usd"] = usd
        return {"signature": "SIG", "in_amount": 100, "out_amount": 5_000_000_000,
                "input_mint": "SOL", "output_mint": token,
                "cost_usd": usd, "sol_price_usd": 80.0}

    monkeypatch.setattr("hibrit_trader.jupiter.swap_sol_to_token", _swap)
    return gorulen


def test_live_bilet_mtm_orani(data_dir, monkeypatch):
    # bilet = MTM x LIVE_TICKET_PCT, alim aninda taze hesap
    br = _live_broker(data_dir, monkeypatch, durum=(100.0, 5.0, 80.0))
    gorulen = _swap_yakala(monkeypatch)
    fill = br.execute(_al_emri(usd=50.0))
    assert fill.ok is True
    assert gorulen["usd"] == 25.0  # MTM $100 x %25


def test_live_bilet_motor_biletini_yok_sayar(data_dir, monkeypatch):
    # motorun paper bileti (order.usd) canli tarafta kullanilmaz: buyuk de
    # kucuk de olsa canli bilet hep MTM x oran
    br = _live_broker(data_dir, monkeypatch, durum=(100.0, 5.0, 80.0))
    gorulen = _swap_yakala(monkeypatch)
    assert br.execute(_al_emri(usd=5.0)).ok is True
    assert gorulen["usd"] == 25.0
    assert br.execute(_al_emri(usd=500.0)).ok is True
    assert gorulen["usd"] == 25.0


def test_live_bilet_hesap_yok_reddeder(data_dir, monkeypatch):
    # cuzdan durumu hesaplanamadi: canli alim yok (fail-closed)
    br = _live_broker(data_dir, monkeypatch, durum=None)
    gorulen = _swap_yakala(monkeypatch)
    fill = br.execute(_al_emri(usd=50.0))
    assert fill.ok is False and fill.neden == "bilet_hesap_yok"
    assert "usd" not in gorulen  # swap katmanina hic ulasilmadi


def test_live_bilet_sifir_mtm_reddeder(data_dir, monkeypatch):
    # MTM 0 ise bilet 0: gecersiz, alim reddedilir
    br = _live_broker(data_dir, monkeypatch, durum=(0.0, 5.0, 80.0))
    gorulen = _swap_yakala(monkeypatch)
    fill = br.execute(_al_emri())
    assert fill.ok is False and fill.neden == "bilet_hesap_yok"
    assert "usd" not in gorulen


def test_live_yetersiz_serbest_reddeder(data_dir, monkeypatch):
    # bilet $25 -> 0.3125 SOL + 0.05 gaz rezervi = 0.3625 > serbest 0.30
    br = _live_broker(data_dir, monkeypatch, durum=(100.0, 0.30, 80.0))
    gorulen = _swap_yakala(monkeypatch)
    fill = br.execute(_al_emri())
    assert fill.ok is False and fill.neden == "yetersiz_serbest"
    assert "usd" not in gorulen


def test_live_serbest_gaz_rezervi_sinirinda_gecer(data_dir, monkeypatch):
    # 0.37 SOL >= 0.3625 gereksinim: alim gecer
    br = _live_broker(data_dir, monkeypatch, durum=(100.0, 0.37, 80.0))
    gorulen = _swap_yakala(monkeypatch)
    assert br.execute(_al_emri()).ok is True
    assert gorulen["usd"] == 25.0


def test_cuzdan_durum_hesabi(data_dir, monkeypatch):
    from types import SimpleNamespace

    # serbest SOL 0.2 x $80 = $16 + canli poz 25000 x 0.00025 = $6.25
    (data_dir / "v7_state.json").write_text(json.dumps({"positions": [
        {"canli_miktar": 25000.0, "last_price": 0.00025},
        {"amount_token": 100.0, "last_price": 1.0},  # canli degil, sayilmaz
    ]}))
    rpc = SimpleNamespace(
        get_balance=lambda pk: SimpleNamespace(value=200_000_000))
    monkeypatch.setattr("hibrit_trader.jupiter.fetch_sol_price_usd",
                        lambda c, fallback=0.0: 80.0)
    mtm, serbest_sol, fiyat = broker._cuzdan_durum(None, rpc, "PUB")
    assert mtm == pytest.approx(22.25)
    assert serbest_sol == pytest.approx(0.2)
    assert fiyat == pytest.approx(80.0)


def test_cuzdan_durum_fiyat_yoksa_none(data_dir, monkeypatch):
    from types import SimpleNamespace

    rpc = SimpleNamespace(
        get_balance=lambda pk: SimpleNamespace(value=200_000_000))
    monkeypatch.setattr("hibrit_trader.jupiter.fetch_sol_price_usd",
                        lambda c, fallback=0.0: 0.0)
    assert broker._cuzdan_durum(None, rpc, "PUB") is None


def test_cuzdan_durum_bakiye_hatasi_none(data_dir, monkeypatch):
    from types import SimpleNamespace

    def _patla(pk):
        raise ValueError("rpc dustu")

    rpc = SimpleNamespace(get_balance=_patla)
    assert broker._cuzdan_durum(None, rpc, "PUB") is None


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
    # MTM $400 x %25 = $100 bilet
    monkeypatch.setattr(broker, "_cuzdan_durum", lambda *a: (400.0, 5.0, 80.0))
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    (data_dir / "LIVE_ONAY").write_text("canli-islem-onayliyorum", encoding="utf-8")
    _cuzdan_dosyasi(data_dir, monkeypatch)
    br = make_exec_broker("live", http=FakeClient())
    monkeypatch.setattr(broker, "_cuzdan_yukle",
                        lambda mode: SimpleNamespace(pubkey=lambda: "PUB"))
    monkeypatch.setattr(br, "_decimals", lambda mint: 9)
    monkeypatch.setattr(broker, "_zincir_dolum", lambda *a, **k: None)
    monkeypatch.setattr(broker, "_zincir_token_bakiye", lambda *a, **k: None)
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


def test_live_bilet_sadece_alimi_boyutlar(data_dir, monkeypatch):
    # alim bileti MTM x %25 = $25; satis bilete bakmaz, gercek miktari satar
    monkeypatch.setattr(broker, "_cuzdan_durum", lambda *a: (100.0, 2.0, 80.0))
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    (data_dir / "LIVE_ONAY").write_text("canli-islem-onayliyorum", encoding="utf-8")
    _cuzdan_dosyasi(data_dir, monkeypatch)
    br = make_exec_broker("live", http=FakeClient())
    monkeypatch.setattr(broker, "_cuzdan_yukle",
                        lambda mode: SimpleNamespace(pubkey=lambda: "PUB"))
    monkeypatch.setattr(br, "_decimals", lambda mint: 9)
    monkeypatch.setattr(broker, "_zincir_dolum", lambda *a, **k: None)
    monkeypatch.setattr(broker, "_zincir_token_bakiye", lambda *a, **k: None)
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
    assert al.fiyat == pytest.approx(0.2)  # birim fiyat biletten etkilenmez

    # satis bilete takilmaz: cuzdandaki gercek miktar aynen satilir
    sat = br.execute(ExecOrder(engine="V7", yon="sat", token_address=TOK,
                               amount_token=125.0, ref_fiyat=0.2))
    assert sat.ok and sat.fiyat == pytest.approx(24.75 / 125.0)
    assert cagri == [("sol_to_token", 25.0, 50),
                     ("token_to_sol", 125 * 10 ** 9, 50)]


def test_live_ticket_pct_varsayilan_ve_bozuk(monkeypatch):
    # varsayilan 25; bozuk veya bos deger emniyetli varsayilana duser
    monkeypatch.delenv("LIVE_TICKET_PCT", raising=False)
    assert broker._live_ticket_pct() == 25.0
    monkeypatch.setenv("LIVE_TICKET_PCT", "bozuk")
    assert broker._live_ticket_pct() == 25.0
    monkeypatch.setenv("LIVE_TICKET_PCT", "")
    assert broker._live_ticket_pct() == 25.0
    monkeypatch.setenv("LIVE_TICKET_PCT", "10")
    assert broker._live_ticket_pct() == 10.0


# ---- zincir gercegi (14 Tem olayi: dolum kaydi ve satis miktari) ------------------------

class RpcFake:
    """Tek amacli RPC istemcisi: method -> hazir cevap ya da Exception."""

    def __init__(self, cevaplar):
        self.cevaplar = cevaplar

    def post(self, url, json=None, timeout=None):
        c = self.cevaplar[json["method"]]
        if isinstance(c, Exception):
            raise c
        return FakeResponse(c)


def _tx_sonucu(pre, post):
    def _bals(cift):
        return [{"owner": o, "mint": TOK,
                 "uiTokenAmount": {"uiAmount": mik}} for o, mik in cift]
    return {"result": {"meta": {"preTokenBalances": _bals(pre),
                                "postTokenBalances": _bals(post)}}}


def test_satis_miktari_karari():
    assert broker._satis_miktari(100.0, None) == 100.0  # RPC okunamadi: kayit
    assert broker._satis_miktari(100.0, 0.0) is None    # zincirde yok: red
    assert broker._satis_miktari(100.0, 60.0) == 60.0   # eksik: zincire in
    assert broker._satis_miktari(100.0, 150.0) == 100.0 # fazla: kayit kadar


def test_zincir_dolum_eksik_ve_kirinti_sismez():
    # cuzdanda onceden 20 kirinti var, tx 464.5 dolum getirdi; baska owner karisir
    fc = RpcFake({"getTransaction": _tx_sonucu(
        pre=[("PUB", 20.0), ("BASKA", 5.0)],
        post=[("PUB", 484.5), ("BASKA", 40.5)])})
    assert broker._zincir_dolum(fc, "SIG", "PUB", TOK) == pytest.approx(464.5)


def test_zincir_dolum_fazla():
    fc = RpcFake({"getTransaction": _tx_sonucu(pre=[], post=[("PUB", 505.0)])})
    assert broker._zincir_dolum(fc, "SIG", "PUB", TOK) == pytest.approx(505.0)


def test_zincir_dolum_okunamazsa_none():
    fc = RpcFake({"getTransaction": {"result": None}})
    assert broker._zincir_dolum(fc, "SIG", "PUB", TOK,
                                deneme=2, bekleme_sn=0.0) is None
    fc2 = RpcFake({"getTransaction": RuntimeError("rpc down")})
    assert broker._zincir_dolum(fc2, "SIG", "PUB", TOK,
                                deneme=2, bekleme_sn=0.0) is None


def test_zincir_token_bakiye_toplam_ve_hata():
    def _hesap(mik):
        return {"account": {"data": {"parsed": {"info": {
            "tokenAmount": {"uiAmount": mik}}}}}}
    fc = RpcFake({"getTokenAccountsByOwner":
                  {"result": {"value": [_hesap(30.0), _hesap(12.5)]}}})
    assert broker._zincir_token_bakiye(fc, "PUB", TOK) == pytest.approx(42.5)
    fc2 = RpcFake({"getTokenAccountsByOwner": RuntimeError("rpc down")})
    assert broker._zincir_token_bakiye(fc2, "PUB", TOK) is None


def _live_kur(data_dir, monkeypatch):
    monkeypatch.setattr(broker, "_cuzdan_durum", lambda *a: (400.0, 5.0, 80.0))
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    (data_dir / "LIVE_ONAY").write_text("canli-islem-onayliyorum", encoding="utf-8")
    _cuzdan_dosyasi(data_dir, monkeypatch)
    br = make_exec_broker("live", http=FakeClient())
    monkeypatch.setattr(broker, "_cuzdan_yukle",
                        lambda mode: SimpleNamespace(pubkey=lambda: "PUB"))
    monkeypatch.setattr(br, "_decimals", lambda mint: 9)
    return br


def test_live_alim_kaydi_zincir_dolumu(data_dir, monkeypatch):
    # quote 500 der ama zincir 464.5 doldurdu: kayit 464.5 olmali (eksik dolum)
    br = _live_kur(data_dir, monkeypatch)
    monkeypatch.setattr(broker, "_zincir_dolum", lambda *a, **k: 464.5)
    monkeypatch.setattr(
        "hibrit_trader.jupiter.swap_sol_to_token",
        lambda *a: {"signature": "SIGAL", "in_amount": 1_250_000_000,
                    "out_amount": 500 * 10 ** 9, "cost_usd": 100.0,
                    "sol_price_usd": 80.0})
    al = br.execute(ExecOrder(engine="V7", yon="al", token_address=TOK,
                              usd=100.0, ref_fiyat=0.2))
    assert al.ok and al.miktar_token == pytest.approx(464.5)
    assert al.fiyat == pytest.approx(100.0 / 464.5)


def test_live_alim_fazla_dolum_da_zincirden(data_dir, monkeypatch):
    # zincir quote'tan fazla doldurdu: kirinti birakmamak icin kayit 505
    br = _live_kur(data_dir, monkeypatch)
    monkeypatch.setattr(broker, "_zincir_dolum", lambda *a, **k: 505.0)
    monkeypatch.setattr(
        "hibrit_trader.jupiter.swap_sol_to_token",
        lambda *a: {"signature": "SIGAL", "in_amount": 1_250_000_000,
                    "out_amount": 500 * 10 ** 9, "cost_usd": 100.0,
                    "sol_price_usd": 80.0})
    al = br.execute(ExecOrder(engine="V7", yon="al", token_address=TOK,
                              usd=100.0, ref_fiyat=0.2))
    assert al.ok and al.miktar_token == pytest.approx(505.0)


def test_live_alim_dolum_okunamazsa_quote_kalir(data_dir, monkeypatch):
    br = _live_kur(data_dir, monkeypatch)
    monkeypatch.setattr(broker, "_zincir_dolum", lambda *a, **k: None)
    monkeypatch.setattr(
        "hibrit_trader.jupiter.swap_sol_to_token",
        lambda *a: {"signature": "SIGAL", "in_amount": 1_250_000_000,
                    "out_amount": 500 * 10 ** 9, "cost_usd": 100.0,
                    "sol_price_usd": 80.0})
    al = br.execute(ExecOrder(engine="V7", yon="al", token_address=TOK,
                              usd=100.0, ref_fiyat=0.2))
    assert al.ok and al.miktar_token == pytest.approx(500.0)


def test_live_satis_zincire_indirger(data_dir, monkeypatch):
    # kayit 500 ama zincirde 464.5 var (HBULL senaryosu): satis 464.5 ile gider
    br = _live_kur(data_dir, monkeypatch)
    monkeypatch.setattr(broker, "_zincir_token_bakiye", lambda *a, **k: 464.5)
    cagri = []

    def sahte_sat(http, rpc, kp, mint, amount_raw, bps):
        cagri.append(amount_raw)
        return {"signature": "SIGSAT", "in_amount": amount_raw,
                "out_amount": 1_150_000_000, "proceeds_usd": 92.0,
                "sol_price_usd": 80.0}

    monkeypatch.setattr("hibrit_trader.jupiter.swap_token_to_sol", sahte_sat)
    sat = br.execute(ExecOrder(engine="V7", yon="sat", token_address=TOK,
                               amount_token=500.0, ref_fiyat=0.2))
    assert sat.ok and sat.miktar_token == pytest.approx(464.5)
    assert cagri == [int(464.5 * 10 ** 9)]
    assert sat.fiyat == pytest.approx(92.0 / 464.5)


def test_live_satis_zincir_sifirsa_reddeder(data_dir, monkeypatch):
    # manuel satis suphesi: zincirde 0 varsa 1-raw-birim denemesi yapilmaz
    br = _live_kur(data_dir, monkeypatch)
    monkeypatch.setattr(broker, "_zincir_token_bakiye", lambda *a, **k: 0.0)
    cagri = []
    monkeypatch.setattr("hibrit_trader.jupiter.swap_token_to_sol",
                        lambda *a: cagri.append(1))
    sat = br.execute(ExecOrder(engine="V7", yon="sat", token_address=TOK,
                               amount_token=500.0, ref_fiyat=0.2))
    assert not sat.ok and sat.neden == "zincir_bakiye_yok"
    assert cagri == []


# ---- R4: taze pozisyon korumasi + R3a: satis stresi (14 Tem taramasi) -------------------

def test_live_satis_taze_pozisyonda_sifir_reddi_uygulanmaz(data_dir, monkeypatch):
    import time as _t
    # alimdan saniyeler sonra RPC dugumu dolumu henuz gormuyor: kayitla denenir
    br = _live_kur(data_dir, monkeypatch)
    monkeypatch.setattr(broker, "_zincir_token_bakiye", lambda *a, **k: 0.0)
    cagri = []

    def sahte_sat(http, rpc, kp, mint, amount_raw, bps):
        cagri.append(amount_raw)
        return {"signature": "SIGSAT", "in_amount": amount_raw,
                "out_amount": 1_237_500_000, "proceeds_usd": 99.0,
                "sol_price_usd": 80.0}

    monkeypatch.setattr("hibrit_trader.jupiter.swap_token_to_sol", sahte_sat)
    sat = br.execute(ExecOrder(engine="V7", yon="sat", token_address=TOK,
                               amount_token=500.0, ref_fiyat=0.2,
                               acilis_ts=_t.time()))
    assert sat.ok and cagri == [500 * 10 ** 9]
    broker.satis_stresi_temizle()


def test_live_satis_taze_pozisyonda_kirpma_uygulanmaz(data_dir, monkeypatch):
    import time as _t
    br = _live_kur(data_dir, monkeypatch)
    monkeypatch.setattr(broker, "_zincir_token_bakiye", lambda *a, **k: 464.5)
    cagri = []

    def sahte_sat(http, rpc, kp, mint, amount_raw, bps):
        cagri.append(amount_raw)
        return {"signature": "SIGSAT", "in_amount": amount_raw,
                "out_amount": 1_237_500_000, "proceeds_usd": 99.0,
                "sol_price_usd": 80.0}

    monkeypatch.setattr("hibrit_trader.jupiter.swap_token_to_sol", sahte_sat)
    sat = br.execute(ExecOrder(engine="V7", yon="sat", token_address=TOK,
                               amount_token=500.0, ref_fiyat=0.2,
                               acilis_ts=_t.time()))
    assert sat.ok and cagri == [500 * 10 ** 9]
    broker.satis_stresi_temizle()


def test_live_satis_eski_pozisyonda_zincir_esas_kalir(data_dir, monkeypatch):
    import time as _t
    # yas > TAZE_POZISYON_SEC: 14 Tem korumasi aynen calisir (0 -> red)
    br = _live_kur(data_dir, monkeypatch)
    monkeypatch.setattr(broker, "_zincir_token_bakiye", lambda *a, **k: 0.0)
    cagri = []
    monkeypatch.setattr("hibrit_trader.jupiter.swap_token_to_sol",
                        lambda *a: cagri.append(1))
    sat = br.execute(ExecOrder(engine="V7", yon="sat", token_address=TOK,
                               amount_token=500.0, ref_fiyat=0.2,
                               acilis_ts=_t.time() - 120.0))
    assert not sat.ok and sat.neden == "zincir_bakiye_yok"
    assert cagri == []
    broker.satis_stresi_temizle()


def test_satis_stresi_golge_ve_hakemi_susturur(data_dir, monkeypatch):
    # conftest golgeyi kapatir; burada stres kapisi olculdugu icin acilir
    monkeypatch.setenv("BROKER_GOLGE_OLCUM", "1")
    broker.satis_stresi_temizle()
    try:
        acilan = []
        monkeypatch.setattr(broker.threading, "Thread",
                            lambda *a, **k: acilan.append(1))
        broker.satis_stresi_bildir()
        assert broker.satis_stresi_aktif()
        broker.golge_olcum("M1", "al", TOK, 1.0, usd=100.0)
        assert acilan == []
        assert broker.jupiter_referans_fiyat(TOK) is None
        broker.satis_stresi_temizle()
        assert not broker.satis_stresi_aktif()
        broker.golge_olcum("M1", "al", TOK, 1.0, usd=100.0)
        assert acilan == [1]
    finally:
        broker.satis_stresi_temizle()


def test_live_satis_hatasi_stres_baslatir_basari_temizler(data_dir, monkeypatch):
    broker.satis_stresi_temizle()
    try:
        br = _live_kur(data_dir, monkeypatch)
        monkeypatch.setattr(broker, "_zincir_token_bakiye", lambda *a, **k: 500.0)

        def patlayan(*a):
            raise RuntimeError("jupiter dustu")

        monkeypatch.setattr("hibrit_trader.jupiter.swap_token_to_sol", patlayan)
        sat = br.execute(ExecOrder(engine="V7", yon="sat", token_address=TOK,
                                   amount_token=500.0, ref_fiyat=0.2))
        assert not sat.ok and broker.satis_stresi_aktif()

        monkeypatch.setattr(
            "hibrit_trader.jupiter.swap_token_to_sol",
            lambda *a: {"signature": "SIGSAT", "in_amount": 1,
                        "out_amount": 1_237_500_000, "proceeds_usd": 99.0,
                        "sol_price_usd": 80.0})
        sat2 = br.execute(ExecOrder(engine="V7", yon="sat", token_address=TOK,
                                    amount_token=500.0, ref_fiyat=0.2))
        assert sat2.ok and not broker.satis_stresi_aktif()
    finally:
        broker.satis_stresi_temizle()
