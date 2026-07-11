"""check_token paylasimli cache testleri: TTL 90s, motorlar arasi tek sorgu."""

from __future__ import annotations

import time
from types import SimpleNamespace

import hibrit_trader.safety as safety
from hibrit_trader.safety import SafetyReport, check_token


def _sayacli_taze(monkeypatch, ok=True):
    calls = []

    def fake(client, chain, token_address, *, genesis_ok=False):
        calls.append((chain, token_address, genesis_ok))
        return SafetyReport(ok=ok, reasons=[] if ok else ["honeypot"])

    monkeypatch.setattr(safety, "_check_token_taze", fake)
    return calls


def test_cache_hit_ikinci_sorgu_yapilmaz(monkeypatch):
    calls = _sayacli_taze(monkeypatch)
    c = SimpleNamespace()
    r1 = check_token(c, "solana", "TOK1")
    r2 = check_token(c, "solana", "TOK1")
    assert len(calls) == 1
    assert r1 is r2  # ayni rapor nesnesi cache'ten doner


def test_farkli_tokenlar_ayri_sorgu(monkeypatch):
    calls = _sayacli_taze(monkeypatch)
    c = SimpleNamespace()
    check_token(c, "solana", "TOK1")
    check_token(c, "solana", "TOK2")
    assert len(calls) == 2


def test_genesis_ok_ayri_anahtar(monkeypatch):
    calls = _sayacli_taze(monkeypatch)
    c = SimpleNamespace()
    check_token(c, "solana", "TOK1")
    check_token(c, "solana", "TOK1", genesis_ok=True)
    assert len(calls) == 2


def test_ttl_dolunca_yeniden_sorgu(monkeypatch):
    calls = _sayacli_taze(monkeypatch)
    c = SimpleNamespace()
    check_token(c, "solana", "TOK1")
    ts, rapor = safety._token_cache[("solana", "TOK1", False)]
    safety._token_cache[("solana", "TOK1", False)] = (ts - 91, rapor)
    check_token(c, "solana", "TOK1")
    assert len(calls) == 2


def test_ttl_env_ile_ayarlanir(monkeypatch):
    monkeypatch.setenv("SAFETY_CACHE_TTL_SEC", "0")
    calls = _sayacli_taze(monkeypatch)
    c = SimpleNamespace()
    check_token(c, "solana", "TOK1")
    check_token(c, "solana", "TOK1")
    assert len(calls) == 2


def test_red_sonucu_da_cache_lenir(monkeypatch):
    # Fail-closed korunur: RED karari da TTL boyunca paylasimli doner
    calls = _sayacli_taze(monkeypatch, ok=False)
    c = SimpleNamespace()
    r1 = check_token(c, "solana", "TOKRED")
    r2 = check_token(c, "solana", "TOKRED")
    assert len(calls) == 1
    assert r1.ok is False and r2.ok is False


def test_taze_fonksiyon_karar_mantigi_degismedi(monkeypatch):
    # _check_token_taze GoPlus'a gider; cache sarmalayici karari degistirmez
    monkeypatch.setattr(
        safety, "_check_goplus",
        lambda client, chain, token: SafetyReport(ok=False, reasons=["GoPlus verisi yok"]),
    )
    r = check_token(SimpleNamespace(), "ethereum", "0xABC")
    assert r.ok is False
    assert r.reasons == ["GoPlus verisi yok"]
