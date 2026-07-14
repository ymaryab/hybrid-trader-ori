"""Canlı simülasyon — mock HTTP, gerçek ağ yok."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from hibrit_trader.config import Settings
from hibrit_trader.live_sim import (
    enrich_position,
    fetch_pool_price,
    jupiter_exit_quote,
    live_sim_summary,
)
from hibrit_trader.paper import Position


def _pos(**kw) -> Position:
    base = dict(
        pair_name="TEST / SOL",
        chain="solana",
        token_address="TokenMint111",
        pool_address="PoolAddr111",
        entry_price=0.01,
        amount_token=1000.0,
        cost_usd=15.0,
        opened_at="2026-06-11T00:00:00+00:00",
        entry_score=60.0,
    )
    base.update(kw)
    return Position(**base)


def test_fetch_pool_price_parses_gecko(monkeypatch):
    client = MagicMock()
    resp = MagicMock()
    resp.json.return_value = {"data": {"attributes": {"base_token_price_usd": "0.00421"}}}
    resp.raise_for_status = MagicMock()
    client.get.return_value = resp

    from hibrit_trader import live_sim

    live_sim._pool_cache.clear()
    price = fetch_pool_price(client, "solana", "Pool1")
    assert price == 0.00421


def test_fetch_pool_price_kota_reddi_istek_atmaz(monkeypatch):
    from hibrit_trader import kota, live_sim

    live_sim._pool_cache.clear()
    monkeypatch.setattr(kota, "izin", lambda host, sinif, maliyet=1.0: False)
    client = MagicMock()
    assert fetch_pool_price(client, "solana", "Pool1") is None
    client.get.assert_not_called()


def test_fetch_pool_price_cache_kota_sormaz(monkeypatch):
    # cache hit'te kota tuketilmez (izin patlarsa bile deger doner)
    from hibrit_trader import kota, live_sim

    live_sim._pool_cache.clear()
    live_sim._cache_set(live_sim._pool_cache, "solana:Pool1", 0.5)

    def _patla(*a, **k):
        raise AssertionError("cache hit'te kota sorulmamali")

    monkeypatch.setattr(kota, "izin", _patla)
    assert fetch_pool_price(MagicMock(), "solana", "Pool1") == 0.5


def test_jupiter_exit_quote(monkeypatch):
    client = MagicMock()

    def fake_quote(c, inp, out, amt, bps):
        assert amt == 1_000_000_000  # 1000 tokens × 6 dec
        return {"outAmount": "14950000", "priceImpactPct": "0.12"}

    monkeypatch.setattr("hibrit_trader.live_sim.get_quote", fake_quote)
    monkeypatch.setattr("hibrit_trader.live_sim.fetch_sol_price_usd", lambda c: 1000.0)
    q = jupiter_exit_quote(client, "Mint", 1000.0, 6, 100)
    assert q is not None
    assert q["proceeds_usd"] == 14.95
    assert q["source"] == "jupiter_v6_sol"


def test_enrich_position_paper_fields(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(
        "hibrit_trader.live_sim.fetch_pool_price",
        lambda *a, **k: 0.0045,
    )
    monkeypatch.setattr(
        "hibrit_trader.live_sim.fetch_token_decimals",
        lambda *a, **k: 6,
    )
    monkeypatch.setattr(
        "hibrit_trader.live_sim.jupiter_exit_quote",
        lambda *a, **k: {"proceeds_usd": 14.2, "price_impact_pct": 0.5, "source": "jupiter_v6"},
    )
    s = Settings(paper_live_quotes=True)
    row = enrich_position(_pos(), 0.004, 200_000, client, s)
    assert row["trade_type"] == "paper"
    assert row["prices_live"] is True
    assert row["price_source"] == "geckoterminal_pool"
    assert row["exit_quote_usd"] == 14.2
    assert row["exit_quote_pnl"] == -0.8


def test_live_sim_summary_tr():
    s = Settings(mode="paper", paper_live_quotes=True)
    summary = live_sim_summary(s)
    assert summary["trade_execution"] == "paper"
    assert "Jupiter" in summary["description_tr"]
