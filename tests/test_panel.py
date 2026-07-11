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


def test_momentum_trend_katmani(client):
    h = client.get("/momentum").text
    # aktif bot chartlarinda trend rozeti; bos-durum (canli/vnext) etkilenmez
    for p in ("v6", "v7", "x1"):
        assert f'id="eq{p}trend"' in h
    assert 'id="eqcanlitrend"' not in h and 'id="eqvnexttrend"' not in h
    # rozet CSS + yon renkleri + rozet metni
    assert ".trendroz" in h
    assert "#1D9E75" in h and "#E24B4A" in h
    assert "yükseliş" in h and "düşüş" in h and "$/saat" in h
    # trend hesap fonksiyonlari inline JS'te (kumulatif ortalama)
    for fn in ("renkAlfa", "kumulatifSeri", "trendHiz"):
        assert f"function {fn}" in h, fn
    # eski EMA/segment katmani kaldirildi
    for eski in ("emaSeries", "trendRenkler", "trendPeriyot", "segment:"):
        assert eski not in h, eski
    # ham egri soluk; trend duz acik ton, ustte ve kalin
    assert "renkAlfa(renk,0.55)" in h
    assert '"#e6edf3"' in h
    assert "borderWidth:3" in h and 'borderCapStyle:"round"' in h
    assert "order:-1" in h
    # tooltip yalniz ham seriden
    assert "it.datasetIndex===0" in h


