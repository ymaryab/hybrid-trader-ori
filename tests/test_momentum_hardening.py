"""Momentum v2 sağlamlaştırma testleri (check-up düzeltmeleri).

Kapsam: atomik state yazımı, bozuk state yedekleme, pozisyon doğrulama,
kapanışta çift sayım koruması, çift giriş koruması, korkulukların
varsayılan-kapalı olduğu (paper davranışı değişmez).
Hepsi MOMENTUM_DATA_DIR=tmp ile izole; gerçek data/'ya dokunmaz.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import hibrit_trader.momentum_session as ms
from hibrit_trader.momentum_session import MomentumEngine


@pytest.fixture(autouse=True)
def mom_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    return tmp_path


def _settings():
    return SimpleNamespace(scan_chains=("solana",))


def _pair(pool="POOLADDR1", token="TOKENADDR1", price=1.0, liq=50_000.0):
    return SimpleNamespace(
        name="TEST / SOL", chain="solana", pool_address=pool, token_address=token,
        price_usd=price, liquidity_usd=liq, chg_m5=3.0, chg_h1=10.0,
    )


def test_save_is_atomic_and_loadable(mom_data_dir):
    eng = MomentumEngine(_settings())
    eng._save()
    p = mom_data_dir / ms.STATE_FILE
    assert p.exists()
    assert not (mom_data_dir / (ms.STATE_FILE + ".tmp")).exists()
    data = json.loads(p.read_text())
    assert data["balance"] == pytest.approx(ms.START_BALANCE)


def test_corrupt_state_is_backed_up_not_lost(mom_data_dir):
    p = mom_data_dir / ms.STATE_FILE
    p.write_text('{"balance": 500, "positions": [BOZUK')
    eng = MomentumEngine(_settings())
    # Temiz başlar ama bozuk dosya silinmez, .corrupt-* yedeğine taşınır
    assert eng.balance == pytest.approx(ms.START_BALANCE)
    backups = list(mom_data_dir.glob(ms.STATE_FILE + ".corrupt-*"))
    assert len(backups) == 1
    assert "BOZUK" in backups[0].read_text()


def test_invalid_position_rows_are_dropped(mom_data_dir):
    good = {
        "trade_id": "t1", "pair": "A / SOL", "chain": "solana",
        "pool_address": "p1", "entry_price": 1.0, "amount_token": 10.0,
        "cost_usd": 10.0, "opened_ts": 1.0, "last_price": 1.0, "peak_price": 1.0,
    }
    bad = {"pair": "eksik alanlar"}
    (mom_data_dir / ms.STATE_FILE).write_text(json.dumps({
        "balance": 900.0, "start_balance": 1000.0, "realized_pnl": -10.0,
        "positions": [good, bad, "dict bile değil"],
    }))
    eng = MomentumEngine(_settings())
    assert eng.balance == pytest.approx(900.0)
    assert [p["trade_id"] for p in eng.positions] == ["t1"]


def test_close_failure_keeps_position_open_no_double_count(mom_data_dir, monkeypatch):
    eng = MomentumEngine(_settings())
    assert eng._open_position(_pair(), 100.0)
    bal_after_open = eng.balance
    pos = eng.positions[0]

    def boom(name, row):
        raise OSError("disk dolu")

    monkeypatch.setattr(eng, "_append", boom)
    with pytest.raises(OSError):
        eng._close_position(pos, price=2.0, reason="trail", now=pos["opened_ts"] + 60)
    # Trades yazılamadıysa state DEĞİŞMEMİŞ olmalı: pozisyon açık, bakiye aynı
    assert eng.positions == [pos]
    assert eng.balance == pytest.approx(bal_after_open)
    assert eng.realized_pnl == pytest.approx(0.0)


def test_close_success_updates_state_and_writes_trade(mom_data_dir):
    eng = MomentumEngine(_settings())
    assert eng._open_position(_pair(), 100.0)
    pos = eng.positions[0]
    eng._close_position(pos, price=2.0, reason="trail", now=pos["opened_ts"] + 60)
    assert eng.positions == []
    lines = (mom_data_dir / ms.TRADES_FILE).read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["exit_reason"] == "trail"
    # State dosyası kapanışın hemen ardından persist edilmiş olmalı
    saved = json.loads((mom_data_dir / ms.STATE_FILE).read_text())
    assert saved["positions"] == []
    assert saved["balance"] == pytest.approx(eng.balance, abs=0.01)


def test_open_position_persists_state_immediately(mom_data_dir):
    eng = MomentumEngine(_settings())
    assert eng._open_position(_pair(), 100.0)
    saved = json.loads((mom_data_dir / ms.STATE_FILE).read_text())
    assert len(saved["positions"]) == 1
    assert saved["balance"] == pytest.approx(eng.balance, abs=0.01)


def test_single_instance_lock(mom_data_dir):
    e1 = MomentumEngine(_settings())
    e2 = MomentumEngine(_settings())
    assert e1._acquire_lock() is True
    assert e2._acquire_lock() is False  # ikinci instance motoru başlatamaz


def test_guardrails_default_off(mom_data_dir):
    # Env yokken limitler kapalı: giriş engeli olmamalı (paper davranışı değişmez)
    assert ms.DAILY_LOSS_LIMIT_USD == 0.0
    assert ms.MAX_POS_USD == 0.0
    eng = MomentumEngine(_settings())
    eng._day_realized = -999_999.0  # limit kapalıyken devasa zarar bile engel değil
    assert eng._entries_blocked() is None


def test_daily_loss_limit_blocks_entries_when_enabled(mom_data_dir, monkeypatch):
    monkeypatch.setattr(ms, "DAILY_LOSS_LIMIT_USD", 50.0)
    eng = MomentumEngine(_settings())
    eng._day_realized_add(-60.0, ms.time.time())  # bugünün zararı
    assert eng._entries_blocked() == "daily_loss_limit"
    # Gün devri: dünkü zarar bugünü bloklamaz
    eng._day_key = "2000-01-01"
    assert eng._entries_blocked() is None


def test_kill_switch_blocks_entries(mom_data_dir, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH", "1")
    eng = MomentumEngine(_settings())
    assert eng._entries_blocked() == "kill_switch"
    monkeypatch.delenv("KILL_SWITCH")
    assert eng._entries_blocked() is None


def test_cooldown_set_by_exit_reason(mom_data_dir):
    eng = MomentumEngine(_settings())
    assert eng._open_position(_pair(token="TOK_STOP"), 100.0)
    pos = eng.positions[0]
    now = pos["opened_ts"] + 60
    eng._close_position(pos, price=0.9, reason="stop_2", now=now)
    assert eng._cooldown_until["TOK_STOP"] == pytest.approx(now + ms.COOLDOWN_STOP_SEC)

    assert eng._open_position(_pair(pool="P2", token="TOK_TRAIL"), 100.0)
    pos = eng.positions[0]
    now2 = pos["opened_ts"] + 60
    eng._close_position(pos, price=2.0, reason="trail", now=now2)
    assert eng._cooldown_until["TOK_TRAIL"] == pytest.approx(now2 + ms.COOLDOWN_EXIT_SEC)


def test_cooldown_blocks_reentry_then_expires(mom_data_dir, monkeypatch):
    eng = MomentumEngine(_settings())
    target = _pair(pool="POOL_A", token="TOK_A")
    monkeypatch.setattr(ms, "scan_all", lambda chains: [target])
    monkeypatch.setattr(
        ms, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(ms.time, "sleep", lambda s: None)  # rate-limit beklemesi olmasın
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: 1.0)  # rejim kapisi acik

    # Cooldown aktifken: giriş YOK (farklı havuz olsa bile token bazlı yasak)
    eng._cooldown_until["TOK_A"] = ms.time.time() + 3600
    eng._enter(client=SimpleNamespace())
    assert eng.positions == []
    # Reject kaydı "cooldown" sebebiyle düşmüş olmalı (pasif gözlem)
    rej = [
        json.loads(ln)
        for ln in (mom_data_dir / ms.REJECTS_FILE).read_text().splitlines()
    ]
    assert any(r.get("reason") == "cooldown" for r in rej)

    # Süre dolunca: aynı token yeniden girilebilir
    eng._cooldown_until["TOK_A"] = ms.time.time() - 1
    eng._enter(client=SimpleNamespace())
    assert len(eng.positions) == 1
    assert eng.positions[0]["token_address"] == "TOK_A"


def _enter_with_regime(eng, monkeypatch, sol_h1):
    """Tek adaylı _enter kurulumu: sol_chg_h1 sabitlenir, giriş sonucu döner."""
    target = _pair(pool="POOL_R", token="TOK_R")
    monkeypatch.setattr(ms, "scan_all", lambda chains: [target])
    monkeypatch.setattr(
        ms, "check_token", lambda client, chain, token: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(ms.time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "_sol_chg_h1", lambda client: sol_h1)
    eng._enter(client=SimpleNamespace())
    return eng.positions


def test_regime_filter_blocks_entries_and_logs_reject(mom_data_dir, monkeypatch):
    eng = MomentumEngine(_settings())
    assert _enter_with_regime(eng, monkeypatch, sol_h1=-0.5) == []
    rej = [
        json.loads(ln)
        for ln in (mom_data_dir / ms.REJECTS_FILE).read_text().splitlines()
    ]
    assert any(r.get("reason") == "rejim" for r in rej)
    # 30dk recheck kuyruğuna girmiş olmalı (kaçan fırsat ölçümü)
    assert any(w["reason"] == "rejim" for w in eng._reject_watch.values())


def test_regime_filter_allows_when_sol_positive(mom_data_dir, monkeypatch):
    eng = MomentumEngine(_settings())
    assert len(_enter_with_regime(eng, monkeypatch, sol_h1=0.3)) == 1


def test_regime_filter_fail_closed_on_api_error(mom_data_dir, monkeypatch):
    # 09 Tem: sol_chg_h1 alınamazsa giriş kapısı KAPALI (fail-closed).
    # 11 Tem: veri-yok/hata kararını artık paylaşımlı sol_chg_h1 verir (None döner).
    eng = MomentumEngine(_settings())
    assert _enter_with_regime(eng, monkeypatch, sol_h1=None) == []
    rej = [
        json.loads(ln)
        for ln in (mom_data_dir / ms.REJECTS_FILE).read_text().splitlines()
    ]
    assert any(r.get("reason") == "rejim_veri_yok" for r in rej)


class _FailingClient:
    def get(self, *a, **k):
        raise RuntimeError("api")


def test_shared_sol_h1_api_error_with_recent_cache_allows(monkeypatch):
    # son başarılı değer SOL_H1_STALE_MAX_SEC'e kadar geçerli
    monkeypatch.setattr(ms, "SOL_H1_CACHE_SEC", 50)
    monkeypatch.setattr(ms, "_sol_h1_paylasimli", (ms.time.time() - 60, 1.0))
    assert ms.sol_chg_h1(_FailingClient()) == 1.0


def test_shared_sol_h1_api_error_with_stale_cache_blocks(monkeypatch):
    monkeypatch.setattr(ms, "SOL_H1_CACHE_SEC", 50)
    monkeypatch.setattr(
        ms, "_sol_h1_paylasimli", (ms.time.time() - ms.SOL_H1_STALE_MAX_SEC - 10, 1.0)
    )
    assert ms.sol_chg_h1(_FailingClient()) is None


def test_shared_sol_h1_no_data_fail_closed(monkeypatch):
    monkeypatch.setattr(ms, "_sol_h1_paylasimli", (0.0, None))
    assert ms.sol_chg_h1(_FailingClient()) is None


def test_shared_sol_h1_fresh_cache_no_fetch(monkeypatch):
    monkeypatch.setattr(ms, "_sol_h1_paylasimli", (ms.time.time() - 10, 0.42))
    assert ms.sol_chg_h1(_FailingClient()) == 0.42  # fetch hiç denenmez


class _DsResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _GtFailDsOkClient:
    """GT istegi patlar (429 firtinasi), DexScreener h1 verir."""

    def __init__(self, h1="0.87"):
        self.h1 = h1
        self.ds_calls = 0

    def get(self, url, *a, **k):
        if "geckoterminal" in url:
            raise RuntimeError("429")
        self.ds_calls += 1
        return _DsResp({"pair": {"priceChange": {"h1": self.h1}}})


def test_sol_h1_gt_dusunce_dexscreener_yedegi(monkeypatch):
    monkeypatch.setattr(ms, "_sol_h1_paylasimli", (0.0, None))
    assert ms.sol_chg_h1(_GtFailDsOkClient()) == 0.87
    # yedek deger paylasimli cache'e taze yazilir
    val, ts = ms.sol_h1_son_olcum()
    assert val == 0.87 and ts > 0


def test_sol_h1_kota_reddi_gt_atlanir_ds_kullanilir(monkeypatch):
    from hibrit_trader import kota

    monkeypatch.setattr(
        kota, "izin",
        lambda host, sinif, maliyet=1.0: host != "geckoterminal",
    )

    class _GtYasak(_GtFailDsOkClient):
        def get(self, url, *a, **k):
            assert "geckoterminal" not in url, "kota reddine ragmen GT istegi"
            return super().get(url, *a, **k)

    monkeypatch.setattr(ms, "_sol_h1_paylasimli", (0.0, None))
    assert ms.sol_chg_h1(_GtYasak()) == 0.87


def test_sol_h1_ds_pairs_listesi_de_okunur(monkeypatch):
    class _DsListe:
        def get(self, url, *a, **k):
            if "geckoterminal" in url:
                raise RuntimeError("429")
            return _DsResp({"pairs": [{"priceChange": {"h1": "-1.25"}}]})

    monkeypatch.setattr(ms, "_sol_h1_paylasimli", (0.0, None))
    assert ms.sol_chg_h1(_DsListe()) == -1.25


def test_sol_h1_son_olcum_fetch_tetiklemez(monkeypatch):
    # panel rozet yasi: cache'teki (deger, fetch_ts) cifti aynen doner,
    # bos cache'te (None, 0.0); hicbir kosulda GET atilmaz
    monkeypatch.setattr(ms, "_sol_h1_paylasimli", (0.0, None))
    assert ms.sol_h1_son_olcum() == (None, 0.0)
    ts = ms.time.time() - 1860
    monkeypatch.setattr(ms, "_sol_h1_paylasimli", (ts, 0.213))
    assert ms.sol_h1_son_olcum() == (0.213, ts)  # bayat olsa da deger+ts doner


# ---- Rejim gecis bildirimi: 0.5 esigi kesilince Telegram --------------------------


def _bildirimler(monkeypatch):
    gonderilen = []
    monkeypatch.setattr("hibrit_trader.killswitch.notify",
                        lambda msg, *a, **k: gonderilen.append(msg))
    return gonderilen


def test_rejim_bildirim_ilk_ornek_baz_restart_spam_yok(monkeypatch):
    g = _bildirimler(monkeypatch)
    ms._rejim_gecis_bildir(0.8, ms.time.time())  # restart sonrasi ilk gozlem
    assert g == []
    ms._rejim_gecis_bildir(0.2, ms.time.time())  # gercek gecis
    assert g == ["Rejim kapandi: sol_h1 0.20"]


def test_rejim_bildirim_yukari_kesis_metni(monkeypatch):
    g = _bildirimler(monkeypatch)
    now = ms.time.time()
    ms._rejim_gecis_bildir(0.2, now)
    ms._rejim_gecis_bildir(0.63, now + 1)
    assert g == ["Rejim ACILDI: sol_h1 0.63"]
    # esik dahil: 0.35 acik sayilir (v7 kapisiyla ayni yon, 13 Tem cift ayar)
    ms._rejim_gecis_bildir(0.34, now + 700)
    ms._rejim_gecis_bildir(0.35, now + 1400)
    assert g[-1] == "Rejim ACILDI: sol_h1 0.35"


def test_rejim_bildirim_ayni_durumda_tekrar_yok(monkeypatch):
    g = _bildirimler(monkeypatch)
    now = ms.time.time()
    ms._rejim_gecis_bildir(0.2, now)
    ms._rejim_gecis_bildir(0.7, now + 1)
    ms._rejim_gecis_bildir(0.9, now + 700)   # hala acik: bildirim yok
    ms._rejim_gecis_bildir(0.55, now + 1400)
    assert len(g) == 1


def test_rejim_bildirim_throttle_yok_flapping_yakalanir(monkeypatch):
    # 15 Tem: 10dk throttle kaldirildi (throttle bloklarken durum guncellenmiyor,
    # rapid flapping kapandi/ACILDI'nin biri sessizce yutuluyordu). Simdi her
    # gecis aninda gider.
    g = _bildirimler(monkeypatch)
    now = ms.time.time()
    ms._rejim_gecis_bildir(0.2, now)          # baz: kapali
    ms._rejim_gecis_bildir(0.8, now + 1)      # ACILDI
    ms._rejim_gecis_bildir(0.1, now + 60)     # 1dk sonra kapandi: hemen gider
    ms._rejim_gecis_bildir(0.9, now + 120)    # 1dk sonra tekrar ACILDI: hemen gider
    assert g == [
        "Rejim ACILDI: sol_h1 0.80",
        "Rejim kapandi: sol_h1 0.10",
        "Rejim ACILDI: sol_h1 0.90",
    ]


def test_rejim_bildirim_none_durumu_degistirmez(monkeypatch):
    g = _bildirimler(monkeypatch)
    now = ms.time.time()
    ms._rejim_gecis_bildir(0.8, now)
    ms._rejim_gecis_bildir(None, now + 700)
    assert g == []
    ms._rejim_gecis_bildir(0.2, now + 1400)
    assert len(g) == 1


def test_rejim_bildirim_restart_diske_persist(monkeypatch):
    # 15 Tem (2): restart aninda meydana gelen transition sessiz kalmiyor,
    # cunku _rejim_bildirim_durum diske persist ediliyor.
    g = _bildirimler(monkeypatch)
    now = ms.time.time()
    # Ilk kurulum: baseline yaz, sessiz
    ms._rejim_gecis_bildir(0.8, now)  # acik baseline
    assert g == []
    # Restart simulasyonu: global sifirlan
    monkeypatch.setattr(ms, "_rejim_bildirim_durum", None)
    # Restart sonrasi ilk sample rejim kapali: diskten "acik" yuklenir, gecis ACILDI->kapandi
    ms._rejim_gecis_bildir(0.1, now + 100)
    assert g == ["Rejim kapandi: sol_h1 0.10"]
    # Restart tekrari: global sifirlan
    monkeypatch.setattr(ms, "_rejim_bildirim_durum", None)
    # Simdi kapali durum diskte; kapali sample yine geldi -> transition YOK
    ms._rejim_gecis_bildir(0.2, now + 200)
    assert len(g) == 1
    # Restart + tekrar acildi
    monkeypatch.setattr(ms, "_rejim_bildirim_durum", None)
    ms._rejim_gecis_bildir(0.9, now + 300)
    assert g[-1] == "Rejim ACILDI: sol_h1 0.90"


class _SolOkClient:
    def __init__(self, h1):
        self.h1 = h1

    def get(self, *a, **k):
        h1 = self.h1

        class _R:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": {"attributes": {
                    "price_change_percentage": {"h1": str(h1)}}}}

        return _R()


def test_sol_chg_h1_taze_fetch_bildirimi_tetikler(monkeypatch):
    g = _bildirimler(monkeypatch)
    monkeypatch.setattr(ms, "SOL_H1_CACHE_SEC", 0)  # her cagri taze fetch
    assert ms.sol_chg_h1(_SolOkClient(0.2)) == 0.2  # baz: kapali, bildirim yok
    assert ms.sol_chg_h1(_SolOkClient(0.71)) == 0.71
    assert g == ["Rejim ACILDI: sol_h1 0.71"]


def test_sol_chg_h1_cache_okumasi_bildirim_uretmez(monkeypatch):
    g = _bildirimler(monkeypatch)
    ms._rejim_gecis_bildir(0.2, ms.time.time())  # baz: kapali
    # taze cache'ten okuma (fetch yok): deger esik ustu olsa da bildirim yok
    monkeypatch.setattr(ms, "_sol_h1_paylasimli", (ms.time.time() - 10, 0.9))
    assert ms.sol_chg_h1(_FailingClient()) == 0.9
    assert g == []


def test_regime_filter_none_means_closed(mom_data_dir, monkeypatch):
    eng = MomentumEngine(_settings())
    assert _enter_with_regime(eng, monkeypatch, sol_h1=None) == []


def test_regime_default_threshold():
    assert ms.SOL_H1_MIN == 0.0  # MOM_SOL_H1_MIN varsayilani


def test_cooldown_default_durations():
    # Sadece bu kural eklendi, süreler spesifikasyon ile birebir: 60dk / 15dk
    assert ms.COOLDOWN_STOP_SEC == 3600.0
    assert ms.COOLDOWN_EXIT_SEC == 900.0


def test_day_realized_restored_from_trades(mom_data_dir):
    eng = MomentumEngine(_settings())
    assert eng._open_position(_pair(), 100.0)
    pos = eng.positions[0]
    eng._close_position(pos, price=2.0, reason="trail", now=pos["opened_ts"] + 60)
    day_pnl = eng._day_realized
    assert day_pnl != 0.0
    eng2 = MomentumEngine(_settings())  # restart simülasyonu
    assert eng2._day_realized == pytest.approx(day_pnl, abs=0.01)
