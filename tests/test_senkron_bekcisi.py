"""Senkron bekcisi: taze pozisyon atlanir, alarm iki-tur teyidiyle calar."""

from __future__ import annotations

import json
import time

import pytest

import hibrit_trader.senkron_bekcisi as sb


@pytest.fixture(autouse=True)
def bekci_ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("CANLI_MOTOR", "canli")
    monkeypatch.setattr(sb, "DATA", tmp_path)
    monkeypatch.setattr(sb, "_son_uyari", {})
    monkeypatch.setattr(sb, "_supheli", {})
    return tmp_path


def _state_yaz(tmp_path, opened_ts, cm=100.0):
    (tmp_path / "canli_state.json").write_text(json.dumps({"positions": [{
        "pair": "T / SOL", "token_address": "MINT1",
        "canli_miktar": cm, "opened_ts": opened_ts,
    }]}))


def test_taze_pozisyon_atlanir(bekci_ortam, monkeypatch):
    _state_yaz(bekci_ortam, opened_ts=time.time() - 30)  # 30s'lik, taze
    sorgular = []
    monkeypatch.setattr(sb, "_cuzdan_token_bakiye",
                        lambda mint: sorgular.append(mint) or 0.0)
    uyarilar = []
    monkeypatch.setattr(sb, "_uyar", lambda m, k: uyarilar.append(m))
    sb.check_once()
    assert sorgular == []  # RPC'ye hic sorulmadi
    assert uyarilar == []


def test_iki_tur_teyidi(bekci_ortam, monkeypatch):
    _state_yaz(bekci_ortam, opened_ts=time.time() - 600)  # eski pozisyon
    monkeypatch.setattr(sb, "_cuzdan_token_bakiye", lambda mint: 0.0)
    uyarilar = []
    monkeypatch.setattr(sb, "_uyar", lambda m, k: uyarilar.append(m))
    sb.check_once()  # 1. tur: suphe, alarm yok
    assert uyarilar == []
    assert sb._supheli
    sb.check_once()  # 2. tur: ayni uyumsuzluk, alarm calar
    assert len(uyarilar) == 1 and "hayali poz" in uyarilar[0]


def test_duzelen_uyumsuzluk_supheyi_temizler(bekci_ortam, monkeypatch):
    _state_yaz(bekci_ortam, opened_ts=time.time() - 600)
    bakiyeler = iter([0.0, 100.0, 0.0])  # gecici RPC sapmasi senaryosu
    monkeypatch.setattr(sb, "_cuzdan_token_bakiye", lambda mint: next(bakiyeler))
    uyarilar = []
    monkeypatch.setattr(sb, "_uyar", lambda m, k: uyarilar.append(m))
    sb.check_once()  # 1. tur: 0 gorur, suphe
    sb.check_once()  # 2. tur: 100 gorur, suphe SILINIR
    assert sb._supheli == {}
    sb.check_once()  # 3. tur: yine 0, ama teyit sifirdan baslar, alarm yok
    assert uyarilar == []