def test_trend_hesap_birim(client):
    h = client.get("/momentum").text
    m = re.search(r"<script>(.*?)</script>", h, re.S)
    assert m
    js = m.group(1)
    parcalar = []
    for fn in ("kumulatifSeri", "trendHiz"):
        fm = re.search(rf"function {fn}\([\s\S]*?\n}}", js)
        assert fm, fn
        parcalar.append(fm.group(0))
    node = shutil.which("node")
    if not node:
        pytest.skip("node yok")
    test_js = "\n".join(parcalar) + """
function assert(c,m){if(!c){console.error("FAIL: "+m);process.exit(1);}}
// kumulatif ortalama: bastan t'ye kadarki tum degerlerin ortalamasi
const k=kumulatifSeri([{x:0,y:1},{x:1,y:2},{x:2,y:3}]);
assert(k.length===3,"uzunluk ayni");
assert(k[0].y===1&&Math.abs(k[1].y-1.5)<1e-9&&Math.abs(k[2].y-2)<1e-9,"1,1.5,2");
assert(k[2].x===2,"x korunur");
assert(kumulatifSeri([]).length===0,"bos seri bos");
// sabit seri: kumulatif ortalama sabit
const s=kumulatifSeri([{x:0,y:5},{x:1,y:5},{x:2,y:5}]);
assert(s.every(p=>Math.abs(p.y-5)<1e-9),"sabit seri sabit");
// hiz: son ~%20 dilimin egimi. 10 nokta, saatte +1$: i0=8 -> 1 $/saat
const seri=[];for(let i=0;i<10;i++)seri.push({x:i*3600000,y:100+i});
assert(Math.abs(trendHiz(seri)-1)<1e-9,"son dilim egimi 1 $/saat");
// son dilim duz, oncesi dik: hiz son dilimden gelir (0'a yakin)
const seri2=[];for(let i=0;i<10;i++)seri2.push({x:i*3600000,y:i<8?100+i*10:180});
assert(Math.abs(trendHiz(seri2))<1e-9,"hiz yalniz son dilimden");
// iki nokta: dilim = tum seri
const h2=trendHiz([{x:0,y:100},{x:3600000,y:110}]);
assert(Math.abs(h2-10)<1e-9,"iki nokta 10 $/saat");
assert(trendHiz([{x:0,y:1}])===null,"tek nokta hiz yok");
assert(trendHiz([{x:5,y:1},{x:5,y:2}])===null,"dt<=0 null");
console.log("OK");
"""
    proc = subprocess.run(
        [node, "-e", test_js], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_x1_arka_plan_bolumu(client):
    h = client.get("/momentum").text
    # katlanir bolum var, varsayilan kapali (open attr yok), arsiv deseninin aynisi
    assert 'id="arkaBox"' in h
    assert '<details id="arkaBox" open' not in h
    assert "ARKA PLAN DENEYLERİ" in h
    # ana kart gridi: canli + v6 + v7 + vnext, x1 yok
    grid = h[h.index('id="kartGrid"'):h.index('id="cmp3"')]
    for kid in ("kart-canli", "kart-v6", "kart-v7", "kart-vnext"):
        assert f'id="{kid}"' in grid
    assert 'id="kart-x1"' not in grid
    # x1 karti + senkron charti arka bolumde (arsivden once)
    arka = h[h.index('id="arkaBox"'):h.index('id="arsivBox"')]
    assert 'id="kart-x1"' in arka
    assert 'id="spark-x1"' in arka
    assert 'id="eqx1chart"' in arka
    assert 'id="eqx1trend"' in arka
    # motor calisir durumda: MOTORLAR JS listesi x1'i icerir, poll/kiyas surer
    assert '"id": "x1"' in h
    # ana ekran 4 sutun
    assert "repeat(4,minmax(0,1fr))" in h


def test_islem_tablosu_x1_arkaya(client):
    h = client.get("/momentum").text
    # arka bayragi JS konfigde: x1 arka, v6/v7 on plan
    assert '"id": "x1", "ad": "X1", "renk": "#d29922", "slots": 3, "arka": true' in h
    assert '"id": "v6", "ad": "V6", "renk": "#3fb950", "slots": 5, "arka": false' in h
    assert '"id": "v7", "ad": "V7", "renk": "#58a6ff", "slots": 5, "arka": false' in h
    # x1'in kendi islem tablosu arka bolumde (arsivden once), ana tablo disari
    arka = h[h.index('id="arkaBox"'):h.index('id="arsivBox"')]
    assert 'id="isltrArka"' in arka
    assert "SON İŞLEMLER · x1" in arka
    assert 'id="isltr"' in h[:h.index('id="arkaBox"')]
    # JS: satirlar arka bayragina gore iki tabloya ayrilir, ayni /api/filo cevabindan
    assert "(m.arka?arkaRows:on).push([m,t])" in h
    assert 'bas("#isltr tbody",on); bas("#isltrArka tbody",arkaRows);' in h


def test_kart_sparkline_24_saat(client):
    h = client.get("/momentum").text
    # spark verisi saf fonksiyonda: 24 saat pencere + ~72 nokta seyreltme
    assert "function sparkHazirla" in h
    assert "SAAT=24,NOKTA=72" in h
    # renk 24s net degisime gore (yesil/kirmizi), motor renginden bagimsiz
    assert '"#f85149":"#3fb950"' in h
    assert "cizSpark(sparkId,sp,st.start)" in h
    # eski 30dk filtresi ve renk parametreli cagri kalmadi
    assert "Date.now()-30*60000" not in h
    assert "cizSpark(sparkId,pts,st.start,renk)" not in h
    # dar pencerede spark kendi 24s verisini ceker
    assert "minutes=1440" in h
    # bos-durum kartlari etkilenmez: kilitli canli/vnext kartlarinda spark yok,
    # spark yalniz bot kartlarinda (mkEqChart bagli)
    assert 'id="spark-canli"' not in h
    assert 'id="spark-vnext"' not in h
    for p in ("v6", "v7", "x1"):
        assert f'id="spark-{p}"' in h


def test_spark_hazirla_birim(client):
    h = client.get("/momentum").text
    m = re.search(r"<script>(.*?)</script>", h, re.S)
    assert m
    js = m.group(1)
    fm = re.search(r"function sparkHazirla\([\s\S]*?\n}", js)
    assert fm
    node = shutil.which("node")
    if not node:
        pytest.skip("node yok")
    test_js = fm.group(0) + """
function assert(c,m){if(!c){console.error("FAIL: "+m);process.exit(1);}}
const now=1000000000000, SAAT=3600000;
// 24 saatten eski noktalar dislanir
const eski=sparkHazirla([{x:now-25*SAAT,y:1},{x:now-1*SAAT,y:2},{x:now,y:3}],now);
assert(eski.p.length===2,"25 saatlik nokta dislandi");
assert(eski.p[0].x===now-1*SAAT,"ilk nokta 24s icinden");
// seyreltme: 500 nokta -> 72, ilk/son korunur
const cok=[];for(let i=0;i<500;i++)cok.push({x:now-23*SAAT+i*60000,y:100+i});
const s=sparkHazirla(cok,now);
assert(s.p.length===72,"72 noktaya seyreltildi");
assert(s.p[0].x===cok[0].x&&s.p[71].x===cok[499].x,"ilk/son korunur");
// 72 ve alti dokunulmaz
const az=[];for(let i=0;i<72;i++)az.push({x:now-i*60000,y:1});
assert(sparkHazirla(az,now).p.length===72,"72 nokta aynen");
// renk: net pozitif yesil, negatif kirmizi, notr/tek nokta yesil
assert(sparkHazirla([{x:now-SAAT,y:100},{x:now,y:110}],now).renk==="#3fb950","yukselis yesil");
assert(sparkHazirla([{x:now-SAAT,y:100},{x:now,y:90}],now).renk==="#f85149","dusus kirmizi");
assert(sparkHazirla([{x:now-SAAT,y:100},{x:now,y:100}],now).renk==="#3fb950","notr yesil");
assert(sparkHazirla([{x:now,y:100}],now).renk==="#3fb950","tek nokta yesil");
// renk seyreltilmis serinin ilk/son noktasindan: 24s neti korunur
assert(s.renk==="#3fb950","500 noktali yukselen seri yesil");
console.log("OK");
"""
    proc = subprocess.run(
        [node, "-e", test_js], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout


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
