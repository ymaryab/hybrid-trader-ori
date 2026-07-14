"""Golge defter (rejim disi): rejim kapisina takilan v7 adaylarinin sanal izi.

Kaynak: v7'nin momentum_rejects.jsonl'e yazdigi rejim_reject satirlari
(diger TUM on filtreleri gecmis adaylar; rejim kapisi safety'den ONCE
kostugu icin safety ve taze fiyat kontrolu burada tamamlanir). Her uygun
aday icin v7 kurallariyla SANAL islem izlenir (tp +2 / stop_gec -2 + 30dk
sabir / fren -10 / 60dk tavan, bilet MTM x %25 esdegeri) ve 60dk sonucu
golge_rejim_disi.jsonl'e yazilir.

COK ESIK: her kayda red anindaki sol_h1 yazilir ve 0.2 / 0.35 / 0.4 esik
kovalarina etiketlenir (orn. sol_h1 0.38: kovalar [0.2, 0.35]); tek
defterden uc "esik X olsaydi" senaryosunun PnL'i ayri raporlanir.
Ozet: python -m hibrit_trader.golge_rejim_disi ozet

KESIN SINIRLAR: gercek islem YOK, broker cagrisi YOK, motor/panel koduna
ve LIVE dosyalarina dokunulmaz. Ayri surec olarak calisir (systemd:
momentum-golge). Fiyatlar DexScreener batched pairs endpointinden 30 sn
kadansla okunur, YALNIZ acik sanal pozisyon varken (kota yuku minimum).
Surec yeniden baslarsa acik sanal pozisyonlar dusulur (hafif izleyici).
"""

from __future__ import annotations

import json
import logging
import sys
import time

import httpx

from hibrit_trader.config import API
from hibrit_trader.live_sim import fetch_pool_price
from hibrit_trader.momentum_session import REJECTS_FILE, _data_dir
from hibrit_trader.paper import _now_iso
from hibrit_trader.safety import check_token
from hibrit_trader.v7_session import (
    CEILING_SEC,
    GRACE_SEC,
    LATE_STOP_PCT,
    TP_PCT,
)

log = logging.getLogger(__name__)

ESIKLER = (0.2, 0.35, 0.4)
OUTPUT_FILE = "golge_rejim_disi.jsonl"
BILET_PCT = 25.0                # MTM x %25 esdegeri (canli bilet modeliyle ayni)
TAIL_POLL_SEC = 5.0             # rejects dosyasi kuyruklamasi
TICK_SEC = 30.0                 # acik sanal pozisyon fiyat kadansi
MAX_ACIK = 10                   # ayni anda izlenen sanal pozisyon tavani
COOLDOWN_SEC = 60 * 60          # ayni token 60dk'da bir kez izlenir
SATIR_MAX_YAS_SEC = 120.0       # bundan eski reject satiri islenmez (restart)
ADAY_MAX_PER_POLL = 3           # poll basina en cok 3 safety+fiyat kontrolu


def esik_kovalari(sol_h1: float | None) -> list[float]:
    """Red anindaki sol_h1'in girebilecegi esik kovalari (kucukten buyuge)."""
    if sol_h1 is None:
        return []
    return [e for e in ESIKLER if sol_h1 >= e]


def bilet_usd_oku() -> float | None:
    """canli_equity.jsonl son satirindan MTM x %25; okunamazsa None (kayit bos kalir)."""
    try:
        satirlar = (_data_dir() / "canli_equity.jsonl").read_text().splitlines()
        for ln in reversed(satirlar):
            if ln.strip():
                eq = float(json.loads(ln)["eq"])
                return round(eq * BILET_PCT / 100.0, 2) if eq > 0 else None
    except Exception:
        log.debug("golge_rejim_disi: bilet okunamadi", exc_info=True)
    return None


def satir_uygun(row: dict, now: float) -> bool:
    """rejects satiri sanal izlemeye aday mi? (saf filtre, IO yok)"""
    if row.get("type") != "reject" or row.get("reason") != "rejim_reject":
        return False
    if row.get("engine") != "V7":
        return False
    if now - float(row.get("ts") or 0.0) > SATIR_MAX_YAS_SEC:
        return False
    return bool(esik_kovalari(row.get("sol_chg_h1")))


