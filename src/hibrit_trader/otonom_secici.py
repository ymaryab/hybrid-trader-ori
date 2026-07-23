"""Otonom kaynak secici (23 Tem 2026, kullanici talebi).

Panel ust menusundeki OTONOM dugmesi acikken calisir: son PENCERE_DK
dakikada en cok KAZANDIRAN paper motoru bulur; canli kaynak farkliysa
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
VARSAYILAN_PENCERE_DK = 120

KONTROL_SN = float(os.getenv("OTONOM_KONTROL_SN", "300"))
MIN_ISLEM = int(os.getenv("OTONOM_MIN_ISLEM", "3"))
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


def pencere_skorlari(pencere_dk: float,
                     kaynaklar: list[str]) -> dict[str, dict]:
    """Motor basina son pencere_dk dakikanin gerceklesen PnL'i.
    Kismi kapanislar trade_id ile gruplanmaz: pencere toplami icin
    satir toplami yeterli (ayni sonuc)."""
    esik = time.time() - pencere_dk * 60
    out: dict[str, dict] = {}
    for m in kaynaklar:
        yol = _data_dir() / f"{m}_trades.jsonl"
        pnl = 0.0
        n = 0
        tids = set()
        try:
            with open(yol) as f:
                for ln in f:
                    if not ln.strip():
                        continue
                    try:
                        t = json.loads(ln)
                    except ValueError:
                        continue
                    if t.get("type") or t.get("exit_reason") == "manuel_kapanis":
                        continue
                    if float(t.get("ts") or 0) < esik:
                        continue
                    pnl += float(t.get("pnl_usd") or 0)
                    tid = t.get("trade_id")
                    if tid not in tids:
                        tids.add(tid)
                        n += 1
        except OSError:
            continue
        out[m] = {"pnl": round(pnl, 2), "islem": n}
    return out


def aday_sec(skorlar: dict[str, dict], mevcut: str,
             min_islem: int = MIN_ISLEM) -> str | None:
    """En yuksek pencere PnL'li motor; pozitif PnL ve asgari islem sarti.
    Mevcut kaynak en iyiyse veya kimse sarti gecemiyorsa None."""
    uygun = {m: s for m, s in skorlar.items()
             if s["islem"] >= min_islem and s["pnl"] > 0}
    if not uygun:
        return None
    aday = max(uygun, key=lambda m: uygun[m]["pnl"])
    if aday == mevcut:
        return None
    # mevcut da uygunsa ve aday ondan iyi degilse gecis yok (esitlikte kal)
    if mevcut in uygun and uygun[aday]["pnl"] <= uygun[mevcut]["pnl"]:
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
                   f"(son {d['pencere_dk']}dk pnl {skorlar[aday]['pnl']}$, "
                   f"{skorlar[aday]['islem']} islem); tasfiye basladi")
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
