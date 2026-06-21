"""SOLANA_ONLY merkezi kilidi — piyasa verisi yalnız Solana, EVM ağlarına istek yok."""

from __future__ import annotations

import httpx
import pytest

from hibrit_trader import config
from hibrit_trader.config import (
    parse_entry_chains,
    parse_scan_chains,
    restrict_chains,
    solana_only_enabled,
)
from hibrit_trader.scanner import fetch_trending


def test_flag_default_on(monkeypatch):
    monkeypatch.delenv("SOLANA_ONLY", raising=False)
    assert solana_only_enabled() is True


def test_restrict_chains_drops_evm(monkeypatch):
    monkeypatch.setenv("SOLANA_ONLY", "1")
    assert restrict_chains(("solana", "base", "bsc", "arbitrum")) == ("solana",)
    assert restrict_chains(("base",)) == ("solana",)


def test_scan_chains_override_ignored(monkeypatch):
    monkeypatch.setenv("SOLANA_ONLY", "1")
    # Env override EVM ister ama kilit yalnız solana döndürür (sabit davranış)
    assert parse_scan_chains("solana,base,bsc,arbitrum") == ("solana",)
    assert parse_entry_chains("base,arbitrum") == ("solana",)


def test_fetch_trending_evm_early_returns_without_request(monkeypatch):
    monkeypatch.setenv("SOLANA_ONLY", "1")

    def _boom(*a, **k):  # ağ çağrısı yapılırsa test patlar
        raise AssertionError("EVM ağına istek gitti")

    client = httpx.Client()
    monkeypatch.setattr(client, "get", _boom)
    for evm in ("base", "arbitrum", "bsc"):
        assert fetch_trending(client, evm) == []
    client.close()


def test_flag_off_restores_multichain(monkeypatch):
    monkeypatch.setenv("SOLANA_ONLY", "0")
    assert solana_only_enabled() is False
    assert restrict_chains(("solana", "base")) == ("solana", "base")
    assert parse_scan_chains("solana,base") == ("solana", "base")
