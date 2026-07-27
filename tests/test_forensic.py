"""Forensic Factory iskeleti: guven kapisi, kohort, ozellik, imza."""

import json
import time

import pytest

from hibrit_trader.forensic import karsilastir, kohort, ozellik, veri


def _satir(m, ts, pnl_usd, pnl_pct, **kw):
    d = {"trade_id": kw.pop("tid", f"t{ts}{pnl_usd}"), "ts": ts,
         "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "cost_usd": 100.0,
         "hold_sec": 300, "exit_reason": kw.pop("cikis", "tp_2"),
         "chg_h1": 15.0, "chg_m5": 1.0, "liq_entry": 200000.0,
         "mfe_pct": 2.0, "mae_pct": -1.0, "token_address": "TOK"}
    d.update(kw)
    return json.dumps(d)


@pytest.fixture
def veri_dizin(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    return tmp_path


def test_guven_kapisi_kirli_pencereyi_reddeder(veri_dizin):
    with pytest.raises(veri.GuvenHatasi):
        veri.yukle(("v7",), baslangic="2026-07-18T00:00:00Z")
    # acikca istenirse gecer ve damgalanir
    ev = veri.yukle(("v7",), baslangic="2026-07-18T00:00:00Z",
                    kirli_pencereye_izin=True)
    assert ev.kirli_pencere is True


def test_yazilmayan_alan_ozellik_olamaz():
    with pytest.raises(veri.GuvenHatasi):
        ozellik.kaydet("olmaz", "giris", ("mae_at_sec",))(lambda t: 1.0)
    with pytest.raises(veri.GuvenHatasi):
        ozellik.kaydet("olmaz2", "giris", ("uydurma_alan",))(lambda t: 1.0)


def test_pencere_disi_dusurulur(veri_dizin):
    bas = veri._ts(veri.GUVENILIR_BASLANGIC)
    (veri_dizin / "v7_trades.jsonl").write_text("\n".join([
        _satir("v7", bas - 3600, -5.0, -5.0, tid="eski"),
        _satir("v7", bas + 3600, +2.0, 2.0, tid="yeni"),
    ]))
    ev = veri.yukle(("v7",))
    assert len(ev.islemler) == 1
    assert ev.dusen.get("pencere_disi") == 1


def test_kismi_cikislar_tek_pozisyona_indirgenir(veri_dizin):
    bas = veri._ts(veri.GUVENILIR_BASLANGIC) + 7200
    (veri_dizin / "r2_trades.jsonl").write_text("\n".join([
        _satir("r2", bas, +3.0, 25.0, tid="P1", cikis="tp_kilit_25"),
        _satir("r2", bas + 60, +4.0, 40.0, tid="P1", cikis="tp_kilit_40"),
        _satir("r2", bas + 120, -9.0, -30.0, tid="P1", cikis="stop_felaket"),
    ]))
    ev = veri.yukle(("r2",))
    assert len(ev.islemler) == 1 and ev.birlestirilen_poz == 1
    t = ev.islemler[0]
    assert t["pnl_usd"] == pytest.approx(-2.0)      # 3 + 4 - 9
    assert t["exit_reason"] == "stop_felaket"       # kismi olmayan son cikis
    assert t["_parca_n"] == 3


def test_kohort_hedef_kontrol_kesismez(veri_dizin):
    bas = veri._ts(veri.GUVENILIR_BASLANGIC) + 7200
    sat = [_satir("v7", bas + i * 60, -float(i), -float(i), tid=f"K{i}")
           for i in range(1, 11)]
    (veri_dizin / "v7_trades.jsonl").write_text("\n".join(sat))
    ev = veri.yukle(("v7",))
    hedef, kontrol = kohort.uygula("gunluk_en_kotu_n", ev.islemler, n=3)
    assert len(hedef) == 3
    assert not ({id(t) for t in hedef} & {id(t) for t in kontrol})
    assert len(hedef) + len(kontrol) == len(ev.islemler)
    # en buyuk 3 kayip secilmis olmali
    assert sorted(t["pnl_usd"] for t in hedef) == [-10.0, -9.0, -8.0]


def test_eksik_deger_sifirla_doldurulmaz(veri_dizin):
    bas = veri._ts(veri.GUVENILIR_BASLANGIC) + 7200
    sat = [_satir("v7", bas + i * 60, -1.0, -1.0, tid=f"A{i}",
                  tetik_gecikme_sec=(0.5 if i % 2 else None))
           for i in range(20)]
    (veri_dizin / "v7_trades.jsonl").write_text("\n".join(sat))
    ev = veri.yukle(("v7",))
    v, eksik = karsilastir._degerler(ev.islemler, "tetik_gecikme")
    assert eksik == 10 and len(v) == 10        # yarisi elenir, sifir eklenmez


def test_imza_zaman_ayrimini_korur(veri_dizin):
    bas = veri._ts(veri.GUVENILIR_BASLANGIC) + 7200
    sat = []
    for i in range(30):                        # kontrol: h1 ~10
        sat.append(_satir("v7", bas + i * 60, +1.0, 1.0, tid=f"C{i}", chg_h1=10.0))
    for i in range(15):                        # hedef: h1 ~45
        sat.append(_satir("v7", bas + 3600 + i * 60, -20.0, -20.0,
                          tid=f"H{i}", chg_h1=45.0, mfe_pct=0.0))
    (veri_dizin / "v7_trades.jsonl").write_text("\n".join(sat))
    ev = veri.yukle(("v7",))
    hedef, kontrol = kohort.uygula("esik_alti_pct", ev.islemler, esik=-15.0)
    imz = karsilastir.imza(hedef, kontrol, min_n=5)
    satir = {r["ozellik"]: r for r in imz["satirlar"]}
    assert satir["h1"]["zaman"] == "giris"
    assert satir["h1"]["cliff_delta"] > 0.9          # net ayrim yakalanir
    assert satir["mfe"]["zaman"] == "sonra"          # teshis blogunda kalir


def test_maliyet_ozeti(veri_dizin):
    bas = veri._ts(veri.GUVENILIR_BASLANGIC) + 7200
    sat = [_satir("v7", bas + i * 60, +1.0, 1.0, tid=f"C{i}") for i in range(9)]
    sat.append(_satir("v7", bas + 999, -50.0, -50.0, tid="BIG"))
    (veri_dizin / "v7_trades.jsonl").write_text("\n".join(sat))
    ev = veri.yukle(("v7",))
    hedef, _ = kohort.uygula("gunluk_en_kotu_n", ev.islemler, n=1)
    m = karsilastir.maliyet_ozeti(hedef, ev.islemler)
    assert m["evren_pnl_usd"] == pytest.approx(-41.0)
    assert m["kohort_haric_pnl_usd"] == pytest.approx(9.0)
