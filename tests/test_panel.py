"""Panel HTML ve JS doğrulama."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

from hibrit_trader.config import CHAIN_ENTRY_PRIORITY

import pytest
from fastapi.testclient import TestClient

from hibrit_trader import panel


@pytest.fixture
def client():
    return TestClient(panel.app)


def test_index_momentum_panelini_sunar(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.text == client.get("/momentum").text
    assert "Momentum filo" in r.text
    assert "/static/" not in r.text


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


# ---- PANEL SENKRON: /api/filo tek gercek kaynak ---------------------------------


def test_kill_akisi_dosya_ve_filo_durumu(client, monkeypatch, tmp_path):
    from hibrit_trader import killswitch

    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    kill_dosya = tmp_path / "KILL"
    monkeypatch.setattr(killswitch, "KILL_FILE", kill_dosya)
    monkeypatch.delenv("KILL_SWITCH", raising=False)

    assert client.get("/api/filo").json()["kill"] is False
    r = client.post("/api/kill")
    assert r.status_code == 200 and r.json()["kill_switch"] is True
    assert kill_dosya.exists()
    assert client.get("/api/filo").json()["kill"] is True
    r = client.delete("/api/kill")
    assert r.status_code == 200 and r.json()["kill_switch"] is False
    assert not kill_dosya.exists()
    assert client.get("/api/filo").json()["kill"] is False


def test_momentum_kill_butonu_ve_bant(client):
    h = client.get("/momentum").text
    assert 'id="killBtn"' in h
    assert 'id="killBant"' in h
    assert "DURDURULDU · kill-switch aktif" in h
    assert "Filo DURDURULSUN mu?" in h
    assert "Kill-switch kaldırılsın mı?" in h
    # durum ayni /api/filo cevabindan; tetik mevcut route'lara gider
    assert "basKill(d.kill);" in h
    assert 'fetch("/api/kill",{method:killAktif?"DELETE":"POST"})' in h
    assert 'b.textContent=killAktif?"BAŞLAT":"DURDUR";' in h


def test_api_filo_tek_tick_tek_kaynak(client, monkeypatch, tmp_path):
    import json
    import time as _time

    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    opened = _time.time() - 7200
    for i, p in enumerate(("v6", "v7", "x1", "v7c")):
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
    for p in ("v6", "v7", "x1", "v7c"):
        assert d["cmp"][p] == d[p]["summary"]["realized_pnl"]
    assert d["cmp"]["v6"] == 10.0
    assert d["cmp"]["x1"] == 12.0
    # equity tek formul (_live_equity): nakit + acik pozisyonun anlik degeri
    assert d["v6"]["summary"]["equity"] == 1110.0
    # ayni 'now': uc motorun pozisyon yasi birebir ayni tick'ten
    ages = {d[p]["positions"][0]["age_min"] for p in ("v6", "v7", "x1", "v7c")}
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
    # gorunumde tek bot: v6; gizli motorlarin upd etiketi basilmaz
    assert 'id="v6upd"' in h
    for eid in ("v7upd", "x1upd", "v7cupd"):
        assert f'id="{eid}"' not in h
    # aktif motorlar icin ayri fetch kalmadi: tek gercek kaynak /api/filo
    for eski in ('fetch("/api/v6?', 'fetch("/api/v7?', 'fetch("/api/x1?',
                 'fetch("/api/v7c?'):
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
    # kart grid sade: canli + v6; gizli motorlar (v7/x1/v7c) gorunumde yok
    assert 'id="kartGrid"' in h
    for kid in ("kart-canli", "kart-v6"):
        assert f'id="{kid}"' in h
    for kid in ("kart-v7", "kart-x1", "kart-v7c"):
        assert f'id="{kid}"' not in h
    assert "cüzdan: bağlı değil" in h
    assert "V-NEXT" not in h and 'id="kart-vnext"' not in h
    # kart/chart eslesmesi: gorunur botun sparkline'i ve charti var
    assert 'id="spark-v6"' in h
    assert 'id="eqv6chart"' in h
    assert 'id="mtm-v6"' in h
    for p in ("v7", "x1", "v7c"):
        assert f'id="spark-{p}"' not in h
        assert f'id="eq{p}chart"' not in h
        assert f'id="mtm-{p}"' not in h
    # iki sutunlu grid (canli + v6)
    assert "repeat(2,minmax(0,1fr))" in h
    # tek ortak zaman filtresi + birlesik islem tablosu + arsiv aynen
    assert 'id="eqsyncbtns"' in h
    assert 'id="isltr"' in h
    assert "mfeMaeBar" in h
    assert 'id="arsivBox"' in h
    # JS konfig sunucudan basildi; gizli motorlar listede de yok
    assert '"__MOTORLAR__"' not in h
    assert '"id": "v6"' in h
    for p in ("v7", "x1", "v7c"):
        assert f'"id": "{p}"' not in h


def test_momentum_mod_rozeti_ve_canli_karti(client, monkeypatch, tmp_path):
    # paper (varsayilan): gri rozet + kilitli CANLI karti
    monkeypatch.delenv("BROKER_MODE", raising=False)
    monkeypatch.delenv("LIVE_UNLOCKED", raising=False)
    h = client.get("/momentum").text
    assert '<span class="badge">paper</span>' in h
    assert "cüzdan: bağlı değil" in h
    # dryrun rozeti
    monkeypatch.setenv("BROKER_MODE", "dryrun")
    h = client.get("/momentum").text
    assert ">dryrun</span>" in h and ">paper</span>" not in h
    # live ama kilit kapali: rozet durust, kart TAM iskeletiyle kilitli durur
    monkeypatch.setenv("BROKER_MODE", "live")
    monkeypatch.setenv("LIVE_TICKET_PCT", "25")
    h = client.get("/momentum").text
    assert "live (kilit kapalı)" in h
    assert "cüzdan: bağlı değil" not in h
    assert "kilitli &#128274;" in h or "kilitli 🔒" in h
    assert "gerçek para" not in h
    assert 'id="mtm-canli"' in h and 'id="sol-canli"' in h
    assert 'id="eqcanlichart"' in h and 'id="canliPoz"' in h
    assert "kilit kapalı, giriş yok" in h
    # live + cift kilit acik: CANLI (V7) rozeti + bilgi karti
    monkeypatch.setenv("LIVE_UNLOCKED", "1")
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    (tmp_path / "LIVE_ONAY").write_text("canli-islem-onayliyorum", encoding="utf-8")
    h = client.get("/momentum").text
    assert 'class="badge err">CANLI (V7)</span>' in h
    assert "cüzdan: bağlı değil" not in h
    assert "gerçek para" in h and "bilet: MTM %25" in h
    assert "kilit kapalı, giriş yok" not in h
    assert "DZXZ" in h  # cuzdan kisaltmasi kfoot'ta
    assert "denetim defterin" in h
    # kill butonu bagli sablonda da var: JS listener'i null'a dusmez,
    # canli moddayken acil durdurma kontrolu ekranda kalir
    assert 'id="killBtn"' in h
    # tam gosterge: MTM + serbest SOL satiri + spark + cuzdan charti + poz tablosu
    assert 'id="mtm-canli"' in h and 'id="sub-canli"' in h
    assert 'id="sol-canli"' in h
    assert 'id="spark-canli"' in h and 'id="foot-canli"' in h
    assert 'id="eqcanlichart"' in h and 'id="eqcanlilabel"' in h
    assert 'id="canliPoz"' in h and "AÇIK POZİSYONLAR" in h
    assert "baz $119.59" in h
    # paper'a donunce gosterge alanlari yok
    monkeypatch.delenv("BROKER_MODE", raising=False)
    h = client.get("/momentum").text
    assert 'id="mtm-canli"' not in h and 'id="eqcanlichart"' not in h
    assert 'id="canliPoz"' not in h


def test_momentum_son_islemler_canli_kolonlari(client, monkeypatch):
    # SON ISLEMLER: giris/cikis/tx kolonlari + v7 canli satir mekanigi.
    # Canli satirlar YALNIZ imzali (zincir) kayitlardan uretilir; v7 paper
    # kayitlari tabloya karismaz (motor gizli, JS imza filtresi).
    monkeypatch.delenv("BROKER_MODE", raising=False)
    h = client.get("/momentum").text
    assert "<th>giriş</th><th>çıkış</th>" in h
    assert "<th>tx</th>" in h
    assert h.count("<th>tx</th>") == 2  # aktif filo + x1 tablosu ayni semada
    assert "solscan.io/tx/" in h
    assert "livechip" in h and "tr.canli td" in h
    assert "if(t.signature)" in h  # imzasiz v7 kaydi tabloya girmez
    assert 'm.id==="v7"' in h  # v7 gorunur olursa cift satir olmaz korumasi
    assert "canli_pnl_usd" in h  # canli satirda pnl gercek cuzdan bazli


def test_api_filo_canli_blogu(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CANLI_BAZ_USD", raising=False)
    # snapshot yokken canli alani hic yok (paper kurulum etkilenmez)
    monkeypatch.setattr("hibrit_trader.canli_gosterge._son", None)
    d = client.get("/api/filo").json()
    assert "canli" not in d
    snap = {"ts": 1000.0, "mtm": 148.56, "sol": 1.5, "sol_fiyat": 80.0,
            "poz_usd": 28.56, "acik_poz": 1, "islem_n": 2}
    monkeypatch.setattr("hibrit_trader.canli_gosterge._son", snap)
    c = client.get("/api/filo").json()["canli"]
    assert c["mtm"] == 148.56 and c["baz"] == 119.59
    assert c["pnl_pct"] == round((148.56 / 119.59 - 1) * 100, 2)
    assert c["islem_n"] == 2 and c["acik_poz"] == 1 and c["sol"] == 1.5
    assert c["pozisyonlar"] == []  # v7 state yok: bos tablo


def test_api_filo_canli_pozisyonlar(client, monkeypatch, tmp_path):
    # Acik pozisyonlar tablosu: v7 state'inden canli_miktar>0 kayitlar,
    # K/Z gercek cuzdan miktari uzerinden (paper amount_token degil)
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    (tmp_path / "v7_state.json").write_text(json.dumps({
        "balance": 1000.0, "start_balance": 1000.0, "realized_pnl": 0.0,
        "positions": [
            {"pair": "FEBU/SOL", "entry_price": 0.0002, "last_price": 0.00025,
             "amount_token": 100000.0, "canli_miktar": 25000.0,
             "opened_ts": time.time()},
            {"pair": "PAPER/SOL", "entry_price": 1.0, "last_price": 1.1,
             "amount_token": 100.0, "opened_ts": time.time()},  # canli degil
        ]}))
    snap = {"ts": 1000.0, "mtm": 120.0, "sol": 1.0, "sol_fiyat": 80.0,
            "poz_usd": 6.25, "acik_poz": 1, "islem_n": 1}
    monkeypatch.setattr("hibrit_trader.canli_gosterge._son", snap)
    pozlar = client.get("/api/filo").json()["canli"]["pozisyonlar"]
    assert len(pozlar) == 1  # canli_miktar'i olmayan paper poz tabloya girmez
    p = pozlar[0]
    assert p["pair"] == "FEBU/SOL" and p["miktar"] == 25000.0
    assert p["giris"] == 0.0002 and p["guncel"] == 0.00025
    assert p["kz_usd"] == round(25000.0 * 0.00005, 2)
    assert p["kz_pct"] == 25.0


def test_api_canli_equity_serisi(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CANLI_BAZ_USD", raising=False)
    now = time.time()
    (tmp_path / "canli_equity.jsonl").write_text(
        json.dumps({"ts": now - 300, "eq": 100.0}) + "\n"
        + json.dumps({"ts": now - 120, "eq": 119.59}) + "\n"
        + json.dumps({"ts": now - 30, "eq": 121.0}) + "\n")
    snap = {"ts": now, "mtm": 122.5, "sol": 1.5, "sol_fiyat": 80.0,
            "poz_usd": 0.0, "acik_poz": 0, "islem_n": 0}
    monkeypatch.setattr("hibrit_trader.canli_gosterge._son", snap)
    d = client.get("/api/canli/equity").json()
    assert d["start_balance"] == 119.59  # kesikli referans cizgisi bazdan
    assert [p[1] for p in d["points"]] == [100.0, 119.59, 121.0, 122.5]
    assert d["points"][0][0] == round((now - 300) * 1000)
    # pencere kirpma: pencere disindaki son eski nokta capa olarak kalir
    d = client.get("/api/canli/equity?minutes=1").json()
    assert [p[1] for p in d["points"]] == [119.59, 121.0, 122.5]


def test_momentum_js_guardsiz_listener_yok(client):
    # Hata sinifi kilidi: script govdesinde zincirleme
    # getElementById(...).addEventListener cagrisi, eleman sablona gore
    # yoksa TypeError ile TUM script'i oldurur (killBtn vakasi, 2026-07-11).
    import re
    h = client.get("/momentum").text
    zincir = re.findall(r'getElementById\([^)]*\)\s*\.\s*addEventListener', h)
    assert zincir == [], zincir


def test_momentum_trend_katmani(client):
    h = client.get("/momentum").text
    # gorunur bot chartinda trend rozeti; bos-durum (canli, paper) etkilenmez
    assert 'id="eqv6trend"' in h
    for p in ("v7", "x1", "v7c"):
        assert f'id="eq{p}trend"' not in h
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


def test_gizli_motorlar_gorunumde_yok(client):
    # 12 Tem karari: canli test doneminde ekranda yalniz V6 + CANLI; v7/x1/v7c
    # motorlari calismaya devam eder (api/filo uretir) ama gorunumden cikar
    h = client.get("/momentum").text
    grid = h[h.index('id="kartGrid"'):h.index('id="cmp3"')]
    for kid in ("kart-canli", "kart-v6"):
        assert f'id="{kid}"' in grid
    for kid in ("kart-v7", "kart-x1", "kart-v7c"):
        assert f'id="{kid}"' not in h
    # arka bolum bos ve gizli (element kalir, JS null gormesin)
    assert '<details id="arkaBox" style="display:none;' in h
    arka = h[h.index('id="arkaBox"'):h.index('id="arsivBox"')]
    assert 'id="eqx1chart"' not in arka
    # motorlar API'de uretmeye devam eder (gorunum disi, veri degil)
    d = client.get("/api/filo").json()
    for p in ("v6", "v7", "x1", "v7c"):
        assert p in d and p in d["cmp"]


def test_islem_tablosu_sadece_gorunur_motorlar(client):
    h = client.get("/momentum").text
    # JS konfig: yalniz v6 (gizli motorlar listede yok, kiyas satiri v6+canli)
    assert '"id": "v6", "ad": "V6", "renk": "#3fb950", "slots": 5, "arka": false' in h
    for p in ('"id": "v7"', '"id": "x1"', '"id": "v7c"'):
        assert p not in h
    # ana islem tablosu duruyor; arka tablo elementi kalir (JS guard'i icin)
    assert 'id="isltr"' in h[:h.index('id="arkaBox"')]
    assert 'id="isltrArka"' in h
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
    # bos-durum karti etkilenmez: paper'da canli placeholder, spark yok;
    # spark yalniz gorunur bot kartinda (mkEqChart bagli)
    assert 'id="spark-canli"' not in h
    assert 'id="spark-v6"' in h
    for p in ("v7", "x1", "v7c"):
        assert f'id="spark-{p}"' not in h


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
