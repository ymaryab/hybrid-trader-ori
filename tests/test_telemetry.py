"""Telemetri katmanı — 7 sinyal log yapısının doğrulaması."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hibrit_trader import telemetry
from hibrit_trader.config import Settings
from hibrit_trader.paper import PaperBroker
from hibrit_trader.safety import _evm_decision, _solana_decision
from hibrit_trader.scanner import Pair
from hibrit_trader.session import Engine


def _pair(**kw) -> Pair:
    temel = dict(
        chain="solana", dex="raydium", pool_address="P1", token_address="T1",
        name="TEST / SOL", price_usd=1.0, liquidity_usd=100_000,
        vol_m5=5000, vol_h1=50_000, vol_h24=600_000,
        chg_m5=1.0, chg_h1=8.0, chg_h24=15.0, txns_h1=150,
    )
    temel.update(kw)
    return Pair(**temel)


@pytest.fixture
def tele_dir(tmp_path, monkeypatch):
    """Telemetri dosyalarını izole tmp dizine yönlendir."""
    monkeypatch.setattr(telemetry, "DATA_DIR", tmp_path)
    monkeypatch.setattr(telemetry, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setenv("TELEMETRY_ENABLED", "1")
    return tmp_path


def _broker(tmp_path) -> PaperBroker:
    return PaperBroker(
        state_path=str(tmp_path / "s.json"),
        trades_path=str(tmp_path / "t.jsonl"),
        start_balance_usd=1000.0,
    )


def test_append_read_summarize(tele_dir):
    telemetry.log_decision({"pair": "A/SOL", "reject_type": "filter", "score": 50})
    telemetry.log_decision({"pair": "B/SOL", "reject_type": "no_slot", "score": 60})
    rows = telemetry.read_recent("decisions", 10)
    assert len(rows) == 2
    assert rows[0]["pair"] == "B/SOL"  # en yeni başta (reversed)
    s = telemetry.summarize()
    assert s["decisions_count"] == 2
    assert s["reject_breakdown"] == {"filter": 1, "no_slot": 1}


def test_disabled_is_noop(tele_dir, monkeypatch):
    monkeypatch.setenv("TELEMETRY_ENABLED", "0")
    telemetry.log_attribution({"pair": "X"})
    assert telemetry.read_recent("attribution") == []


def test_trade_carries_entry_context(tmp_path):
    broker = _broker(tmp_path)
    pair = _pair(price_usd=10.0, liquidity_usd=500_000, discovery_source="pump_fun")
    pos = broker.buy(pair, 100.0, 70.0)
    assert pos.trade_id and pos.discovery_source == "pump_fun"
    assert pos.liq_entry == 500_000
    # tutuş boyunca tepe/dip işlendi varsay
    pos.mfe_pct, pos.mae_pct, pos.mfe_at_sec = 25.0, -8.0, 120.0
    pos.entry_regime, pos.entry_fear_greed = "risk_on", 72
    broker.sell(pos, 12.0, 500_000, "hedef")
    line = json.loads(Path(tmp_path / "t.jsonl").read_text().splitlines()[-1])
    assert line["trade_id"] == pos.trade_id
    assert line["source"] == "pump_fun"
    assert line["token_address"] == "T1"
    assert line["regime"] == "risk_on" and line["fear_greed"] == 72
    assert line["mfe_pct"] == 25.0 and line["mae_pct"] == -8.0
    assert line["liq_entry"] == 500_000 and line["liq_exit"] == 500_000
    assert line["pnl_pct"] != 0.0 and line["hold_sec"] >= 0


def test_safety_metrics_solana():
    rep = _solana_decision(
        {"mintable": {"status": "0"}, "freezable": {"status": "0"},
         "holders": [{"percent": "30"}, {"percent": "10"}]}
    )
    assert rep.metrics["top1_holder_pct"] == 30.0
    assert rep.metrics["top10_holder_pct"] == 40.0
    assert rep.metrics["mint_revoked"] is True
    assert rep.metrics["freeze_revoked"] is True


def test_safety_metrics_evm():
    rep = _evm_decision({"is_honeypot": "0", "is_mintable": "1",
                         "holders": [{"percent": "0.2"}]})
    assert rep.metrics["top1_holder_pct"] == 20.0
    assert rep.metrics["mint_revoked"] is False
    assert rep.metrics["honeypot"] is False


def test_update_excursion_tracks_mfe_mae_liq(tele_dir):
    broker = _broker(tele_dir)
    engine = Engine(Settings.from_env(), broker)
    pair = _pair(price_usd=10.0, liquidity_usd=200_000)
    pos = broker.buy(pair, 50.0, 70.0)
    pos.opened_ts = time.time() - 60
    engine._update_excursion(pos, 12.0, pair)   # +~20%
    engine._update_excursion(pos, 9.0, _pair(price_usd=9.0, liquidity_usd=150_000))  # -~10%, düşük likidite
    assert pos.mfe_pct == pytest.approx(20.0, abs=0.5)
    assert pos.mae_pct == pytest.approx(-10.0, abs=0.5)
    assert pos.liq_min == 150_000
    assert pos.mfe_at_sec > 0


def test_runner_time_profile_to_exits(tele_dir):
    broker = _broker(tele_dir)
    engine = Engine(Settings.from_env(), broker)
    pair = _pair(price_usd=10.0, liquidity_usd=200_000)
    pos = broker.buy(pair, 50.0, 70.0)
    pos.opened_ts = time.time() - 120
    for px in (14.0, 9.0, 11.0, 12.0, 13.0, 13.5):  # tepe 14, dip 9; 5'ten fazla tick
        engine._update_excursion(pos, px, pair)
    assert pos.obs_peak_price == 14.0 and pos.obs_trough_price == 9.0
    assert pos.obs_peak_ts_ms > 0 and pos.obs_trough_ts_ms > 0
    assert len(pos.early_ticks) == 5  # ilk ~5 tick ile sınırlı
    assert pos.early_ticks[0] == [0, pos.entry_price]  # entry baseline t=0
    trade = broker.sell(pos, 13.0, 200_000, "hedef")
    engine._log_exit_profile(pos, trade)
    rows = telemetry.read_recent("exits")
    assert len(rows) == 1
    r = rows[0]
    assert r["trade_id"] == pos.trade_id
    assert r["peak_price"] == 14.0 and r["trough_price"] == 9.0
    assert r["time_to_peak_ms"] is not None and r["time_to_peak_ms"] > 0
    assert isinstance(r["early_ticks"], list) and len(r["early_ticks"]) == 5
    # trades.jsonl bu alanları taşımaz (ayrı dosya, append-only korunur)
    tline = json.loads((tele_dir / "t.jsonl").read_text().splitlines()[-1])
    assert "peak_price" not in tline and "early_ticks" not in tline


def test_log_reject_dedup_and_features(tele_dir):
    broker = _broker(tele_dir)
    engine = Engine(Settings.from_env(), broker)
    pair = _pair()
    engine._log_reject(pair, 60.0, "slot dolu", "no_slot")
    engine._log_reject(pair, 60.0, "slot dolu", "no_slot")  # dedup → tek kayıt
    rows = telemetry.read_recent("decisions")
    assert len(rows) == 1
    assert rows[0]["reject_type"] == "no_slot"
    assert "features" in rows[0] and rows[0]["features"]["liquidity"] == 100_000
    assert "open_pos_count" in rows[0] and "deploy_pct" in rows[0]
