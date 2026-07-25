"""Hayali poz bug kapanisi (25 Tem): broker karari + derin mutabakat.

Yon 1 (fail tx -> hayali poz): _alim_kayit_karari saf karari.
Yon 2 (CASHCOW: basarili alim + persist kaybi): WAL<->defter mutabakati
ve cuzdan yetim taramasi (senkron_bekcisi.derin_mutabakat parcalari).
"""

import json
import time

import pytest

from hibrit_trader.broker import _alim_kayit_karari
import hibrit_trader.senkron_bekcisi as sb


# ---- Yon 1: broker karar tablosu ----------------------------------------

def test_karar_zincir_dolumu_her_zaman_kazanir():
    assert _alim_kayit_karari("hatali", 123.0, 999.0) == ("kayit", 123.0)


def test_karar_hatali_tx_kayit_yok():
    assert _alim_kayit_karari("hatali", None, 999.0) == ("basarisiz", 0.0)
    assert _alim_kayit_karari("hatali", 0.0, 999.0) == ("basarisiz", 0.0)


def test_karar_onayli_ama_dolum_yok_quote():
    assert _alim_kayit_karari("onaylandi", None, 999.0) == ("kayit", 999.0)


def test_karar_belirsiz_veya_yok_uzlastiriciya():
    """Eski davranis quote yazip hayali poz aciyordu; yeni: belirsiz
    (kilit + zincir mutabakati; kayit da CASHCOW da olmaz)."""
    assert _alim_kayit_karari("yok", None, 999.0) == ("belirsiz", 0.0)
    assert _alim_kayit_karari(None, None, 999.0) == ("belirsiz", 0.0)


# ---- Yon 2: senkron derin mutabakat -------------------------------------

@pytest.fixture
def ortam(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "DATA", tmp_path)
    monkeypatch.setattr(sb, "WAL_OLGUNLUK_SEC", 0.0)
    uyarilar = []
    import hibrit_trader.uyari_notify as un
    monkeypatch.setattr(un, "kritik_uyari",
                        lambda b, k, m: uyarilar.append((b, m)))
    return tmp_path, uyarilar


def _wal_yaz(tmp_path, satirlar):
    with open(tmp_path / "canli_fills.jsonl", "w") as f:
        for s in satirlar:
            f.write(json.dumps(s) + "\n")


def test_wal_cashcow_alarmi(ortam):
    tmp_path, uyarilar = ortam
    simdi = time.time()
    _wal_yaz(tmp_path, [
        {"yon": "al", "tx": "TX_YETIM", "token_address": "CASHCOW",
         "ts": simdi - 1000},
        {"yon": "sat", "tx": "TX_SAT", "token_address": "CASHCOW",
         "ts": simdi - 900},
    ])
    sb.wal_defter_mutabakat("canli", {"positions": []})
    assert len(uyarilar) == 1 and "CASHCOW sinifi" in uyarilar[0][1]
    # kalici gorulen: ikinci kosuda tekrar alarm yok
    uyarilar.clear()
    sb.wal_defter_mutabakat("canli", {"positions": []})
    assert uyarilar == []


def test_wal_normal_dolum_alarm_uretmez(ortam):
    tmp_path, uyarilar = ortam
    simdi = time.time()
    _wal_yaz(tmp_path, [
        {"yon": "al", "tx": "TX1", "token_address": "TOK",
         "ts": simdi - 1000},                      # state'te tx ile esli
        {"yon": "al", "tx": "TX2", "token_address": "TOK2",
         "ts": simdi - 2000},                      # trades'te zamanla esli
    ])
    with open(tmp_path / "canli_trades.jsonl", "w") as f:
        f.write(json.dumps({"token_address": "TOK2",
                            "ts": simdi - 1400,
                            "hold_sec": 600}) + "\n")   # giris ~ -2000
    state = {"positions": [{"tx_al": "TX1", "token_address": "TOK",
                            "opened_ts": simdi - 1000}]}
    sb.wal_defter_mutabakat("canli", state)
    assert uyarilar == []


def test_yetim_tarama_iki_tur_teyit(ortam, monkeypatch):
    tmp_path, uyarilar = ortam
    monkeypatch.setattr(sb, "_cuzdan_tum_mintler",
                        lambda: {"YETIMMINT": 5000.0, sb.WSOL: 1.0,
                                 "BILINEN": 3.0})
    with open(tmp_path / "canli_trades.jsonl", "w") as f:
        f.write(json.dumps({"token_address": "BILINEN",
                            "ts": 1.0, "hold_sec": 0}) + "\n")
    sb._yetim_supheli.clear()
    sb.yetim_token_tarama("canli", {"positions": []})
    assert uyarilar == []                          # ilk tur: teyit bekle
    sb.yetim_token_tarama("canli", {"positions": []})
    assert len(uyarilar) == 1
    assert "YETIMMINT" in uyarilar[0][1]           # WSOL/BILINEN degil
    uyarilar.clear()
    sb.yetim_token_tarama("canli", {"positions": []})
    assert uyarilar == []                          # kalici gorulen
