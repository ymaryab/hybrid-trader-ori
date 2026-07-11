"""Holder concentration — RugCheck top holder filtre + fail-closed hata yolu."""

import logging
from types import SimpleNamespace

import httpx
import pytest

import hibrit_trader.holder_risk as hr
import hibrit_trader.safety as safety
from hibrit_trader.holder_risk import check_holder_concentration, holder_report_from_payload
from hibrit_trader.safety import SafetyReport


def test_holder_blocks_concentrated():
    data = {
        "topHolders": [
            {"pct": 50.0, "insider": False},
            {"pct": 20.0, "insider": False},
        ]
    }
    rep = holder_report_from_payload(data, genesis_ok=False)
    assert not rep.ok
    assert any("top1" in r for r in rep.reasons)


def test_genesis_relaxes_top1():
    data = {"topHolders": [{"pct": 50.0, "insider": False}]}
    rep = holder_report_from_payload(data, genesis_ok=True)
    assert rep.ok


def test_insider_cluster_blocks_standard():
    data = {
        "topHolders": [
            {"pct": 10, "insider": True},
            {"pct": 9, "insider": True},
            {"pct": 8, "insider": True},
        ]
    }
    rep = holder_report_from_payload(data, genesis_ok=False)
    assert not rep.ok


# ---- fail-closed hata yolu: veri alinamayan token GECMEZ -----------------------------

@pytest.fixture(autouse=True)
def _izole(monkeypatch):
    monkeypatch.setattr(hr, "_CACHE", {})
    monkeypatch.setattr(hr, "_rate_limit", lambda: None)


class _PatlayanClient:
    def __init__(self):
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        raise httpx.ConnectError("api down")


def test_httpx_hatasi_fail_closed_red():
    rep = check_holder_concentration(_PatlayanClient(), "MINT1")
    assert rep.ok is False
    assert rep.kapi == "holder_hata"
    assert "holder verisi alinamadi" in rep.reasons[0]


def test_karar_redi_kapi_etiketi_tasimaz():
    data = {"topHolders": [{"pct": 50.0, "insider": False}]}
    rep = holder_report_from_payload(data, genesis_ok=False)
    assert not rep.ok
    assert rep.kapi == ""  # gercek konsantrasyon RED'i safety_red olarak kalir


def test_hata_raporu_cache_e_yazilmaz():
    c = _PatlayanClient()
    check_holder_concentration(c, "MINT1")
    check_holder_concentration(c, "MINT1")
    assert c.calls == 2  # hata cache'lenmez, kesinti bitince ilk sorgu toparlar
    assert hr._CACHE == {}


def test_basarili_sonuc_600s_cache_te():
    class _OkClient:
        def __init__(self):
            self.calls = 0

        def get(self, *a, **k):
            self.calls += 1
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    c = _OkClient()
    r1 = check_holder_concentration(c, "MINT1")
    r2 = check_holder_concentration(c, "MINT1")
    assert c.calls == 1
    assert r1.ok and r2.ok


def test_hata_warning_loglanir(caplog):
    with caplog.at_level(logging.WARNING, logger="hibrit_trader.holder_risk"):
        check_holder_concentration(_PatlayanClient(), "MINT1")
    assert any("fail-closed" in r.message for r in caplog.records)


def test_safety_check_token_kapi_propagasyonu(monkeypatch):
    monkeypatch.setattr(safety, "_check_goplus", lambda c, ch, t: SafetyReport(ok=True))
    monkeypatch.setattr(
        "hibrit_trader.rugcheck.check_rugcheck_summary",
        lambda c, t: SafetyReport(ok=True),
    )
    monkeypatch.setattr(
        "hibrit_trader.holder_risk.check_holder_concentration",
        lambda c, m, *, genesis_ok=False: SafetyReport(
            ok=False,
            reasons=["holder verisi alinamadi: ConnectError"],
            kapi="holder_hata",
        ),
    )
    rep = safety.check_token(SimpleNamespace(), "solana", "TOKH")
    assert rep.ok is False
    assert rep.kapi == "holder_hata"
