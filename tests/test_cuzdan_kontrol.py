"""cuzdan_kontrol testleri: iki keypair formati, RPC parse, sadece-okuma akisi."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "cuzdan_kontrol", Path(__file__).parent.parent / "scripts" / "cuzdan_kontrol.py")
ck = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ck)


def _kp():
    from solders.keypair import Keypair
    return Keypair.from_seed(bytes(range(32)))


def test_keypair_yukle_json_dizisi(tmp_path):
    kp = _kp()
    yol = tmp_path / "kp.json"
    yol.write_text(json.dumps(list(bytes(kp))))
    assert ck.keypair_yukle(yol).pubkey() == kp.pubkey()


def test_keypair_yukle_base58(tmp_path):
    kp = _kp()
    yol = tmp_path / "kp.txt"
    yol.write_text(str(kp) + "\n")
    assert ck.keypair_yukle(yol).pubkey() == kp.pubkey()


class _Resp:
    def __init__(self, veri):
        self._veri = veri

    def raise_for_status(self):
        pass

    def json(self):
        return self._veri


class _Client:
    def __init__(self, cevaplar):
        self.cevaplar = cevaplar
        self.istekler = []

    def post(self, url, json=None):
        self.istekler.append((url, json))
        return _Resp(self.cevaplar[json["method"]])


def test_sol_bakiye_lamports_cevirir():
    c = _Client({"getBalance": {"result": {"value": 1_500_000_000}}})
    assert ck.sol_bakiye(c, "u", "PUB") == pytest.approx(1.5)
    assert c.istekler[0][1]["params"] == ["PUB"]


def test_usdc_bakiye_hesaplari_toplar():
    hesap = lambda ui: {"account": {"data": {"parsed": {"info": {
        "tokenAmount": {"uiAmount": ui}}}}}}
    c = _Client({"getTokenAccountsByOwner": {"result": {"value": [
        hesap(100.5), hesap(None), hesap(2.25)]}}})
    assert ck.usdc_bakiye(c, "u", "PUB") == pytest.approx(102.75)
    assert c.istekler[0][1]["params"][1] == {"mint": ck.USDC_MINT}


def test_rpc_hatasi_yukseltilir():
    c = _Client({"getBalance": {"error": {"code": -32602, "message": "bozuk"}}})
    with pytest.raises(RuntimeError, match="RPC hatasi"):
        ck.sol_bakiye(c, "u", "PUB")


def test_main_dosya_yoksa_iptal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOL_KEYPAIR_PATH", str(tmp_path / "yok.json"))
    with pytest.raises(SystemExit, match="keypair dosyasi yok"):
        ck.main()


def test_main_sadece_okuma_akisi(tmp_path, monkeypatch, capsys):
    kp = _kp()
    yol = tmp_path / "kp.json"
    yol.write_text(json.dumps(list(bytes(kp))))
    monkeypatch.setenv("SOL_KEYPAIR_PATH", str(yol))
    monkeypatch.setenv("SOLANA_RPC_URL", "http://rpc.test")
    c = _Client({
        "getBalance": {"result": {"value": 0}},
        "getTokenAccountsByOwner": {"result": {"value": []}},
    })
    class _Ctx:
        def __init__(self, **kw): pass
        def __enter__(self): return c
        def __exit__(self, *a): return False
    monkeypatch.setattr(ck.httpx, "Client", _Ctx)
    ck.main()
    out = capsys.readouterr().out
    assert str(kp.pubkey()) in out
    assert "SOL bakiyesi : 0.000000" in out
    assert "USDC bakiyesi: 0.00" in out
    assert "UYARI: SOL 0" in out
    # yalniz iki okuma cagrisi, imza/gonderim yok
    metodlar = [j["method"] for _, j in c.istekler]
    assert metodlar == ["getBalance", "getTokenAccountsByOwner"]
