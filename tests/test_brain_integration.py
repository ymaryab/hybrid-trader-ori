"""Saito beyin ↔ motor ↔ panel entegrasyon testleri."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from hibrit_trader.brain.orchestrator import BrainVerdict
from hibrit_trader.config import Settings
from hibrit_trader.paper import PaperBroker
from hibrit_trader.session import Engine


def _fake_verdict(**kw) -> BrainVerdict:
    base = dict(
        regime="risk_off",
        action_bias="defensive",
        exit_bias="defensive",
        entry_penalty=12.0,
        counterparty_thesis="Test trap senaryosu",
        predicted_moves=[],
        confidence=70.0,
        macro_avg=35.0,
        fear_greed=82,
        fear_greed_label="Greed",
        scan_count=3,
        tam_isabet_symbols=[],
        top_picks=[],
        sources=["test"],
    )
    base.update(kw)
    return BrainVerdict(**base)


def test_engine_brain_penalty_wires_to_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_AGGRESSIVE", "0")
    settings = Settings(max_position_usd=20.0, paper_aggressive=False)
    broker = PaperBroker(
        state_path=str(tmp_path / "s.json"),
        trades_path=str(tmp_path / "t.jsonl"),
    )
    engine = Engine(settings, broker)
    engine._brain_verdict = _fake_verdict()
    assert engine._brain_entry_penalty() == 12.0
    state = engine.decision_state()
    assert state["brain_penalty"] == 12.0
    brain = engine.brain_state()
    assert brain["ready"] is True
    assert brain["regime"] == "risk_off"


def test_engine_run_brain_now_mock(tmp_path, monkeypatch):
    monkeypatch.setenv("HIBRIT_BRAIN_ENABLED", "1")
    settings = Settings()
    broker = PaperBroker(
        state_path=str(tmp_path / "s.json"),
        trades_path=str(tmp_path / "t.jsonl"),
    )
    engine = Engine(settings, broker)

    def fake_locked() -> None:
        engine._brain_verdict = _fake_verdict(entry_penalty=8.0)
        engine._brain_updated_at = 1.0

    with patch.object(engine, "_run_brain_locked", fake_locked):
        out = engine.run_brain_now()
    assert out["ready"] is True
    assert out["entry_penalty"] == 8.0


@pytest.fixture
def client():
    from hibrit_trader import panel

    return TestClient(panel.app)


def test_api_state_includes_decision_and_brain(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert "decision" in data
    assert "brain" in data
    assert "policy" in data["decision"]
    assert "ready" in data["brain"]


def test_api_brain_run_mock(client, monkeypatch):
    from hibrit_trader import panel

    def fake_run():
        panel.engine._brain_verdict = _fake_verdict()
        panel.engine._brain_updated_at = 1.0
        panel.engine._brain_running = False
        return panel.engine.brain_state()

    monkeypatch.setattr(panel.engine, "request_brain_run", fake_run)
    r = client.post("/api/brain/run")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["action_bias"] == "defensive"


def test_engine_brain_fallback_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HIBRIT_BRAIN_ENABLED", "1")
    settings = Settings()
    broker = PaperBroker(
        state_path=str(tmp_path / "s.json"),
        trades_path=str(tmp_path / "t.jsonl"),
    )
    engine = Engine(settings, broker)

    def boom() -> None:
        raise RuntimeError("network down")

    with patch.object(engine, "_run_brain_locked", boom):
        engine._brain_running = True
        engine._run_brain_job()

    state = engine.brain_state()
    assert state["ready"] is True
    assert state["degraded"] is True
    assert engine._brain_running is False


def test_api_brain_get(client):
    r = client.get("/api/brain")
    assert r.status_code == 200
    assert "ready" in r.json()
