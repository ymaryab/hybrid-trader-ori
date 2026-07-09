"""Panel HTML ve static JS doğrulama."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from hibrit_trader.config import CHAIN_ENTRY_PRIORITY

import pytest
from fastapi.testclient import TestClient

from hibrit_trader import panel

STATIC_JS = Path(__file__).resolve().parents[1] / "src" / "hibrit_trader" / "static" / "panel.js"


@pytest.fixture
def client():
    return TestClient(panel.app)


def test_index_serves_panel_js_reference(client):
    r = client.get("/")
    assert r.status_code == 200
    assert '/static/panel.js' in r.text
    assert '/static/panel.css' in r.text
    assert '/static/panel-quantum.css' in r.text
    assert 'data-theme' in r.text
    assert "onclick=" not in r.text
    assert 'walletHoldings' in r.text
    assert 'holdingsBody' in r.text
    assert 'runScanBtn' in r.text
    assert 'scanPanel' in r.text
    assert 'hudCockpit' in r.text
    assert 'hudMetrics' in r.text
    assert 'qc-dash' in r.text
    assert 'trendPanel' in r.text
    assert 'HYBRID' in r.text
    assert 'Hybrid Trade' in r.text
    assert 'HIBRIT_BOT' not in r.text
    assert 'saitoBrainVisual' in r.text
    assert 'saitoHub' in r.text
    assert 'positionsPanel' in r.text
    assert 'hudPositionUsd' in r.text
    assert 'hudPositionMeta' in r.text
    assert 'positionsTotalBar' in r.text
    assert 'positionsTotalCost' in r.text
    assert 'Pozisyon</span>' in r.text
    assert 'liveSimTags' in r.text
    assert 'phantomBtn' in r.text
    assert 'killBtn' not in r.text


def test_static_panel_quantum_css_served(client):
    r = client.get("/static/panel-quantum.css")
    assert r.status_code == 200
    assert ".qc-dash" in r.text
    assert ".saito-core" in r.text


def test_static_panel_css_served(client):
    r = client.get("/static/panel.css")
    assert r.status_code == 200
    assert "--accent" in r.text
    assert "data-theme" in r.text


def test_static_panel_js_served(client):
    r = client.get("/static/panel.js")
    assert r.status_code == 200
    body = r.text
    assert "connectPhantom" in body
    assert "phantomBtn" in body
    assert "refreshSolPortfolio" in body or "processPhantomPending" in body
    assert "function shortAddr" in body
    assert "runAdvancedScan" in body


def test_api_scan_modes(client):
    r = client.get("/api/scan/modes")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 6
    assert any(m["id"] == "cex" for m in data)


def test_api_scan_run_mock(client, monkeypatch):
    fake = {
        "modes": ["cex"],
        "social": {"enabled": False},
        "count": 1,
        "results": [{"symbol": "BTC", "exchange": "binance", "score": 80, "tam_isabet": True, "reason": "test"}],
    }
    monkeypatch.setattr(panel, "run_advanced_scan", lambda modes, limit=15: fake)
    r = client.post("/api/scan", json={"modes": ["cex"], "limit": 5})
    assert r.status_code == 200
    assert r.json()["results"][0]["symbol"] == "BTC"


def test_api_wallet_portfolio(client, monkeypatch):
    addr = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"

    def fake_portfolio(rpc_map, address):
        assert address == addr
        return {
            "address": address,
            "chains": {
                "base": {
                    "chain": "base",
                    "label": "Base",
                    "tokens": [{"symbol": "ETH", "balance": 1.0, "decimals": 18}],
                    "error": None,
                },
            },
        }

    monkeypatch.setattr(panel, "fetch_portfolio", fake_portfolio)
    r = client.get("/api/wallet/portfolio", params={"address": addr})
    assert r.status_code == 200
    data = r.json()
    assert data["chains"]["base"]["tokens"][0]["symbol"] == "ETH"


def test_api_wallet_portfolio_invalid_address(client):
    r = client.get(
        "/api/wallet/portfolio",
        params={"address": "0xgggggggggggggggggggggggggggggggggggggggg"},
    )
    assert r.status_code == 400


def test_wallet_logo_served(client):
    r = client.get("/static/wallet-logo.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


def test_api_state_live_sim(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert "live_sim" in data
    assert data["live_sim"]["trade_execution"] in ("paper", "live")
    assert "description_tr" in data["live_sim"]


def test_api_state_chain_opportunities(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert "chain_opportunities" in data
    chains = [c["chain"] for c in data["chain_opportunities"]]
    assert chains == sorted(chains, key=lambda c: (
        -max((w["score"] for w in data["watchlist"] if w["chain"] == c), default=0),
        CHAIN_ENTRY_PRIORITY.get(c, 99),
        c,
    ))


def test_panel_js_syntax_valid():
    js = STATIC_JS.read_text()
    node = shutil.which("node")
    if not node:
        pytest.skip("node yok")
    proc = subprocess.run(
        [node, "-e", f"new Function({js!r}); console.log('OK')"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


# ---- PANEL SENKRON: /api/filo tek gercek kaynak ---------------------------------


def test_api_filo_tek_tick_tek_kaynak(client, monkeypatch, tmp_path):
    import json
    import time as _time

    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    opened = _time.time() - 7200
    for i, p in enumerate(("v6", "v7", "x1")):
        (tmp_path / f"{p}_state.json").write_text(json.dumps({
            "balance": 1000.0 + i, "start_balance": 1000.0,
            "realized_pnl": 10.0 + i,
            "positions": [{"pair": "AAA/SOL", "entry_price": 1.0,
                           "last_price": 1.1, "amount_token": 100.0,
                           "opened_ts": opened}],
        }))
        (tmp_path / f"{p}_trades.jsonl").write_text(
            json.dumps({"pair": "AAA/SOL", "exit_reason": "tp",
                        "pnl_usd": 10.0 + i, "ts": opened}) + "\n")
    r = client.get("/api/filo")
    assert r.status_code == 200
    d = r.json()
    assert d["ts"] > 0
    # arsivdekiler aktif filoda yok
    assert "m1" not in d and "m2" not in d
    # kiyas satiri ayni gecisin ozetinden: ikinci okuma/hesap yok, birebir esit
    for p in ("v6", "v7", "x1"):
        assert d["cmp"][p] == d[p]["summary"]["realized_pnl"]
    assert d["cmp"]["v6"] == 10.0
    assert d["cmp"]["x1"] == 12.0
    # equity tek formul (_live_equity): nakit + acik pozisyonun anlik degeri
    assert d["v6"]["summary"]["equity"] == 1110.0
    # ayni 'now': uc motorun pozisyon yasi birebir ayni tick'ten
    ages = {d[p]["positions"][0]["age_min"] for p in ("v6", "v7", "x1")}
    assert len(ages) == 1
    # slot rozeti ayni cevaptan: 7200 sn = 2.0 saat
    assert d["v6"]["summary"]["oldest_slot_hours"] == 2.0
    assert d["v6"]["summary"]["win_rate_pct"] == 100.0


def test_api_motor_endpoint_semasi_korundu(client, monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    (tmp_path / "m2_state.json").write_text(json.dumps(
        {"balance": 500.0, "realized_pnl": 5.0, "positions": []}))
    (tmp_path / "v6_state.json").write_text(json.dumps({"realized_pnl": 7.0}))
    r = client.get("/api/m2")
    assert r.status_code == 200
    s = r.json()["summary"]
    assert s["realized_pnl"] == 5.0
    assert s["v6_realized"] == 7.0
    assert "universe_n" in s
    r2 = client.get("/api/v6")
    assert r2.json()["summary"]["realized_pnl"] == 7.0


def test_momentum_sayfasi_tek_poll_ve_upd_etiketi(client):
    h = client.get("/momentum").text
    assert "/api/filo" in h
    assert "son güncelleme" in h
    for eid in ("v6upd", "v7upd", "x1upd"):
        assert f'id="{eid}"' in h
    # aktif motorlar icin ayri fetch kalmadi: tek gercek kaynak /api/filo
    for eski in ('fetch("/api/v6?', 'fetch("/api/v7?', 'fetch("/api/x1?'):
        assert eski not in h
    # m1/m2 arsivde: bir kez yuklenen arsiv fetch'leri var, upd etiketi yok
    assert 'fetch("/api/m1?' in h and 'fetch("/api/m2?' in h
    assert 'id="m1upd"' not in h and 'id="m2upd"' not in h
    assert "arsivM1" in h and "arsivM2" in h


def test_momentum_yeni_duzen(client):
    h = client.get("/momentum").text
    # ust bar rozetleri
    for eid in ("feedBadge", "rejimBadge"):
        assert f'id="{eid}"' in h
    # besli kart grid: canli + uc bot + v-next, konfigden uretilir
    assert 'id="kartGrid"' in h
    for kid in ("kart-canli", "kart-v6", "kart-v7", "kart-x1", "kart-vnext"):
        assert f'id="{kid}"' in h
    assert "cüzdan: bağlı değil" in h
    assert "yakında" in h
    # kart/chart eslesmesi: her bot kartinin sparkline'i ve charti var
    for p in ("v6", "v7", "x1"):
        assert f'id="spark-{p}"' in h
        assert f'id="eq{p}chart"' in h
        assert f'id="mtm-{p}"' in h
    # tek ortak zaman filtresi + birlesik islem tablosu + arsiv aynen
    assert 'id="eqsyncbtns"' in h
    assert 'id="isltr"' in h
    assert "mfeMaeBar" in h
    assert 'id="arsivBox"' in h
    # JS konfig sunucudan basildi (tek konfig listesi, elle esleme yok)
    assert '"__MOTORLAR__"' not in h
    assert '"id": "v6"' in h


def test_momentum_sayfasi_js_syntax_valid(client):
    h = client.get("/momentum").text
    m = re.search(r"<script>(.*?)</script>", h, re.S)
    assert m, "inline script bulunamadi"
    js = m.group(1)
    node = shutil.which("node")
    if not node:
        pytest.skip("node yok")
    proc = subprocess.run(
        [node, "-e", f"new Function({js!r}); console.log('OK')"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