class GolgeDefter:
    """Sanal v7 pozisyonlari: acilis, tick degerlendirme, kapanis satiri."""

    def __init__(self) -> None:
        self.acik: list[dict] = []
        self._cooldown: dict[str, float] = {}

    def aday_ekle(self, row: dict, giris_fiyat: float,
                  bilet_usd: float | None, now: float) -> dict | None:
        token = str(row.get("token_address") or "")
        if not token or giris_fiyat <= 0:
            return None
        if len(self.acik) >= MAX_ACIK:
            return None
        if now < self._cooldown.get(token, 0.0):
            return None
        if any(p["token"] == token for p in self.acik):
            return None
        self._cooldown[token] = now + COOLDOWN_SEC
        pos = {
            "pair": row.get("pair"),
            "token": token,
            "pool_address": row.get("pool_address"),
            "chain": row.get("chain") or "solana",
            "sol_h1": row.get("sol_chg_h1"),
            "esik_kovalar": esik_kovalari(row.get("sol_chg_h1")),
            "giris": giris_fiyat,
            "bilet_usd": bilet_usd,
            "opened_ts": now,
            "reject_ts": row.get("ts"),
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "last_price": giris_fiyat,
        }
        self.acik.append(pos)
        return pos

    def tick(self, fiyatlar: dict[str, float], now: float) -> list[dict]:
        """Acik pozisyonlari degerlendir; kapananlarin kayit satirlarini dondur.

        Kural seti v7 _eval_position ile birebir: tp / fren her an, stop_gec
        yalniz 30dk sabir sonrasi, 60dk tavan. Fiyati gelmeyen pozisyon son
        fiyatiyla degerlendirilir (yalniz tavan tetiklenebilir).
        """
        kapanan: list[dict] = []
        for pos in list(self.acik):
            price = fiyatlar.get(pos["pool_address"]) or 0.0
            if price > 0:
                pos["last_price"] = price
            price = pos["last_price"]
            pnl_pct = (price / pos["giris"] - 1) * 100 if pos["giris"] > 0 else 0.0
            pos["mfe_pct"] = round(max(pos["mfe_pct"], pnl_pct), 4)
            pos["mae_pct"] = round(min(pos["mae_pct"], pnl_pct), 4)
            age = now - pos["opened_ts"]
            sonuc = None
            if pnl_pct > TP_PCT:
                sonuc = "tp_2"
            elif age >= GRACE_SEC and pnl_pct <= LATE_STOP_PCT:
                sonuc = "stop_gec"
            elif age >= CEILING_SEC:
                sonuc = "timeout_120"
            if sonuc is None:
                continue
            self.acik.remove(pos)
            kapanan.append({
                "ts": round(now, 3),
                "ts_iso": _now_iso(),
                "tur": "golge_rejim_disi",
                "pair": pos["pair"],
                "token": pos["token"],
                "pool_address": pos["pool_address"],
                "chain": pos["chain"],
                "sol_h1": pos["sol_h1"],
                "esik_kovalar": pos["esik_kovalar"],
                "giris": pos["giris"],
                "cikis": price,
                "bilet_usd": pos["bilet_usd"],
                "sonuc": sonuc,
                "pnl_pct": round(pnl_pct, 3),
                "tavan_pct": pos["mfe_pct"],
                "taban_pct": pos["mae_pct"],
                "hold_sec": round(age, 1),
                "reject_ts": pos["reject_ts"],
            })
        return kapanan


def _kayit_yaz(row: dict) -> None:
    p = _data_dir() / OUTPUT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _fiyatlari_cek(client: httpx.Client, pools_by_chain: dict[str, list[str]]) -> dict[str, float]:
    """Acik pozisyon havuzlari icin TEK batched istek/zincir (30'luk chunk)."""
    out: dict[str, float] = {}
    for chain, pools in pools_by_chain.items():
        for i in range(0, len(pools), 30):
            grup = ",".join(pools[i:i + 30])
            try:
                r = client.get(f"{API['dexscreener']}/latest/dex/pairs/{chain}/{grup}")
                r.raise_for_status()
                data = r.json()
                items = data.get("pairs") or ([data["pair"]] if data.get("pair") else [])
                for item in items:
                    try:
                        addr = str(item.get("pairAddress") or "")
                        price = float(item.get("priceUsd") or 0)
                    except (TypeError, ValueError):
                        continue
                    if addr and price > 0:
                        out[addr] = price
            except Exception:
                log.debug("golge_rejim_disi: fiyat cekilemedi", exc_info=True)
    return out


