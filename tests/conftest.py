"""Pytest — üretim .env ve data/ dosyalarından izole test ortamı."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HIBRIT_BRAIN_AUTO", "0")
os.environ.setdefault("HIBRIT_BRAIN_ENABLED", "0")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """pair_cooldown.json ve agresif .env ayarları testleri bozmasın."""
    monkeypatch.setenv("PAIR_COOLDOWN_FILE", str(tmp_path / "pair_cooldown.json"))
    monkeypatch.setenv("RUGCHECK_STRICT", "1")
    monkeypatch.setenv("PAPER_AGGRESSIVE", "0")
    monkeypatch.delenv("MAX_TOP10_HOLDER_PCT", raising=False)
    # .env kalibrasyon knob'lari test varsayimlarini ezmesin: testler kod
    # varsayilanini sinar, calisan sistemin .env kalibrasyonu degismeden kalir.
    monkeypatch.delenv("PAPER_SLIPPAGE_PCT", raising=False)
    monkeypatch.setenv("ALPHA_RPC_FALLBACK", "1")
    # Telemetri yazimini izole et: testler gercek data/ + logs/'a yazmasin
    # (ornek: killswitch.activate / engine buy-sell -> events/attribution).
    monkeypatch.setattr("hibrit_trader.telemetry.DATA_DIR", tmp_path)
    monkeypatch.setattr("hibrit_trader.telemetry.LOGS_DIR", tmp_path / "logs")
    monkeypatch.setenv("HIBRIT_BRAIN_AUTO", "0")
    monkeypatch.setenv("HIBRIT_BRAIN_ENABLED", "0")
    # Golge olcum testte gercek Jupiter/RPC cagrisi yapmasin
    monkeypatch.setenv("BROKER_GOLGE_OLCUM", "0")
    # Paylasimli cache'ler testler arasi sizmasin (safety 90s, sol_h1 1sa)
    monkeypatch.setattr("hibrit_trader.safety._token_cache", {})
    monkeypatch.setattr(
        "hibrit_trader.momentum_session._sol_h1_paylasimli", (0.0, None)
    )
    # Rejim gecis bildirimi durumu testler arasi sizmasin
    monkeypatch.setattr("hibrit_trader.momentum_session._rejim_bildirim_durum", None)
    # Telegram bildirimi testte gercek mesaj atmasin (killswitch.notify erken doner)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    # Kota kovalari ve tarama backoff izi testler arasi sizmasin
    from hibrit_trader import kota
    kota._reset()
    monkeypatch.setattr("hibrit_trader.scanner._backoff_sec", 0.0)
    monkeypatch.setattr("hibrit_trader.scanner._backoff_bitis", 0.0)


@pytest.fixture(autouse=True)
def mock_session_network(monkeypatch):
    """Engine.tick() ağ/RPC çağrısı yapmasın."""
    monkeypatch.setattr(
        "hibrit_trader.session.smart_money_entry_ok",
        lambda pair, min_wallets, client=None: (True, "test"),
    )
    monkeypatch.setattr(
        "hibrit_trader.session.estimate_wallet_buyers",
        lambda pair, client=None: 5,
    )
    monkeypatch.setattr(
        "hibrit_trader.session.estimate_entry_slippage_pct",
        lambda client, pair, position_usd, settings: (1.0, "ok"),
    )
