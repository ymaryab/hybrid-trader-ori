"""Cuzdan-motor senkron bekcisi (18 Tem).

Aktif canli motorun state.positions'undaki canli_miktar>0 pozisyonlari,
Solana RPC'deki gercek token bakiyeleriyle karsilastirir. Fark varsa
telegram'a UYARI (5dk dedup).

Amac: motor "hayali acik poz" tutar hale gelirse (V7HIZLI 16 Tem olayi
gibi) kullaniciya erken haber ver.

SENKRON_ENABLED=0 ile kapatilir.
Env: SENKRON_PERIOD_SEC (default 60), SENKRON_DEDUP_SEC (default 300),
     SENKRON_EKSIK_ORAN (default 0.5 - beklenen*0.5'dan az ise UYARI),
     SENKRON_TAZE_POZ_SEC (default 120 - daha taze pozisyonlar atlanir).

Yanlis alarm korumasi (20 Tem): taze alimda RPC token hesabini henuz
indekslememis olabilir; 120s'den taze pozisyon kontrol edilmez ve alarm
ancak ART ARDA IKI turda ayni uyumsuzluk gorulurse calar.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

CUZDAN = os.getenv("SENKRON_CUZDAN",
                   "DZXZGD5FURZDwa5BWByxxd7iLdCvGxSCy6RWHsgupaYa")
PERIOD_SEC = float(os.getenv("SENKRON_PERIOD_SEC", "60"))
DEDUP_SEC = float(os.getenv("SENKRON_DEDUP_SEC", "300"))
EKSIK_ORAN = float(os.getenv("SENKRON_EKSIK_ORAN", "0.5"))
TAZE_POZ_SEC = float(os.getenv("SENKRON_TAZE_POZ_SEC", "120"))
DATA = Path(os.getenv("MOMENTUM_DATA_DIR", "data"))
TOKEN_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

_son_uyari: dict[str, float] = {}
_supheli: dict[str, float] = {}  # key -> ilk uyumsuzluk ts (iki-tur teyidi)


def _rpc(method: str, params: list, timeout: float = 15.0):
    # 18 Tem: multi-RPC fallback (primary fail -> sirada URL)
    from hibrit_trader.rpc_multi import rpc_post
    return rpc_post(method, params, timeout=timeout)


def _cuzdan_token_bakiye(mint: str) -> float | None:
    """Belirli bir mint icin cuzdan toplam bakiye. Mint filter -> Token-2022 dahil.
    Hata halinde None (senkron kararsiz - uyari basmaz)."""
    try:
        r = _rpc("getTokenAccountsByOwner",
                 [CUZDAN, {"mint": mint}, {"encoding": "jsonParsed"}])
        accts = r.get("result", {}).get("value", []) or []
        toplam = 0.0
        for a in accts:
            info = a["account"]["data"]["parsed"]["info"]
            toplam += float(info["tokenAmount"].get("uiAmount") or 0)
        return toplam
    except Exception as e:
        log.warning("SENKRON: %s icin cuzdan okunamadi: %r", mint[:8], e)
        return None


def _uyar(mesaj: str, kanal_key: str) -> None:
    """Log CRITICAL + telegram notify (dedup)."""
    now = time.time()
    if now - _son_uyari.get(kanal_key, 0.0) < DEDUP_SEC:
        return
    _son_uyari[kanal_key] = now
    log.critical("SENKRON UYARI: %s", mesaj)
    try:
        from hibrit_trader.killswitch import notify
        notify(f"⚠️ SENKRON UYARI: {mesaj}")
    except Exception:
        log.warning("SENKRON telegram gonderilemedi", exc_info=True)


KASA_ESIK_USD = float(os.getenv("SENKRON_KASA_ESIK", "3.0"))
KASA_PERIOD_SEC = float(os.getenv("SENKRON_KASA_PERIOD", "600"))
_kasa_son_ts = 0.0
_kasa_son_fark: float | None = None


def _sol_fiyat() -> float | None:
    try:
        r = urllib.request.urlopen(
            "https://api.dexscreener.com/latest/dex/tokens/"
            "So11111111111111111111111111111111111111112", timeout=8)
        y = json.loads(r.read())
        prs = [p for p in (y.get("pairs") or []) if p.get("priceUsd")]
        if prs:
            return float(max(prs, key=lambda x: float(
                (x.get("liquidity") or {}).get("usd") or 0))["priceUsd"])
    except Exception:
        pass
    return None


def kasa_mutabakat(state: dict) -> None:
    """24 Tem (onay): defter nakiti vs zincir SOL. Fark (ucretler + toz)
    data/canli_kasa_mutabakat.jsonl'e yazilir; sicramada [CANLI] uyarisi
    (telegram filtresinden gecer)."""
    global _kasa_son_ts, _kasa_son_fark
    if time.time() - _kasa_son_ts < KASA_PERIOD_SEC:
        return
    _kasa_son_ts = time.time()
    try:
        sol = _rpc("getBalance", [CUZDAN]).get("result", {}).get("value")
        if sol is None:
            return
        sol = float(sol) / 1e9
    except Exception:
        return
    fiyat = _sol_fiyat()
    if not fiyat:
        return
    defter = float(state.get("balance") or 0.0)
    zincir_usd = sol * fiyat
    fark = defter - zincir_usd
    kayit = {"ts": time.time(), "defter_usd": round(defter, 2),
             "zincir_sol": round(sol, 6), "sol_fiyat": round(fiyat, 2),
             "zincir_usd": round(zincir_usd, 2), "fark_usd": round(fark, 2)}
    try:
        with open(DATA / "canli_kasa_mutabakat.jsonl", "a") as f:
            f.write(json.dumps(kayit) + "\n")
    except OSError:
        pass
    if _kasa_son_fark is not None and fark - _kasa_son_fark >= KASA_ESIK_USD:
        try:
            from hibrit_trader.killswitch import notify
            notify(f"[CANLI] KASA MUTABAKAT: defter-cuzdan nakit farki "
                   f"{_kasa_son_fark:.2f}$ -> {fark:.2f}$ buyudu "
                   f"(ucret/toz kacagi olabilir)")
        except Exception:
            pass
    _kasa_son_fark = fark


# ---- Derin mutabakat (25 Tem, CASHCOW yonu): WAL<->defter + yetim tarama
DERIN_PERIYOT_SEC = float(os.getenv("SENKRON_DERIN_SN", "600"))
WAL_OLGUNLUK_SEC = float(os.getenv("SENKRON_WAL_OLGUNLUK", "180"))
WSOL = "So11111111111111111111111111111111111111112"
TOKEN22_PROG = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
_derin_son = 0.0
_yetim_supheli: dict[str, float] = {}


def _gorulen_yukle(ad: str) -> dict:
    try:
        return json.loads((DATA / ad).read_text())
    except (OSError, ValueError):
        return {}


def _gorulen_kaydet(ad: str, d: dict) -> None:
    try:
        tmp = DATA / (ad + ".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, DATA / ad)
    except OSError:
        log.warning("SENKRON: %s kaydedilemedi", ad)


GIRIS_ES_TOLERANS_SEC = 600.0


def _defter_izleri(canli_motor: str,
                   state: dict) -> tuple[set, set, list]:
    """(tx kumesi, mint kumesi, giris listesi[(token, giris_ts)]).

    Trade satirlari tx tasimaz (dogrulandi 25 Tem); WAL eslesmesi bu
    yuzden tx VEYA token+giris-zamani yakinligi ile yapilir. Yetim
    tanimi: cuzdanda var ama bu izlerin HICBIRINDE yok (toz dogal
    olarak dislanir: bir kez ticareti yapilan mint iz birakir)."""
    txler, mintler, girisler = set(), set(), []
    for p in state.get("positions", []) or []:
        if p.get("tx_al"):
            txler.add(p["tx_al"])
        tok = p.get("token_address")
        if tok:
            mintler.add(tok)
            girisler.append((tok, float(p.get("opened_ts") or 0)))
    # 25 Tem ilk tur dersi: yalniz aktif canli defteri taramak eski
    # canli donemlerin (v7/r1) tozunu sahte-CASHCOW yapiyordu. Mint izi
    # TUM defterlerden toplanir (cuzdan tek); zaman-esi girisler ise
    # yalniz aktif canli defterden (WAL kiyasinin dogrulugu icin).
    import glob as _glob
    for yolad in _glob.glob(str(DATA / "*_trades.jsonl")):
        aktif = yolad.endswith(f"{canli_motor}_trades.jsonl")
        try:
            for ln in open(yolad):
                if not ln.strip():
                    continue
                try:
                    t = json.loads(ln)
                except ValueError:
                    continue
                if t.get("type"):
                    continue
                if t.get("tx_al"):
                    txler.add(t["tx_al"])
                tok = t.get("token_address")
                if tok:
                    mintler.add(tok)
                    if aktif:
                        ts = float(t.get("ts") or 0)
                        girisler.append(
                            (tok, ts - float(t.get("hold_sec") or 0)))
        except OSError:
            continue
    return txler, mintler, girisler


def wal_defter_mutabakat(canli_motor: str, state: dict) -> None:
    """WAL'daki her 'al' dolumu WAL_OLGUNLUK_SEC sonra defterde iz
    birakmis olmali; birakmadiysa CASHCOW sinifi persist kaybi demektir."""
    txler, _, girisler = _defter_izleri(canli_motor, state)
    gorulen = _gorulen_yukle("senkron_wal_gorulen.json")
    simdi = time.time()
    degisti = False
    try:
        fh = open(DATA / "canli_fills.jsonl")
    except OSError:
        return
    with fh:
        for ln in fh:
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            if t.get("yon") not in ("al", "alis"):
                continue
            tx = t.get("tx") or t.get("tx_al")
            ts = float(t.get("ts") or 0)
            if not tx or tx in gorulen or simdi - ts < WAL_OLGUNLUK_SEC:
                continue
            tok = t.get("token_address")
            zaman_esi = any(
                g_tok == tok and abs(g_ts - ts) <= GIRIS_ES_TOLERANS_SEC
                for g_tok, g_ts in girisler)
            if tx not in txler and not zaman_esi:
                from hibrit_trader.uyari_notify import kritik_uyari
                kritik_uyari("[CANLI] WAL MUTABAKAT", f"wal:{tx[:12]}",
                             f"zincirde dolum var, defterde IZ YOK "
                             f"(CASHCOW sinifi): {t.get('pair') or ''} "
                             f"mint {(t.get('token_address') or '?')[:8]} "
                             f"tx {tx[:12]}")
            gorulen[tx] = simdi          # cozulen de tekrar taranmaz
            degisti = True
    if degisti:
        _gorulen_kaydet("senkron_wal_gorulen.json", gorulen)


def _cuzdan_tum_mintler() -> dict[str, float] | None:
    mintler: dict[str, float] = {}
    for prog in (TOKEN_PROG, TOKEN22_PROG):
        try:
            r = _rpc("getTokenAccountsByOwner",
                     [CUZDAN, {"programId": prog},
                      {"encoding": "jsonParsed"}])
        except Exception as e:  # noqa: BLE001
            log.warning("SENKRON yetim tarama RPC hatasi: %r", e)
            return None
        for a in (r.get("result", {}).get("value", []) or []):
            try:
                info = a["account"]["data"]["parsed"]["info"]
                ui = float(info["tokenAmount"].get("uiAmount") or 0)
                if ui > 0:
                    mintler[info["mint"]] = ui
            except (KeyError, TypeError, ValueError):
                continue
    return mintler


def yetim_token_tarama(canli_motor: str, state: dict) -> None:
    """Cuzdanda olup defterde HIC iz birakmamis mint = yetim (17 Tem
    CASHCOW vakasi: basarili alim + persist kaybi). Iki-tur teyit +
    kalici gorulen kaydi (restart sonrasi tekrar alarm yok)."""
    mintler = _cuzdan_tum_mintler()
    if mintler is None:
        return
    _, defter_mintler, _ = _defter_izleri(canli_motor, state)
    gorulen = _gorulen_yukle("senkron_yetim_gorulen.json")
    simdi = time.time()
    degisti = False
    for mint, ui in mintler.items():
        if mint == WSOL or mint in defter_mintler or mint in gorulen:
            continue
        if mint not in _yetim_supheli:
            _yetim_supheli[mint] = simdi      # ilk tur: teyit bekle
            continue
        from hibrit_trader.uyari_notify import kritik_uyari
        kritik_uyari("[CANLI] YETIM TOKEN", f"yetim:{mint[:12]}",
                     f"cuzdanda {ui:.4g} adet var, defterde HIC iz yok "
                     f"(CASHCOW sinifi, WAL/persist kaybi?): mint {mint}")
        gorulen[mint] = simdi
        _yetim_supheli.pop(mint, None)
        degisti = True
    for mint in list(_yetim_supheli):
        if mint not in mintler or mint in gorulen:
            _yetim_supheli.pop(mint, None)
    if degisti:
        _gorulen_kaydet("senkron_yetim_gorulen.json", gorulen)


def derin_mutabakat(canli_motor: str, state: dict) -> None:
    global _derin_son
    if time.time() - _derin_son < DERIN_PERIYOT_SEC:
        return
    _derin_son = time.time()
    wal_defter_mutabakat(canli_motor, state)
    yetim_token_tarama(canli_motor, state)


def check_once() -> None:
    # 24 Tem: 10. motor mimarisi: canli pozisyonlar canli_state.json'da.
    # (Eski CANLI_MOTOR env'i v7 varsayip YANLIS dosyayi izliyordu.)
    canli_motor = os.getenv("CANLI_MOTOR", "canli").strip().lower()
    sp = DATA / f"{canli_motor}_state.json"
    if not sp.exists():
        return
    try:
        s = json.loads(sp.read_text())
    except Exception:
        return

    # state'teki canli_miktar>0 pozisyonlari icin her mint icin ayrı sorgu
    # (mint filtresi Token-2022 dahil TUM hesaplari getirir)
    su_tur_supheli: set[str] = set()
    for p in s.get("positions", []) or []:
        cm = float(p.get("canli_miktar") or 0)
        if cm <= 0:
            continue
        mint = p.get("token_address")
        pair = p.get("pair", "?")
        if not mint:
            continue
        opened_ts = float(p.get("opened_ts") or 0)
        if opened_ts and time.time() - opened_ts < TAZE_POZ_SEC:
            continue  # taze alim: RPC indeks gecikmesi, bu tur atla
        gercek = _cuzdan_token_bakiye(mint)
        if gercek is None:
            continue  # RPC hata, atla
        if gercek < cm * EKSIK_ORAN:
            key = f"{canli_motor}:{mint}:eksik"
            su_tur_supheli.add(key)
            if key not in _supheli:
                # ilk tur: alarm yok, sonraki turda teyit beklenir
                _supheli[key] = time.time()
                log.warning("SENKRON suphe (teyit bekleniyor): %s %s "
                            "state=%.2f cuzdan=%.2f", canli_motor.upper(),
                            pair, cm, gercek)
                continue
            _uyar(f"{canli_motor.upper()} {pair}: state={cm:.2f} "
                  f"cuzdan={gercek:.2f} (hayali poz - motor takip ediyor "
                  f"ama cuzdanda YOK)", key)
    # uyumsuzlugu gecen (duzelen/kapanan) pozisyonlarin suphe kaydini sil
    for key in list(_supheli):
        if key not in su_tur_supheli:
            _supheli.pop(key, None)
    kasa_mutabakat(s)
    derin_mutabakat(canli_motor, s)


def run_forever() -> None:
    log.warning("SENKRON BEKCISI basladi: cuzdan %s..%s, period=%.0fs, "
                "dedup=%.0fs, eksik_oran=%.0f%%",
                CUZDAN[:6], CUZDAN[-4:], PERIOD_SEC, DEDUP_SEC, EKSIK_ORAN * 100)
    # Ilk kontrol icin kucuk gecikme (motor start_up bekle)
    time.sleep(20.0)
    while True:
        try:
            check_once()
        except Exception:
            log.exception("SENKRON check exception")
        time.sleep(PERIOD_SEC)
