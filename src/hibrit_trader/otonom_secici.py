"""Otonom kaynak secici (23 Tem 2026, kullanici talebi).

Panel ust menusundeki OTONOM dugmesi acikken calisir: son PENCERE_DK (vars. 60)
dakikanin KAYAN degisiminde zirvede olan motoru bulur (eq_simdi/eq_once-1); canli kaynak farkliysa
once acik canli pozisyonlari tasfiye eder (CANLI_TASFIYE dosyasi,
canli motor "otonom_tasfiye" ile satar), duzlesince mevcut swap
akisini tetikler (canli_swap.py: drop-in + servis restart).

Durum dosyasi: data/OTONOM_MOD.json {"acik", "pencere_dk", "son_gecis_ts"}
  - restart'lara dayanir: dosya durdugu surece otonom mod acik kalir.
Karar gunlugu: data/otonom_secici.jsonl (append-only, her karar yazilir).

Ayarlar (env):
  OTONOM_KONTROL_SN      kontrol araligi (vars. 300)
  OTONOM_MIN_ISLEM       pencere icinde asgari islem sayisi (vars. 3)
  OTONOM_COOLDOWN_SN     iki gecis arasi asgari sure (vars. 900)
  OTONOM_TASFIYE_SN      tasfiye duzlesme beklemesi (vars. 180)

Guvenlik: LIVE_ONAY ve gunluk zarar limitleri AYNEN gecerli kalir;
otonom mod bunlarin ustunde degil altinda calisir. Dugme ACILINCA
salter de acilir (CANLI_DUR silinir: "alip satmaya baslasin").
Dugme kapaninca yalniz otonom secim durur, mevcut kaynak calismaya
devam eder (salter degismez).
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
KARAR_LOG = "otonom_secici.jsonl"
VARSAYILAN_PENCERE_DK = 60

KONTROL_SN = float(os.getenv("OTONOM_KONTROL_SN", "300"))
MIN_ISLEM = int(os.getenv("OTONOM_MIN_ISLEM", "0"))
COOLDOWN_SN = float(os.getenv("OTONOM_COOLDOWN_SN", "900"))
TASFIYE_SN = float(os.getenv("OTONOM_TASFIYE_SN", "180"))


def _data_dir() -> Path:
    return Path(os.getenv("MOMENTUM_DATA_DIR", "data"))


def durum_oku() -> dict:
    try:
        d = json.loads((_data_dir() / DURUM_DOSYA).read_text())
    except (OSError, ValueError):
        return {"acik": False, "pencere_dk": VARSAYILAN_PENCERE_DK,
                "son_gecis_ts": 0.0}
    d.setdefault("acik", False)
    d.setdefault("pencere_dk", VARSAYILAN_PENCERE_DK)
    d.setdefault("son_gecis_ts", 0.0)
    return d


def durum_yaz(d: dict) -> None:
    p = _data_dir() / DURUM_DOSYA
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, p)


def _karar_logla(kayit: dict) -> None:
    kayit["ts"] = time.time()
    with open(_data_dir() / KARAR_LOG, "a") as f:
        f.write(json.dumps(kayit) + "\n")


def kayan_degisim(motor: str, pencere_dk: float) -> dict:
    """Kayan pencere degisimi (23 Tem kullanici formulu):
    eq_simdi / eq_pencere_once - 1.

    eq_simdi = start + gerceklesen pnl toplami (motor defterinden).
    eq_once = equity ornek dosyasindan son ornek <= t0; yoksa gerceklesen
    kumulatif (ts <= t0). Motor pencereden gencse baz start_balance.
    Panel _motor_ozet ile ayni formul; pencere suresi burada ayarlanabilir.
    """
    d = _data_dir()
    start = 1000.0
    created = 0.0
    try:
        st = json.loads((d / f"{motor}_state.json").read_text())
        start = float(st.get("start_balance") or 1000.0)
        created = float(st.get("created_ts") or 0.0)
    except (OSError, ValueError):
        pass
    t0 = time.time() - pencere_dk * 60
    kum = start
    kum_t0 = None
    n = 0
    try:
        with open(d / f"{motor}_trades.jsonl") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    t = json.loads(ln)
                except ValueError:
                    continue
                if t.get("type") or t.get("exit_reason") == "manuel_kapanis":
                    continue
                ts = float(t.get("ts") or 0)
                if ts < created:
                    continue
                if ts > t0 and kum_t0 is None:
                    kum_t0 = kum
                kum += float(t.get("pnl_usd") or 0)
                if ts >= max(t0, created):
                    n += 1
    except OSError:
        pass
    if kum_t0 is None:
        kum_t0 = kum   # pencerede islem yok: degisim 0
    eq_once = kum_t0
    try:
        for ln in (d / f"{motor}_equity.jsonl").read_text().splitlines():
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
                ts_e = float(e["ts"])
            except (ValueError, KeyError):
                continue
            if created <= ts_e <= t0:
                eq_once = float(e["eq"])
    except OSError:
        pass
    pct = (kum / eq_once - 1) * 100 if eq_once > 0 else 0.0
    return {"pct": round(pct, 3), "islem": n}


def pencere_skorlari(pencere_dk: float,
                     kaynaklar: list[str]) -> dict[str, dict]:
    """Motor basina kayan pencere degisim yuzdesi."""
    out: dict[str, dict] = {}
    for m in kaynaklar:
        if (_data_dir() / f"{m}_trades.jsonl").exists() \
                or (_data_dir() / f"{m}_state.json").exists():
            out[m] = kayan_degisim(m, pencere_dk)
    return out


def aday_sec(skorlar: dict[str, dict], mevcut: str,
             min_islem: int = MIN_ISLEM) -> str | None:
    """En yuksek kayan degisimli motor; pozitif degisim sarti.
    ZIRVEDE OLANDA KAL: mevcut kaynak en yuksekse gecis yok."""
    uygun = {m: s for m, s in skorlar.items()
             if s["islem"] >= min_islem and s["pct"] > 0}
    if not uygun:
        return None
    aday = max(uygun, key=lambda m: uygun[m]["pct"])
    if aday == mevcut:
        return None
    if mevcut in uygun and uygun[aday]["pct"] <= uygun[mevcut]["pct"]:
        return None
    return aday


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


def kontrol_dongusu() -> None:
    """Panel icinde daemon thread. Her turda: mod acik mi, aday var mi,
    cooldown gecti mi; gecis = tasfiye -> duzlesme -> swap (restart)."""
    from hibrit_trader.canli_session import DESTEKLENEN_KAYNAKLAR, TASFIYE_FILE
    from hibrit_trader.killswitch import notify
    kaynaklar = sorted(DESTEKLENEN_KAYNAKLAR)
    while True:
        time.sleep(KONTROL_SN)
        try:
            d = durum_oku()
            if not d["acik"]:
                continue
            mevcut = os.getenv("CANLI_KAYNAK_MOTOR", "r1").strip().lower()
            skorlar = pencere_skorlari(float(d["pencere_dk"]), kaynaklar)
            aday = aday_sec(skorlar, mevcut)
            if aday is None:
                _karar_logla({"karar": "kal", "mevcut": mevcut,
                              "skorlar": skorlar})
                continue
            if time.time() - float(d["son_gecis_ts"]) < COOLDOWN_SN:
                _karar_logla({"karar": "cooldown", "mevcut": mevcut,
                              "aday": aday, "skorlar": skorlar})
                continue
            _karar_logla({"karar": "gecis_basla", "mevcut": mevcut,
                          "aday": aday, "skorlar": skorlar})
            notify(f"[CANLI] OTONOM GECIS: {mevcut} -> {aday} "
                   f"(son {d['pencere_dk']}dk degisim "
                   f"%{skorlar[aday]['pct']}, {skorlar[aday]['islem']} islem); "
                   "tasfiye basladi")
            tasfiye = _data_dir() / TASFIYE_FILE
            tasfiye.write_text(f"otonom {mevcut}->{aday}")
            bas = time.time()
            duz = False
            while time.time() - bas < TASFIYE_SN:
                time.sleep(5)
                if _canli_acik_poz() == 0:
                    duz = True
                    break
            if not duz:
                tasfiye.unlink(missing_ok=True)
                _karar_logla({"karar": "tasfiye_zaman_asimi",
                              "aday": aday, "acik_poz": _canli_acik_poz()})
                notify("[CANLI] OTONOM: tasfiye zaman asimi, gecis iptal "
                       "(sonraki turda tekrar denenir)")
                continue
            tasfiye.unlink(missing_ok=True)
            d["son_gecis_ts"] = time.time()
            d["son_gecis"] = {"kimden": mevcut, "kime": aday,
                              "skor": skorlar.get(aday)}
            durum_yaz(d)
            _karar_logla({"karar": "swap_tetiklendi", "aday": aday})
            notify(f"[CANLI] OTONOM: duzlesti, kaynak {aday} oluyor "
                   "(servis restart)")
            _swap_tetikle(aday)
            return   # restart geliyor; thread yeni sureçte yeniden dogar
        except Exception:
            log.exception("otonom secici tur hatasi")