def _aday_isle(client: httpx.Client, defter: GolgeDefter, row: dict, now: float) -> None:
    """Safety + taze fiyat tamamla, gecerse sanal pozisyon ac. Broker YOK."""
    try:
        rep = check_token(client, row.get("chain") or "solana",
                          str(row.get("token_address") or ""))
        if not rep.ok:
            return
    except Exception:
        return  # fail-closed: safety belirsizse izleme yok (v7 ile ayni ruh)
    try:
        fiyat = fetch_pool_price(client, row.get("chain") or "solana",
                                 str(row.get("pool_address") or ""))
    except Exception:
        fiyat = None
    if not fiyat or fiyat <= 0:
        return
    pos = defter.aday_ekle(row, float(fiyat), bilet_usd_oku(), now)
    if pos is not None:
        log.warning("GOLGE-REJIM aday: %s sol_h1 %s kovalar %s giris %.8g",
                    pos["pair"], pos["sol_h1"], pos["esik_kovalar"], pos["giris"])


def izleyici(defter: GolgeDefter | None = None) -> None:
    """Ana dongu: rejects dosyasini kuyrukla, sanal pozisyonlari 30s'de degerlendir."""
    defter = defter or GolgeDefter()
    rejects_p = _data_dir() / REJECTS_FILE
    offset = rejects_p.stat().st_size if rejects_p.exists() else 0
    son_tick = 0.0
    log.warning("GOLGE-REJIM izleyici basladi (esikler %s, kadans %ds, tavan %d poz)",
                ESIKLER, int(TICK_SEC), MAX_ACIK)
    with httpx.Client(timeout=10.0) as client:
        while True:
            now = time.time()
            try:
                if rejects_p.exists():
                    boyut = rejects_p.stat().st_size
                    if boyut < offset:
                        offset = 0  # dosya dondu/truncate: bastan
                    if boyut > offset:
                        with rejects_p.open("r", encoding="utf-8") as f:
                            f.seek(offset)
                            yeni = f.read()
                            offset = f.tell()
                        adaylar = []
                        for ln in yeni.splitlines():
                            if not ln.strip():
                                continue
                            try:
                                r = json.loads(ln)
                            except ValueError:
                                continue
                            if satir_uygun(r, now):
                                adaylar.append(r)
                        for r in adaylar[:ADAY_MAX_PER_POLL]:
                            _aday_isle(client, defter, r, now)
                            time.sleep(1.5)  # v7 safety kadansiyla ayni nezaket
                if defter.acik and now - son_tick >= TICK_SEC:
                    son_tick = now
                    pools: dict[str, list[str]] = {}
                    for p in defter.acik:
                        pools.setdefault(p["chain"], []).append(p["pool_address"])
                    fiyatlar = _fiyatlari_cek(client, pools)
                    for row in defter.tick(fiyatlar, time.time()):
                        _kayit_yaz(row)
                        log.warning("GOLGE-REJIM kapanis: %s %s pnl %.2f%% kovalar %s",
                                    row["pair"], row["sonuc"], row["pnl_pct"],
                                    row["esik_kovalar"])
            except Exception:
                log.warning("GOLGE-REJIM dongu hatasi", exc_info=True)
            time.sleep(TAIL_POLL_SEC)


def ozet(path=None) -> str:
    """Esik bazli PnL raporu: her esik kendi kovasindaki kayitlardan hesaplanir."""
    p = path or (_data_dir() / OUTPUT_FILE)
    rows: list[dict] = []
    try:
        for ln in p.read_text().splitlines():
            if ln.strip():
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    continue
    except FileNotFoundError:
        return "golge_rejim_disi: kayit yok"
    if not rows:
        return "golge_rejim_disi: kayit yok"
    cikti = [f"GOLGE REJIM DISI OZET: {len(rows)} kayit"]
    for esik in ESIKLER:
        grup = [r for r in rows if esik in (r.get("esik_kovalar") or [])]
        if not grup:
            cikti.append(f"esik {esik}: kayit yok")
            continue
        pnls = [float(r.get("pnl_pct") or 0.0) for r in grup]
        usd = sum(
            float(r["pnl_pct"]) / 100.0 * float(r["bilet_usd"])
            for r in grup if r.get("bilet_usd")
        )
        wins = sum(1 for x in pnls if x > 0)
        sonuclar: dict[str, int] = {}
        for r in grup:
            s = r.get("sonuc", "?")
            sonuclar[s] = sonuclar.get(s, 0) + 1
        cikti.append(
            f"esik {esik}: n {len(grup)} | win {wins}/{len(grup)}"
            f" | ort pnl {sum(pnls) / len(pnls):+.2f}%"
            f" | toplam pnl {sum(pnls):+.2f}% (~${usd:+.2f} biletle)"
            f" | {sonuclar}"
        )
    return "\n".join(cikti)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "ozet":
        print(ozet())
    else:
        izleyici()
