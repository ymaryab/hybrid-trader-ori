"""K1 islem akisi: agregat, flush, backpressure, kesif, kanca."""

import base64
import json
import time

import pytest

import hibrit_trader.gozlem.islem_akisi as ia
from hibrit_trader.gozlem.sayim_r2 import SayimR2
from hibrit_trader.gozlem.ortak import Sayaclar


class _Bus:
    def __init__(self):
        self.olaylar = []
        self.yazici = self

    def yayinla_kayipli(self, akis, kind, payload, **kw):
        self.olaylar.append((akis, kind, payload, kw))
        return True

    def yaz(self, akis, kind, payload, **kw):
        self.olaylar.append((akis, kind, payload, kw))


def _kayitli_akis(tmp_path):
    (tmp_path / "gozlem").mkdir(parents=True, exist_ok=True)
    kayit = {"sv": 1, "kayitlar": {"aa" * 8: {
        "tur": "pumpfun_trade", "mint_ofs": 8, "sol_ofs": 40,
        "token_ofs": 48, "user_ofs": None, "is_buy": True,
        "boy_min": 56}}}
    (tmp_path / "gozlem" / "anchor_kayit.json").write_text(
        json.dumps(kayit))
    return ia.IslemAkisi(_Bus(), tmp_path)


def _mint_pump():
    # 'pump' sonekli gecerli 32B mint uret (kaba arama, kucuk uzay)
    from hibrit_trader.gozlem.lp_kilit import _b58
    for i in range(300000):
        b = i.to_bytes(32, "big")
        if _b58(b).endswith("pump"):
            return b
    pytest.skip("pump sonekli mint bulunamadi")


def _ham_mesaj(mint_b, sol=5 * 10**8, tok=10**9, yon="Buy"):
    ham = (b"\xaa" * 8 + mint_b + sol.to_bytes(8, "little")
           + tok.to_bytes(8, "little"))
    log = "Program data: " + base64.b64encode(ham).decode()
    return json.dumps({"params": {"result": {"value": {
        "err": None, "logs": [f"Program log: Instruction: {yon}", log],
        "signature": "S"}}}})


def test_agregat_ve_flush(tmp_path):
    ak = _kayitli_akis(tmp_path)
    mb = _mint_pump()
    for _ in range(3):
        ak._isle_metin(_ham_mesaj(mb))
    assert ak.cozulen == 3 and len(ak.agreg) == 1
    g = next(iter(ak.agreg.values()))
    assert g["n_al"] == 3 and g["sol_al"] == 3 * 5 * 10**8
    assert g["o"] == g["c"] == pytest.approx(0.0005)   # (0.5 SOL)/(1000 tok)
    # flush: dakika kapanmadan yazmaz; erken flush yazar
    assert ak._flush() == 0
    assert ak._flush(erken=True) == 1
    olay = [o for o in ak.bus.olaylar if o[1] == "TradeAggregate"]
    assert olay and olay[0][2]["sv"] == 1
    assert olay[0][3]["token"].endswith("pump")


def test_kesif_bilinmeyen_disc(tmp_path):
    ak = _kayitli_akis(tmp_path)
    mb = _mint_pump()
    ham = b"\xbb" * 8 + mb + (10**8).to_bytes(8, "little")
    msg = json.dumps({"params": {"result": {"value": {
        "err": None, "logs": [
            "Program log: Instruction: Sell",
            "Program data: " + base64.b64encode(ham).decode()]}}}})
    ak._isle_metin(msg)
    d = ak.kesif["bb" * 8]
    assert d["n"] == 1 and d["sell_n"] == 1
    assert d["mint_ofs"].most_common(1)[0][0] == 8   # pump penceresi bulundu


def test_backpressure_ornekleme(tmp_path, monkeypatch):
    ak = _kayitli_akis(tmp_path)
    monkeypatch.setattr(ia, "KUYRUK_MAX", 100)
    # doluluk > %50 -> N katlanir; dusukse geri iner
    monkeypatch.setattr(ak.q, "qsize", lambda: 60)
    ak._ornekleme_ayarla()
    assert ak.ornekleme_n == 2
    ak._ornekleme_ayarla()
    assert ak.ornekleme_n == 4
    monkeypatch.setattr(ak.q, "qsize", lambda: 5)
    ak._ornekleme_ayarla()
    assert ak.ornekleme_n == 2
    # ornekleme modunda girislerin bir kismi sayilarak dusuruluyor
    ak.ornekleme_n = 4
    for _ in range(8):
        ak.on_ham("Program data: x")
    assert ak.dusen_ornekleme == 6 and len(ak.q._queue) == 2


def test_kuyruk_dolu_dusurur(tmp_path, monkeypatch):
    ak = _kayitli_akis(tmp_path)
    monkeypatch.setattr(ia, "KUYRUK_MAX", 100)
    import asyncio
    ak.q = asyncio.Queue(maxsize=2)
    for _ in range(5):
        ak.on_ham("Program data: x")
    assert ak.q.qsize() == 2 and ak.dusen_kuyruk == 3


def test_sayim_kancasi(tmp_path):
    ak = _kayitli_akis(tmp_path)
    s = SayimR2(_Bus(), Sayaclar(), islem_kanca=ak.on_ham)
    dolgu = "x" * 300
    assert s.on_ham("Instruction: Create" + dolgu) is True   # sayima gider
    assert s.on_ham("Program data: abc" + dolgu) is False    # kancaya gider
    assert ak.q.qsize() == 1
    assert s.on_ham("alakasiz" + dolgu) is False             # kancada elenir
    assert ak.q.qsize() == 1
