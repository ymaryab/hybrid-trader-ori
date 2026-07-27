"""Otonom kaynak secici v2 (23 Tem, P0 denetim duzeltmeleri).

Mantik (kullanici speci + P0 duzeltmeleri):
- STATE-TRIGGER: karar leader != current_live_engine uzerinden verilir
  (lider degisti mi degil). Basarisiz gecis sonraki turda yeniden denenir.
- Cift bayrak: user_enabled (yalniz kullanici degistirir) x system_enabled
  (yalniz sistem: tum motorlar <=0 ise OFF, pozitif lider dogunca ON).
  effective = user_enabled AND system_enabled. system OFF SALTERI DE INDIRIR
  (23 Tem kullanici karari: negatif rejimde giris yok, cikislar surer);
  pozitif lider donunce salter tekrar acilir.
- Gecis (HIBRIT, 23 Tem): alim durur, DOGAL_SN boyunca dogal
  cikislar calisir, sure dolunca kalanlar zorla satilir -> duzlesme -> swap oncesi lider yeniden
  dogrulanir -> niyet diske yazilir -> canli_swap.py (drop-in + restart).
- MUTABAKAT: restart seciciyi oldurdugu icin SwitchCompleted/Failed
  olayini restart sonrasi YENI surec, diskteki niyetle env'i
  karsilastirarak yazar. switch_id ile Requested -> Completed zinciri
  eksiksizdir.
- Esitlik bozma (determinizm): ayni pct'de mevcut kaynak kazanir,
  sonra alfabetik.

TUM olaylar Gozlem Katmani omurgasina yazilir (akis: "otonom", ayri log
altyapisi YOK): AutonomEvaluated, AutonomSwitchRequested/Aborted/
Completed/Failed, AutonomOn/Off, AutonomConfigChanged, AutonomUserToggle,
SelectorBoot. Her olayda actor (user|system), git_sha ve config anlik
goruntusu bulunur; Evaluated ham girdileri (equity_now, baseline,
baseline_ts, baseline_source, tam siralama) tasir: karar fonksiyonu
salt bu girdilerden yeniden oynatilabilir.

Bilincli ertelenenler (kullanici karari): histerezis, dead-band, esik
optimizasyonu, secim metrigi degisikligi.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

DURUM_DOSYA = "OTONOM_MOD.json"
NIYET_DOSYA = "OTONOM_GECIS_NIYET.json"
VARSAYILAN_PENCERE_DK = 60

KONTROL_SN = float(os.getenv("OTONOM_KONTROL_SN", "300"))
MIN_ISLEM = int(os.getenv("OTONOM_MIN_ISLEM", "1"))
COOLDOWN_SN = float(os.getenv("OTONOM_COOLDOWN_SN", "1200"))  # 24 Tem kullanici karari: 15dk -> 20dk
TASFIYE_SN = float(os.getenv("OTONOM_TASFIYE_SN", "180"))
DOGAL_SN = float(os.getenv("OTONOM_DOGAL_SN", "600"))   # hibrit dogal faz
# 23 Tem kullanici karari: saatlik artisi bu esigin ALTINDA kalan motor
# "negatif" sayilir; hepsi altindaysa sistem beklemeye gecer + SALTER INER
POZITIF_ESIK = float(os.getenv("OTONOM_POZITIF_ESIK", "1.5"))  # 24 Tem: pencere 30dk ile birlikte 1.0->1.5
# 24 Tem kullanici karari (egim kurali): liderlik farki bu marjin
# ICINDEyse egim (son iki tur pct farki) karar verir; sonen lidere
# marj icinden gecilmez (veto). Marj disinda seviye kazanir.
MARJ_PUAN = float(os.getenv("OTONOM_MARJ_PUAN", "1.0"))
# 24 Tem sabah (kullanici sikayeti: cuce-kasa/zombi liderligi): kasasi
# bu esigin altindaki motor liderlige aday olamaz
MIN_KASA_USD = float(os.getenv("OTONOM_MIN_KASA_USD", "150"))
# 24 Tem (kullanici onayi, "firsat sarti"): aday motorun paper'inda son
# FIRSAT_DK icinde YENI giris yoksa gecis atlanir (firsat_yok): MTM'le
# lider gorunen ama masaya kagit koymayan motora bosa tasinilmaz
FIRSAT_DK = float(os.getenv("OTONOM_FIRSAT_DK", "10"))
# kullanici netlestirmesi: sart YALNIZ kagit-bazli runner motorlara
FIRSAT_MOTORLAR = set(os.getenv("OTONOM_FIRSAT_MOTORLAR", "r1,r2").split(","))

_yazici = None
_git_sha_cache: str | None = None


def _data_dir() -> Path:
    return Path(os.getenv("MOMENTUM_DATA_DIR", "data"))


def _git_sha() -> str:
    global _git_sha_cache
    if _git_sha_cache is None:
        try:
            _git_sha_cache = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=Path(__file__).resolve().parents[2],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or "bilinmiyor"
        except Exception:
            _git_sha_cache = "bilinmiyor"
    return _git_sha_cache


def config_anlik() -> dict:
    return {"kontrol_sn": KONTROL_SN, "min_islem": MIN_ISLEM,
            "cooldown_sn": COOLDOWN_SN, "tasfiye_sn": TASFIYE_SN,
            "dogal_sn": DOGAL_SN, "pozitif_esik": POZITIF_ESIK,
            "marj_puan": MARJ_PUAN}


def olay_yaz(kind: str, payload: dict, actor: str = "system") -> dict:
    """Otonom olayini Gozlem Katmani omurgasina yazar (akis: otonom)."""
    global _yazici
    if _yazici is None:
        from hibrit_trader.gozlem.yazici import OlayYazici
        kok = Path(os.getenv("GOZLEM_DATA_DIR", str(_data_dir() / "gozlem")))
        kok.mkdir(parents=True, exist_ok=True)
        _yazici = OlayYazici(kok)
    tam = {"actor": actor, "git_sha": _git_sha(), **payload}
    return _yazici.yaz("otonom", kind, tam, src="otonom")


# ---------------------------------------------------------------- durum

def durum_oku() -> dict:
    try:
        d = json.loads((_data_dir() / DURUM_DOSYA).read_text())
    except (OSError, ValueError):
        d = {}
    if "acik" in d and "user_enabled" not in d:   # eski format gocu
        d["user_enabled"] = bool(d.pop("acik"))
    d.setdefault("user_enabled", False)
    d.setdefault("system_enabled", True)
    d.setdefault("pencere_dk", VARSAYILAN_PENCERE_DK)
    d.setdefault("son_gecis_ts", 0.0)
    return d


def durum_yaz(d: dict) -> None:
    p = _data_dir() / DURUM_DOSYA
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, p)


# ---------------------------------------------------------------- skor

def kayan_degisim(motor: str, pencere_dk: float) -> dict:
    """Kayan pencere degisimi: eq_simdi / eq_pencere_once - 1.

    Ham girdiler donulur (denetlenebilirlik): equity_now, equity_baseline,
    baseline_ts, baseline_source (equity_ornek|gerceklesen|start),
    acik_poz_unreal.
    eq_simdi = start + gerceklesen pnl + ACIK POZ GERCEKLESMEMIS K/Z
    (24 Tem kullanici karari: acik pozisyonlar aninda yansisin). MTM
    girdisi state.last_price'tan gelir ve karar olayina ham deger olarak
    yazildigi icin replay logdan yapilir.
    """
    from hibrit_trader.jsonl_onbellek import equity_satirlari, islem_satirlari
    d = _data_dir()
    start = 1000.0
    created = 0.0
    unreal = 0.0
    try:
        st = json.loads((d / f"{motor}_state.json").read_text())
        start = float(st.get("start_balance") or 1000.0)
        created = float(st.get("created_ts") or 0.0)
        for pz in (st.get("positions") or []):
            giris = float(pz.get("entry_price") or 0)
            son = float(pz.get("last_price") or giris)
            maliyet = float(pz.get("cost_usd") or 0)
            if giris > 0 and maliyet > 0:
                unreal += maliyet * (son / giris - 1)
    except (OSError, ValueError):
        pass
    t0 = time.time() - pencere_dk * 60
    kum = start
    kum_t0 = None
    n = 0
    for ts, pnl, gecerli, _tid in islem_satirlari(d / f"{motor}_trades.jsonl"):
        if not gecerli or ts < created:
            continue
        if ts > t0 and kum_t0 is None:
            kum_t0 = kum
        kum += pnl
        if ts >= max(t0, created):
            n += 1
    if kum_t0 is None:                     # pencerede islem yok
        baz, baz_ts, baz_kaynak = kum, t0, "gerceklesen"
    else:
        baz, baz_ts, baz_kaynak = kum_t0, t0, "gerceklesen"
    for ts_e, eq_e in reversed(equity_satirlari(d / f"{motor}_equity.jsonl")):
        if ts_e <= t0:
            if ts_e >= created:
                baz, baz_ts, baz_kaynak = eq_e, ts_e, "equity_ornek"
            break
    if created > t0 and baz_kaynak == "gerceklesen" and kum_t0 is None:
        baz_kaynak = "start"               # motor pencereden genc
    eq_now = kum + unreal
    pct = (eq_now / baz - 1) * 100 if baz > 0 else 0.0
    return {"pct": round(pct, 3), "islem": n,
            "equity_now": round(eq_now, 2),
            "acik_poz_unreal": round(unreal, 2),
            "equity_baseline": round(baz, 2),
            "baseline_ts": round(baz_ts, 3), "baseline_source": baz_kaynak}


def pencere_skorlari(pencere_dk: float,
                     kaynaklar: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in kaynaklar:
        if (_data_dir() / f"{m}_trades.jsonl").exists() \
                or (_data_dir() / f"{m}_state.json").exists():
            out[m] = kayan_degisim(m, pencere_dk)
    return out


def lider_bul(skorlar: dict[str, dict], mevcut: str) -> str | None:
    """Deterministik lider: en yuksek pct; esitlikte mevcut, sonra alfabetik."""
    if not skorlar:
        return None
    return min(skorlar,
               key=lambda m: (-skorlar[m]["pct"], 0 if m == mevcut else 1, m))


def aday_sec(skorlar: dict[str, dict], mevcut: str,
             min_islem: int = MIN_ISLEM,
             esik: float | None = None,
             egimler: dict[str, float] | None = None,
             marj: float | None = None) -> str | None:
    """STATE-TRIGGER + EGIM KURALI (24 Tem):
    1) Fark belirginse (marj disinda) en yuksek seviye kazanir.
    2) Zirvenin marj icindeki adaylari arasinda EGIM kazanir
       (son iki tur pct farki; egim verisi yoksa seviye).
    3) Veto: mevcut uygunken, SONEN (egim<0) bir adaya marj icinden
       gecilmez. ZIRVEDE OLANDA KAL korunur."""
    if esik is None:
        esik = POZITIF_ESIK
    if marj is None:
        marj = MARJ_PUAN
    uygun = {m: s for m, s in skorlar.items()
             if s["islem"] >= min_islem and s["pct"] >= esik
             and s.get("equity_now", MIN_KASA_USD) >= MIN_KASA_USD}
    if not uygun:
        return None
    en_yuksek = max(s["pct"] for s in uygun.values())
    marj_ici = {m: s for m, s in uygun.items()
                if s["pct"] >= en_yuksek - marj}
    def _egim(m):
        if egimler is None or egimler.get(m) is None:
            return None
        return egimler[m]
    # 24 Tem fix: egim onceligi YALNIZ pozitif egimlilere (sifir/negatif
    # egim "yukselen" degildir; zombi-sabit motor egim kazanamaz)
    yukselen = {m: s for m, s in marj_ici.items()
                if (_egim(m) or 0) > 0}
    if yukselen:
        # marj icinde YUKSELEN varsa: en dik egim, esitlikte seviye
        aday = min(yukselen, key=lambda m: (
            -_egim(m), -yukselen[m]["pct"], 0 if m == mevcut else 1, m))
    else:
        # kimse yukselmiyorsa SEVIYE kazanir (egim kiyasi yapilmaz)
        aday = lider_bul(marj_ici, mevcut)
    if aday is None or aday == mevcut:
        return None
    if mevcut in uygun and uygun[aday]["pct"] <= uygun[mevcut]["pct"]             and (_egim(aday) is None or _egim(aday) <= (_egim(mevcut) or 0)):
        return None
    if (mevcut in uygun and _egim(aday) is not None and _egim(aday) < 0
            and uygun[aday]["pct"] - uygun[mevcut]["pct"] <= marj):
        return None   # veto: sonen lidere marj icinden gecme
    return aday


# ------------------------------------------------------------ mutabakat

def gecis_mutabakati(mevcut: str) -> dict | None:
    """Restart sonrasi: diskteki gecis niyetini env ile karsilastir,
    SwitchCompleted/Failed olayini yaz, niyeti sil. P0 madde 3."""
    yol = _data_dir() / NIYET_DOSYA
    try:
        niyet = json.loads(yol.read_text())
    except (OSError, ValueError):
        return None
    basari = (mevcut == niyet.get("to"))
    kind = "AutonomSwitchCompleted" if basari else "AutonomSwitchFailed"
    payload = {
        "switch_id": niyet.get("switch_id"),
        "eval_id": niyet.get("eval_id"),
        "from": niyet.get("from"), "to": niyet.get("to"),
        "duration_sec": round(time.time() - float(niyet.get("bas_ts") or 0), 1),
        "positions_closed": niyet.get("positions_closed"),
        "tasfiye_sure_sec": niyet.get("tasfiye_sure_sec"),
        "env_kaynak": mevcut,
        "success": basari,
    }
    olay_yaz(kind, payload)
    yol.unlink(missing_ok=True)
    return payload


# ---- GOVERNOR asgari canli korumalari (26 Tem P0-4) --------------------
GOV_GUNLUK_KAYIP_USD = float(os.getenv("GOV_GUNLUK_KAYIP_USD", "40"))
GOV_GUNLUK_GECIS_MAX = int(os.getenv("GOV_GUNLUK_GECIS_MAX", "6"))
_GOV_SAYAC_DOSYA = "gov_sayac.json"


def _gov_sayac_oku() -> dict:
    try:
        s = json.loads((_data_dir() / _GOV_SAYAC_DOSYA).read_text())
    except (OSError, ValueError):
        s = {}
    gun = time.strftime("%Y-%m-%d", time.gmtime())
    if s.get("gun") != gun:
        s = {"gun": gun, "gecis_n": 0, "kayip_bildirildi": False}
    return s


def _gov_sayac_yaz(s: dict) -> None:
    tmp = _data_dir() / (_GOV_SAYAC_DOSYA + ".tmp")
    tmp.write_text(json.dumps(s))
    os.replace(tmp, _data_dir() / _GOV_SAYAC_DOSYA)


def _gov_gecis_kaydet() -> None:
    s = _gov_sayac_oku()
    s["gecis_n"] = int(s.get("gecis_n", 0)) + 1
    _gov_sayac_yaz(s)


def _canli_gun_pnl() -> float:
    """Bugunku (UTC) canli gerceklesen PnL (governor kayip butcesi)."""
    gun = time.strftime("%Y-%m-%d", time.gmtime())
    toplam = 0.0
    try:
        for ln in open(_data_dir() / "canli_trades.jsonl"):
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            if t.get("type"):
                continue
            if time.strftime("%Y-%m-%d",
                             time.gmtime(float(t.get("ts") or 0))) == gun:
                toplam += float(t.get("pnl_usd") or 0)
    except OSError:
        pass
    return round(toplam, 2)


def _governor_salter_kaldir_gun_donumu() -> None:
    icerik = _salter_icerik()
    if icerik and "governor:" in icerik:
        s = _gov_sayac_oku()
        if not s.get("kayip_bildirildi"):     # yeni gun: butce tazelendi
            (_data_dir() / "CANLI_DUR").unlink(missing_ok=True)
            olay_yaz("GovernorSalterKalkti", {"neden": "gun_donumu"})


def governor_kontrol(notify) -> dict:
    """Mutlak sigortalar: gunluk kayip butcesi + gecis tavani.
    Kayip asiminda governor-yazarli salter (yalniz kullanici veya gun
    donumu kaldirir); gecis tavani asiminda yeni gecis izni yok."""
    s = _gov_sayac_oku()
    _governor_salter_kaldir_gun_donumu()
    pnl = _canli_gun_pnl()
    kayip_asildi = pnl <= -GOV_GUNLUK_KAYIP_USD
    if kayip_asildi:
        p = _data_dir() / "CANLI_DUR"
        if not p.exists():
            p.write_text(f"governor: gunluk kayip {pnl} <= "
                         f"-{GOV_GUNLUK_KAYIP_USD}")
        if not s.get("kayip_bildirildi"):
            s["kayip_bildirildi"] = True
            _gov_sayac_yaz(s)
            olay_yaz("GovernorKayipFreni", {
                "gun_pnl": pnl, "limit": -GOV_GUNLUK_KAYIP_USD})
            notify(f"[CANLI] GOVERNOR: gunluk kayip {pnl}$ limiti asti, "
                   "yeni girisler DURDU (cikislar acik)")
    return {"gun_pnl": pnl, "kayip_asildi": kayip_asildi,
            "gecis_n": int(s.get("gecis_n", 0)),
            "gecis_izin": int(s.get("gecis_n", 0)) < GOV_GUNLUK_GECIS_MAX}


def _canli_yasak_aileler() -> set:
    """CANLI'ya kapali aileler (26 Tem risk karari: runner). Golge/GO
    olcumu etkilenmez; kisit yalniz canli surucu katmaninda.

    Iki kaynak birlesir: EDGE_CANLI_AILE_YASAK listesi ve RUNNER_DONDUR
    tek-anahtari (27 Tem: runner tam dondurma)."""
    from hibrit_trader import runner_dondur

    ham = os.getenv("EDGE_CANLI_AILE_YASAK", "")
    yasak = {a.strip() for a in ham.split(",") if a.strip()}
    if runner_dondur.aktif():
        yasak.add(runner_dondur.AILE)
    return yasak


def _canli_skor_suz(skorlar: dict) -> tuple:
    """Yasakli aile uyelerini skor tablosundan cikarir.
    Donis: (suzulmus_skorlar, sirali_yasak_listesi | None)."""
    yasak = _canli_yasak_aileler()
    if not yasak:
        return skorlar, None
    from hibrit_trader.edge.cekirdek import KATALOG
    uyeler = {m for a in yasak
              for m in KATALOG.get(a, {}).get("uyeler", [])}
    return ({k: v for k, v in skorlar.items() if k not in uyeler},
            sorted(yasak))


def _edge_canli_turu(skorlar: dict, mevcut: str, d: dict,
                     notify) -> str:
    """EDGE CANLI SURUCU turu (26 Tem). Donis: "devam" | "restart".
    Karar eslemesi: cash -> edge-salter; kal -> salteri kaldir;
    gecis -> ortak gecis borusu. Hata -> mevcutta kal (fallback).
    Cift cekirdek (26 Tem risk karari): tam evren cekirdegi golge/GO
    kaydi icin calisir; canli hat, EDGE_CANLI_AILE_YASAK suzgecinden
    gecmis skorlarla ayri cekirdekten surulur."""
    global _CEKIRDEK, _CEKIRDEK_CANLI
    from hibrit_trader.edge.cekirdek import Cekirdek
    eval_id = f"ev-{int(time.time() * 1000)}"
    gov = governor_kontrol(notify)
    v2, v2c, hedef, hedef_tam, yasak, hata = (None,) * 6
    try:
        if _CEKIRDEK is None:
            _CEKIRDEK = Cekirdek()
        v2 = _CEKIRDEK.karar(skorlar)          # tam evren: golge/GO kaydi
        hedef_tam = _CEKIRDEK.temsilci(skorlar)
        canli_skor, yasak = _canli_skor_suz(skorlar)
        if yasak:
            if _CEKIRDEK_CANLI is None:
                _CEKIRDEK_CANLI = Cekirdek()
            v2c = _CEKIRDEK_CANLI.karar(canli_skor)
            hedef = _CEKIRDEK_CANLI.temsilci(canli_skor)
        else:
            v2c, hedef = v2, hedef_tam
        if v2c["aile"] == "cash" or hedef is None:
            karar = "edge_cash"
        elif hedef == mevcut:
            karar = "kal"
        else:
            karar = "gecis"
    except Exception as e:  # noqa: BLE001
        hata = str(e)[:120]
        karar = "cekirdek_hata_kal"
        v2c = v2 = {"surum": "v2", "katman": "cekirdek_hata",
                    "aile": None, "hata": hata}
    cooldown_kalan = max(
        0.0, COOLDOWN_SN - (time.time() - float(d["son_gecis_ts"])))
    if karar == "gecis":
        if gov["kayip_asildi"]:
            karar = "governor_kayip"
        elif not gov["gecis_izin"]:
            karar = "governor_gecis_tavani"
        elif cooldown_kalan > 0:
            karar = "cooldown"
        elif hedef in FIRSAT_MOTORLAR and not firsat_var(hedef)[0]:
            karar = "firsat_yok"
    if karar == "edge_cash":
        if _edge_salter_indir("cekirdek CASH karari"):
            notify("[CANLI] EDGE: CASH karari, yeni girisler durdu "
                   "(cikislar acik)")
    elif karar in ("kal", "gecis") and not gov["kayip_asildi"]:
        if _edge_salter_kaldir():
            notify("[CANLI] EDGE: CASH bitti, girisler acildi")
    switch_id = (f"sw-{int(time.time() * 1000)}"
                 if karar == "gecis" else None)
    olay_yaz("AutonomEvaluated", {
        "eval_id": eval_id, "switch_id": switch_id,
        "karar_ureticisi": "edge_v2", "window_min": d["pencere_dk"],
        "current_live_engine": mevcut, "ranking": skorlar,
        "decision": karar, "aday": hedef, "v2": v2, "governor": gov,
        "aday_tam": hedef_tam, "canli_yasak_aileler": yasak,
        "v2_canli": (v2c if yasak else None),
        "cooldown_remaining_sec": round(cooldown_kalan, 1),
        "config": config_anlik()})
    _golge_olayla(skorlar, mevcut, karar, hedef_tam, eval_id,
                  v2_hazir=v2, temsilci_hazir=hedef_tam)
    if karar != "gecis":
        return "devam"
    notify(f"[CANLI] EDGE GECIS KARARI: {mevcut} -> {hedef} "
           f"(aile {v2c['aile']}, guven {v2c.get('guven')})")

    def _edge_dogrulayici():
        son_skor = pencere_skorlari(float(d["pencere_dk"]),
                                    sorted(skorlar))
        try:
            suzuk, y = _canli_skor_suz(son_skor)
            cek = _CEKIRDEK_CANLI if y else _CEKIRDEK
            v2y = cek.karar(suzuk)
            t = cek.temsilci(suzuk)
            return (t if v2y.get("aile") not in (None, "cash") else None,
                    son_skor)
        except Exception:  # noqa: BLE001
            return None, son_skor
    sonuc = _gecis_uygula(mevcut, hedef, eval_id, switch_id,
                          v2.get("guven"), d, _edge_dogrulayici, notify,
                          "edge_karari")
    return "restart" if sonuc == "tamam" else "devam"


_CEKIRDEK = None
_CEKIRDEK_CANLI = None      # 26 Tem: aile-yasakli canli surucu cekirdegi


def _golge_olayla(skorlar: dict, mevcut: str, karar: str,
                  aday: str | None, eval_id: str | None,
                  v2_hazir: dict | None = None,
                  temsilci_hazir: str | None = None) -> None:
    """Edge zinciri GOLGE kiyasi (25 Tem HAT 2; 26 Tem v2 cekirdek):
    ayni girdiler, sifir etki; hata golgeyi oldurur, seciciyi ASLA.
    Fallback merdiveni: cekirdek -> girdi_yok -> cekirdek_hata(legacy)."""
    global _CEKIRDEK
    try:
        from hibrit_trader.edge.cekirdek import Cekirdek
        from hibrit_trader.edge.golge import golge_degerlendir
        if _CEKIRDEK is None:
            _CEKIRDEK = Cekirdek()
        try:
            if v2_hazir is not None:      # surucu turu: cift-ilerletme yok
                v2, golge_aday = v2_hazir, temsilci_hazir
            else:
                v2 = _CEKIRDEK.karar(skorlar)
                golge_aday = _CEKIRDEK.temsilci(skorlar)
        except Exception as e:  # noqa: BLE001  (cekirdek_hata katmani)
            v2 = {"surum": "v2", "katman": "cekirdek_hata",
                  "aile": None, "dagilim": None, "guven": 0.0,
                  "hata": str(e)[:120]}
            golge_aday = aday if karar == "gecis" else (
                mevcut if karar in ("kal", "cooldown", "firsat_yok",
                                    "otonom_kapali") else None)
        eski = golge_degerlendir(skorlar, mevcut, karar, aday,
                                 esik=POZITIF_ESIK)
        eski["golge_aday"] = golge_aday          # KPI: v2 temsilcisi esas
        eski["paylar"] = ({} if golge_aday is None else {golge_aday: 1.0})
        eski["uyum"] = golge_aday == eski.get("legacy_hedef")
        # H8: tam girdi anlik goruntusu (pct edgeler'de; islem burada)
        eski["girdi_islem"] = {m: int(s.get("islem") or 0)
                               for m, s in skorlar.items()}
        olay_yaz("EdgeShadowEvaluated",
                 {"eval_id": eval_id, **eski, "v2": v2})
        # H9: panel icin son karar dosyasi (atomik, kucuk)
        try:
            son = {"ts": time.time(), "eval_id": eval_id, **v2,
                   "surucu": "canli" if edge_canli_aktif() else "golge",
                   "golge_aday": golge_aday,
                   "legacy_hedef": eski.get("legacy_hedef")}
            tmp = _data_dir() / "edge_karar_son.json.tmp"
            tmp.write_text(json.dumps(son))
            os.replace(tmp, _data_dir() / "edge_karar_son.json")
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        log.debug("edge golge hatasi", exc_info=True)


def _gecis_uygula(mevcut: str, aday: str, eval_id: str, switch_id: str,
                  gerekce_pct, d: dict, dogrulayici, notify,
                  neden: str) -> str:
    """Ortak canli kaynak gecis borusu (26 Tem: legacy + edge surucusu).

    Hibrit tasfiye -> duzlesme -> son dogrulama -> niyet -> swap.
    dogrulayici() -> (yeni_aday, ranking): aday degistiyse iptal.
    Donis: "tamam" (swap tetiklendi, restart geliyor) | "iptal"."""
    from hibrit_trader.canli_session import TASFIYE_FILE
    acik = _canli_acik_poz()
    olay_yaz("AutonomSwitchRequested", {
        "switch_id": switch_id, "eval_id": eval_id,
        "from": mevcut, "to": aday, "reason": neden,
        "leader_change_pct": gerekce_pct,
        "cooldown_remaining_sec": 0.0,
        "open_positions": acik, "config": config_anlik()})
    notify(f"[CANLI] GECIS ({neden}): {mevcut} -> {aday}; hibrit "
           f"tasfiye: {acik} poz, dogal cikisa {DOGAL_SN:.0f}sn")
    tasfiye = _data_dir() / TASFIYE_FILE
    bas = time.time()
    zorla_ts = bas + DOGAL_SN
    # pid = sahiplik damgasi (25 Tem P0): restart sonrasi yeni surec
    # bu dosyayi YETIM tanir, zorla satis ateslenmez
    tasfiye.write_text(json.dumps({
        "switch_id": switch_id, "from": mevcut, "to": aday,
        "zorla_ts": zorla_ts, "bas_ts": bas, "pid": os.getpid()}))
    duz = False
    dogal_sonu_poz = None
    while time.time() - bas < DOGAL_SN + TASFIYE_SN:
        time.sleep(5)
        if dogal_sonu_poz is None and time.time() >= zorla_ts:
            dogal_sonu_poz = _canli_acik_poz()   # zorlamaya kalanlar
        if _canli_acik_poz() == 0:
            duz = True
            break
    tasfiye.unlink(missing_ok=True)
    kalan = _canli_acik_poz()
    if dogal_sonu_poz is None:       # dogal fazda duzlesti
        dogal_sonu_poz = 0 if duz else kalan
    dogal_kapatilan = max(0, acik - max(dogal_sonu_poz, 0))
    zorla_kapatilan = max(0, max(dogal_sonu_poz, 0) - max(kalan, 0))
    if not duz:
        olay_yaz("AutonomSwitchAborted", {
            "switch_id": switch_id, "reason": "timeout",
            "asama": "tasfiye", "acik_kalan_poz": kalan,
            "dogal_kapatilan": dogal_kapatilan,
            "zorla_kapatilan": zorla_kapatilan})
        notify("[CANLI] GECIS: tasfiye zaman asimi, iptal")
        return "iptal"
    son_aday, son_ranking = dogrulayici()
    if son_aday != aday:
        olay_yaz("AutonomSwitchAborted", {
            "switch_id": switch_id, "reason": "leader_changed",
            "asama": "son_dogrulama", "eski_aday": aday,
            "yeni_aday": son_aday, "ranking": son_ranking})
        notify("[CANLI] GECIS: hedef degisti, iptal")
        return "iptal"
    d["son_gecis_ts"] = time.time()
    durum_yaz(d)
    _gov_gecis_kaydet()
    niyet = {"switch_id": switch_id, "eval_id": eval_id,
             "from": mevcut, "to": aday, "bas_ts": bas,
             "tasfiye_sure_sec": round(time.time() - bas, 1),
             "positions_closed": acik,
             "dogal_kapatilan": dogal_kapatilan,
             "zorla_kapatilan": zorla_kapatilan}
    yol = _data_dir() / NIYET_DOSYA
    tmp = yol.with_suffix(".tmp")
    tmp.write_text(json.dumps(niyet))
    os.replace(tmp, yol)
    notify(f"[CANLI] GECIS: duzlesti, kaynak {aday} oluyor "
           "(servis restart)")
    _swap_tetikle(aday)
    return "tamam"


# ---- EDGE CANLI SURUCU (26 Tem kullanici talimati) ---------------------
EDGE_CANLI = os.getenv("EDGE_CANLI", "0") == "1"
EDGE_GERI_AL_DOSYA = "EDGE_GERI_AL"


def edge_canli_aktif() -> bool:
    """Edge surucu yetkisi: EDGE_CANLI=1 VE geri-alma bayragi yok.
    TEK-KOMUT ROLLBACK: `touch data/EDGE_GERI_AL` -> anlik golgeye
    doner, legacy devralir (restartsiz). Yeniden yetki: dosyayi silmek
    KULLANICI kararidir."""
    return EDGE_CANLI and not (_data_dir() / EDGE_GERI_AL_DOSYA).exists()


def _salter_icerik() -> str | None:
    try:
        return (_data_dir() / "CANLI_DUR").read_text(errors="replace")
    except OSError:
        return None


def _edge_salter_indir(neden: str) -> bool:
    """Edge-yazarli salter: dosya varsa (kim koyduysa) dokunmaz;
    yoksa 'edge:' imzasiyla yazar. Oncelik: kullanici > governor >
    edge > otonom."""
    p = _data_dir() / "CANLI_DUR"
    if p.exists():
        return False
    p.write_text(f"edge: {neden}")
    return True


def _edge_salter_kaldir() -> bool:
    icerik = _salter_icerik()
    if icerik is None or "edge:" not in icerik:
        return False
    (_data_dir() / "CANLI_DUR").unlink(missing_ok=True)
    return True


def yetim_tasfiye_mutabakati() -> dict | None:
    """Boot'ta diskte CANLI_TASFIYE varsa YETIMDIR: onu bekleyen secici
    dongusu restart'ta oldu, tasfiyeyi tamamlayacak kimse yok. Dosya
    silinir, olay + bildirim yazilir (24 Tem yetim vakasi: zamanlanmis
    yanlis zorla-satis elle durdurulmustu; bu kalici fix, P0 madde 1)."""
    from hibrit_trader.canli_session import TASFIYE_FILE
    from hibrit_trader.killswitch import notify
    yol = _data_dir() / TASFIYE_FILE
    if not yol.exists():
        return None
    try:
        icerik = json.loads(yol.read_text())
    except (OSError, ValueError):
        icerik = {"bozuk": True}
    yol.unlink(missing_ok=True)
    payload = {"switch_id": icerik.get("switch_id"),
               "from": icerik.get("from"), "to": icerik.get("to"),
               "zorla_ts": icerik.get("zorla_ts"),
               "yetim_pid": icerik.get("pid"),
               "zorla_gecmis_miydi": bool(
                   icerik.get("zorla_ts")
                   and time.time() >= float(icerik["zorla_ts"] or 0))}
    olay_yaz("AutonomOrphanTasfiyeCleared", payload)
    notify("[CANLI] OTONOM: yetim tasfiye temizlendi "
           f"(switch {payload['switch_id']}), gecis iptal sayildi")
    return payload


def _salter_indir(neden: str) -> None:
    """Kural 3 eki (23 Tem kullanici karari): tum motorlar <=0 iken
    salter de iner (CANLI_DUR): yeni canli giris yok, cikislar surer.
    TEK-YAZAR (26 Tem CRITICAL-3): sistem KENDI koydugu salteri
    yonetir; kullanicinin (panel) koydugunu ASLA ezmez/kaldirmaz."""
    p = _data_dir() / "CANLI_DUR"
    if p.exists() and "otonom:" not in p.read_text(errors="replace"):
        log.warning("salter kullanici-yazarli; sistem dokunmuyor")
        return
    p.write_text(f"otonom: {neden}")


def _salter_kaldir() -> None:
    p = _data_dir() / "CANLI_DUR"
    try:
        icerik = p.read_text(errors="replace")
    except OSError:
        return
    if "otonom:" not in icerik:              # panel/kullanici yazdi
        log.warning("salter kullanici-yazarli; sistem KALDIRMIYOR "
                    "(tek-yazar kurali)")
        return
    p.unlink(missing_ok=True)


def firsat_var(motor: str, dk: float | None = None) -> tuple[bool, float]:
    """Aday motorun son YENI girisinden bu yana gecen sure <= dk mi?
    Kaynak: state acik pozisyonlarin opened_ts'i + defterin son
    girisleri (trade_id epoch'u). Doner: (var_mi, son_giris_yasi_sn)."""
    if dk is None:
        dk = FIRSAT_DK
    d = _data_dir()
    son = 0.0
    try:
        st = json.loads((d / f"{motor}_state.json").read_text())
        for p in (st.get("positions") or []):
            son = max(son, float(p.get("opened_ts") or 0))
    except (OSError, ValueError):
        pass
    try:
        from hibrit_trader.jsonl_onbellek import islem_satirlari
        for ts, _pnl, gecerli, tid in islem_satirlari(
                d / f"{motor}_trades.jsonl")[-50:]:
            if not gecerli:
                continue
            try:
                son = max(son, float(str(tid).rsplit("-", 1)[-1]))
            except ValueError:
                continue
    except Exception:
        pass
    yas = time.time() - son if son > 0 else 1e9
    return (yas <= dk * 60, yas)


def _canli_acik_poz() -> int:
    try:
        st = json.loads((_data_dir() / "canli_state.json").read_text())
        return len(st.get("positions") or [])
    except (OSError, ValueError):
        return -1   # okunamadi: guvenli taraf, gecis yapma


def _swap_tetikle(motor: str) -> None:
    kok = Path(__file__).resolve().parents[2]
    log_f = open(_data_dir() / "canli_swap.log", "ab")
    subprocess.Popen(
        [str(kok / ".venv" / "bin" / "python"),
         str(kok / "scripts" / "canli_swap.py"), motor],
        stdout=log_f, stderr=log_f, start_new_session=True)


# ----------------------------------------------------------------- ana

def kontrol_dongusu() -> None:
    """Panel icinde daemon thread. Boot'ta mutabakat + SelectorBoot;
    her turda degerlendir, olayla, gerekirse gecis."""
    from hibrit_trader.canli_session import DESTEKLENEN_KAYNAKLAR, TASFIYE_FILE
    from hibrit_trader.killswitch import notify
    kaynaklar = sorted(DESTEKLENEN_KAYNAKLAR)
    # 24 Tem: egim bakisi 2 tur (10dk): 30dk pencerede ardisik turlar
    # verinin cogunu paylasir, 1-turluk fark mikroskobik kalirdi
    gecmis_skorlar: dict[str, list] = {}
    mevcut = os.getenv("CANLI_KAYNAK_MOTOR", "r1").strip().lower()
    mut = gecis_mutabakati(mevcut)
    yetim = yetim_tasfiye_mutabakati()
    d = durum_oku()
    olay_yaz("SelectorBoot", {
        "current_live_engine": mevcut, "durum": d,
        "mutabakat": None if mut is None else mut.get("switch_id"),
        "yetim_tasfiye": None if yetim is None else yetim.get("switch_id"),
        "config": config_anlik(), "kaynaklar": kaynaklar})
    while True:
        time.sleep(KONTROL_SN)
        try:
            d = durum_oku()
            mevcut = os.getenv("CANLI_KAYNAK_MOTOR", "r1").strip().lower()
            skorlar = pencere_skorlari(float(d["pencere_dk"]), kaynaklar)
            egimler = {m: (round(skorlar[m]["pct"] - gecmis_skorlar[m][0], 3)
                           if len(gecmis_skorlar.get(m, [])) >= 2 else None)
                       for m in skorlar}
            for m in skorlar:
                skorlar[m]["egim"] = egimler[m]
                gecmis_skorlar.setdefault(m, []).append(skorlar[m]["pct"])
                gecmis_skorlar[m] = gecmis_skorlar[m][-2:]
            lider = lider_bul(skorlar, mevcut)
            lider_pct = skorlar[lider]["pct"] if lider else 0.0
            if not d["user_enabled"]:
                # OTONOM kapaliyken de GOLGE kiyasi birikir (25 Tem):
                # legacy pasif = mevcutta kal; secici hicbir sey yapmaz
                _golge_olayla(skorlar, mevcut, "otonom_kapali", None, None)
                continue
            # ---- EDGE CANLI SURUCU dali (26 Tem kullanici talimati) ----
            if EDGE_CANLI:
                if not edge_canli_aktif():
                    # geri-alma bayragi: edge salteri asili kalmasin,
                    # legacy asagida devralir
                    _edge_salter_kaldir()
                else:
                    if _edge_canli_turu(skorlar, mevcut, d,
                                        notify) == "restart":
                        return
                    continue
            eval_id = f"ev-{int(time.time() * 1000)}"
            # kural 3-4: system_enabled (saltere dokunmaz, yalniz secim)
            if all(s["pct"] < POZITIF_ESIK for s in skorlar.values()):
                if d["system_enabled"]:
                    d["system_enabled"] = False
                    durum_yaz(d)
                    _salter_indir(f"tum motorlar esik altinda (<%{POZITIF_ESIK})")
                    olay_yaz("AutonomOff", {
                        "reason": "ALL_MOTORS_BELOW_THRESHOLD",
                        "esik": POZITIF_ESIK,
                        "eval_id": eval_id, "ranking": skorlar,
                        "leader": lider, "leader_change_pct": lider_pct,
                        "salter": "kapatildi",
                        "config": config_anlik()})
                    notify(f"[CANLI] OTONOM BEKLEMEDE: tum motorlar "
                           f"%{POZITIF_ESIK} esiginin altinda, SALTER INDI "
                           "(giris yok, cikislar acik)")
            elif (not d["system_enabled"] and lider is not None
                  and lider_pct >= POZITIF_ESIK):
                d["system_enabled"] = True
                durum_yaz(d)
                _salter_kaldir()
                olay_yaz("AutonomOn", {
                    "reason": "LEADER_ABOVE_THRESHOLD", "eval_id": eval_id,
                    "esik": POZITIF_ESIK,
                    "ranking": skorlar, "selected_motor": lider,
                    "selected_change_pct": lider_pct,
                    "salter": "acildi",
                    "config": config_anlik()})
                notify(f"[CANLI] OTONOM DEVAM: pozitif lider {lider} "
                       f"(%{lider_pct}), salter acildi")
            aday = None
            cooldown_kalan = max(
                0.0, COOLDOWN_SN - (time.time() - float(d["son_gecis_ts"])))
            if d["user_enabled"] and d["system_enabled"]:
                aday = aday_sec(skorlar, mevcut, egimler=egimler)
            firsat_ok, firsat_yas = (True, None)
            if aday is not None and aday in FIRSAT_MOTORLAR:
                firsat_ok, firsat_yas = firsat_var(aday)
            if aday is None:
                karar = ("kal" if d["system_enabled"] else "sistem_kapali")
            elif cooldown_kalan > 0:
                karar = "cooldown"
            elif not firsat_ok:
                karar = "firsat_yok"
            else:
                karar = "gecis"
            switch_id = (f"sw-{int(time.time() * 1000)}"
                         if karar == "gecis" else None)
            olay_yaz("AutonomEvaluated", {
                "eval_id": eval_id, "switch_id": switch_id,
                "window_min": d["pencere_dk"],
                "current_live_engine": mevcut,
                "leader_engine": lider, "leader_change_pct": lider_pct,
                "ranking": skorlar,
                "state": {"user_enabled": d["user_enabled"],
                          "system_enabled": d["system_enabled"],
                          "effective": d["user_enabled"] and d["system_enabled"]},
                "decision": karar, "aday": aday,
                "aday_son_giris_yasi_sn": (None if firsat_yas is None
                                           else round(firsat_yas, 1)),
                "cooldown_remaining_sec": round(cooldown_kalan, 1),
                "config": config_anlik()})
            _golge_olayla(skorlar, mevcut, karar, aday, eval_id)
            if karar != "gecis":
                continue
            def _legacy_dogrulayici():
                son_skor = pencere_skorlari(float(d["pencere_dk"]),
                                            kaynaklar)
                son_egim = {m: (round(son_skor[m]["pct"]
                                      - gecmis_skorlar[m][0], 3)
                                if len(gecmis_skorlar.get(m, [])) >= 2
                                else None) for m in son_skor}
                return aday_sec(son_skor, mevcut, egimler=son_egim), son_skor
            if _gecis_uygula(mevcut, aday, eval_id, switch_id, lider_pct,
                             d, _legacy_dogrulayici, notify,
                             "lider_degisti") == "tamam":
                return   # restart geliyor; mutabakati yeni surec yapar
            continue
        except Exception:
            log.exception("otonom secici tur hatasi")
