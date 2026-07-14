"""Broker fabrikası — paper veya live. Altta: uc modlu yurutme katmani."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from hibrit_trader.config import DEFAULT_RPC, Settings
from hibrit_trader.paper import PaperBroker


def make_broker(settings: Settings):
    if settings.mode == "live":
        if settings.sol_server_signing_enabled() or (
            settings.evm_private_key and settings.zero_x_api_key
        ):
            from hibrit_trader.live import LiveBroker
            return LiveBroker(settings)
        from hibrit_trader.phantom_broker import PhantomLiveBroker
        return PhantomLiveBroker(settings)
    return PaperBroker(start_balance_usd=settings.paper_start_balance_usd)


# =====================================================================================
# YURUTME KATMANI (broker projesi): paper / dryrun / live tek arayuz arkasinda.
# Motorlarin trading kurallarina ve yarisa SIFIR dokunus; bagimsiz altyapi katmani.
#
# LIVE CIFT KILIT (degistirilemez kural):
#   1) ortam degiskeni LIVE_UNLOCKED=1
#   2) data dizininde LIVE_ONAY dosyasi, icerigi tam olarak "canli-islem-onayliyorum"
# Ikisi birden saglanmadan live broker kurulamaz; execute her cagrida yeniden kontrol
# eder. Dryrun hicbir kosulda imza atmaz, cuzdan hic yuklenmez.
# =====================================================================================

log = logging.getLogger(__name__)

_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_LIVE_ONAY_DOSYA = "LIVE_ONAY"
_LIVE_ONAY_ICERIK = "canli-islem-onayliyorum"
_FILLS_DOSYA = "dryrun_fills.jsonl"

_fills_lock = threading.Lock()


def _exec_data_dir() -> Path:
    return Path(os.getenv("MOMENTUM_DATA_DIR", "data"))


def _rpc_url() -> str:
    return os.getenv("SOLANA_RPC_URL") or DEFAULT_RPC["solana"]


def _zincir_dolum(http: httpx.Client, sig: str, owner: str, mint: str,
                  deneme: int = 3, bekleme_sn: float = 1.5) -> float | None:
    """Onayli alim tx'inin pre/postTokenBalances farkindan owner+mint icin
    GERCEK dolumu dondurur (ui miktar). Cuzdan bakiyesi kullanilmaz cunku
    onceden kalan kirinti dolumu sisirir (14 Tem olayi). Okunamazsa None."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
               "params": [sig, {"encoding": "jsonParsed",
                                "commitment": "confirmed",
                                "maxSupportedTransactionVersion": 0}]}
    for i in range(deneme):
        try:
            r = http.post(_rpc_url(), json=payload, timeout=15).json()
            tx = r.get("result")
            if tx:
                meta = tx["meta"]

                def _tok(bals):
                    return sum((b["uiTokenAmount"]["uiAmount"] or 0.0)
                               for b in bals or []
                               if b.get("owner") == owner and b.get("mint") == mint)

                return _tok(meta.get("postTokenBalances")) - _tok(meta.get("preTokenBalances"))
        except Exception as e:
            log.warning("zincir dolum okuma hatasi (%d/%d): %s", i + 1, deneme, e)
        if i + 1 < deneme:
            time.sleep(bekleme_sn)
    return None


def _zincir_token_bakiye(http: httpx.Client, owner: str, mint: str) -> float | None:
    """Cuzdanin mint icin toplam zincir bakiyesi (ui miktar). Okunamazsa None."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
               "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}]}
    try:
        r = http.post(_rpc_url(), json=payload, timeout=10).json()
        return sum((acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0.0)
                   for acc in r["result"]["value"])
    except Exception as e:
        log.warning("zincir bakiye okuma hatasi: %s", e)
        return None


def _zincir_imza_durumu(http: httpx.Client, sig: str) -> str | None:
    """getSignatureStatuses: 'onaylandi' | 'hatali' | 'yok' | None (sorgu hatasi).
    searchTransactionHistory=True: 'yok' cevabi ledger aramasini da kapsar."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
               "params": [[sig], {"searchTransactionHistory": True}]}
    try:
        r = http.post(_rpc_url(), json=payload, timeout=10).json()
        durum = r["result"]["value"][0]
    except Exception as e:
        log.warning("zincir imza durumu okunamadi: %s", e)
        return None
    if durum is None:
        return "yok"
    return "hatali" if durum.get("err") is not None else "onaylandi"


def _belirsiz_poll_sec() -> float:
    try:
        return float(os.getenv("BELIRSIZ_POLL_SEC", "30") or 30.0)
    except ValueError:
        return 30.0


def _belirsiz_yok_sec() -> float:
    """Bu sureden (ve >=3 basarili 'yok' sorgusundan) sonra tx 'zincirde yok'
    sayilir: blockhash omru coktan dolmustur, para cikmamistir."""
    try:
        return float(os.getenv("BELIRSIZ_YOK_SEC", "150") or 150.0)
    except ValueError:
        return 150.0


def _belirsiz_cap_sec() -> float:
    try:
        return float(os.getenv("BELIRSIZ_CAP_SEC", "600") or 600.0)
    except ValueError:
        return 600.0


def _satis_miktari(kayit: float, zincir: float | None) -> float | None:
    """Satilacak miktar karari. None donerse satis reddedilmeli (zincirde
    bakiye yok, manuel islem suphesi). RPC okunamadiysa kayit kullanilir
    (satis hatti asla korlesmesin)."""
    if zincir is None:
        return kayit
    if zincir <= 0:
        return None
    return min(kayit, zincir)


# ---- Canli satis stresi: Jupiter kotasi satis hattina oncelenir ------------------------

SATIS_STRES_SEC = 60.0
# Alim sonrasi RPC gecikme toleransi: bu yastan taze pozisyonda zincir
# bakiyesi kayittan kucuk gorunuyorsa geride kalan dugum varsayilir,
# 0-red ve kirpma uygulanmaz (kayit zaten alim tx'inin zincir dolumu).
TAZE_POZISYON_SEC = 60.0
_satis_stres_ts = 0.0


def satis_stresi_bildir() -> None:
    """Canli satis basarisiz: golge/hakem Jupiter quotelari bir sure susturulur
    ki kota satis hattina kalsin (14 Tem 429 firtinasi dersi)."""
    global _satis_stres_ts
    _satis_stres_ts = time.time() + SATIS_STRES_SEC


def satis_stresi_temizle() -> None:
    global _satis_stres_ts
    _satis_stres_ts = 0.0


def satis_stresi_aktif() -> bool:
    return time.time() < _satis_stres_ts


def _fills_yaz(row: dict) -> None:
    try:
        d = _exec_data_dir()
        d.mkdir(parents=True, exist_ok=True)
        with _fills_lock, open(d / _FILLS_DOSYA, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("BROKER: dryrun_fills yazilamadi: %s", e)


# ---- Live cift kilit ----------------------------------------------------------------

def live_kilit_acik() -> bool:
    """Cift kilit: env LIVE_UNLOCKED=1 VE LIVE_ONAY dosyasi dogru icerikle var."""
    if os.getenv("LIVE_UNLOCKED", "").strip() != "1":
        return False
    onay = _exec_data_dir() / _LIVE_ONAY_DOSYA
    try:
        return onay.read_text(encoding="utf-8").strip() == _LIVE_ONAY_ICERIK
    except OSError:
        return False


# ---- Cuzdan iskeleti ------------------------------------------------------------------

def _cuzdan_yukle(mode: str):
    """Keypair'i SOL_KEYPAIR_PATH'ten okur. SADECE live modda calisir; dryrun/paper
    modda cagirilirsa hata firlatir (cuzdan hic yuklenmez). Anahtar iceriği asla
    loglanmaz. Repo icindeki yollar reddedilir."""
    if mode != "live":
        raise RuntimeError("cuzdan sadece live modda yuklenir (mode=%s)" % mode)
    yol = os.getenv("SOL_KEYPAIR_PATH", "").strip()
    if not yol:
        raise RuntimeError("cuzdan_yok: SOL_KEYPAIR_PATH tanimsiz")
    p = Path(yol).expanduser().resolve()
    repo = Path(__file__).resolve().parents[2]
    if p.is_relative_to(repo):
        raise RuntimeError("cuzdan_yok: keypair dosyasi repo icinde olamaz")
    if not p.is_file():
        raise RuntimeError("cuzdan_yok: keypair dosyasi bulunamadi")
    from hibrit_trader.jupiter import load_keypair  # tembel: solders yalniz burada
    try:
        return load_keypair(p.read_text(encoding="utf-8"))
    except Exception as e:
        # anahtar icerigi asla loglanmaz; sadece hata sinifi disari cikar
        raise RuntimeError(
            f"cuzdan_yok: keypair cozumlenemedi ({type(e).__name__})") from e


# ---- Veri siniflari -------------------------------------------------------------------

@dataclass
class ExecOrder:
    engine: str
    yon: str                    # "al" | "sat"
    token_address: str
    usd: float = 0.0            # al: harcanacak USD
    amount_token: float = 0.0   # sat: satilacak token miktari
    ref_fiyat: float = 0.0      # paper referans fiyati (kiyas icin)
    slippage_bps: int = 50
    acilis_ts: float | None = None  # sat: pozisyon acilis zamani (taze koruma)


@dataclass
class ExecQuote:
    fiyat: float
    miktar_token: float
    route: list = field(default_factory=list)
    gecikme_ms: float = 0.0
    ham: dict = field(default_factory=dict)


@dataclass
class ExecFill:
    ok: bool
    fiyat: float = 0.0
    miktar_token: float = 0.0
    fee_usd: float = 0.0
    tx_id: str | None = None
    gecikme_ms: float = 0.0
    neden: str | None = None
    route: list = field(default_factory=list)


# ---- Paper: mevcut davranis, referans fiyat aynen fill olur ---------------------------

class PaperExecBroker:
    mode = "paper"

    def get_quote(self, token_address: str, yon: str, miktar: float) -> ExecQuote | None:
        return None  # paper modda dis dunyadan quote alinmaz

    def execute(self, order: ExecOrder) -> ExecFill:
        miktar_token = order.amount_token
        if order.yon == "al" and order.ref_fiyat > 0:
            miktar_token = order.usd / order.ref_fiyat
        return ExecFill(ok=True, fiyat=order.ref_fiyat, miktar_token=miktar_token,
                        fee_usd=0.0, tx_id=None, gecikme_ms=0.0)


# ---- Dryrun: Jupiter quote + tx kurma + simulasyon; IMZA ASLA yok ---------------------

class DryrunExecBroker:
    mode = "dryrun"

    def __init__(self, http: httpx.Client | None = None):
        self._http = http or httpx.Client(timeout=20)
        self._dec_cache: dict[str, int] = {}

    # -- yardimcilar --
    def _decimals(self, mint: str) -> int | None:
        if mint in self._dec_cache:
            return self._dec_cache[mint]
        try:
            resp = self._http.post(_rpc_url(), json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenSupply", "params": [mint],
            }, timeout=15)
            resp.raise_for_status()
            dec = int(resp.json()["result"]["value"]["decimals"])
            self._dec_cache[mint] = dec
            return dec
        except Exception as e:
            log.warning("BROKER: decimals alinamadi %s: %s", mint[:8], e)
            return None

    def _quote(self, token_address: str, yon: str, miktar: float,
               slippage_bps: int) -> tuple[ExecQuote | None, str | None]:
        if miktar is None or miktar <= 0:
            return None, "quote_yok"
        dec = self._decimals(token_address)
        if dec is None:
            return None, "decimals_yok"
        if yon == "al":
            amount_raw = max(1, int(miktar * 1_000_000))
            inp, out = _USDC_MINT, token_address
        else:
            amount_raw = max(1, int(miktar * 10 ** dec))
            inp, out = token_address, _USDC_MINT
        t0 = time.monotonic()
        try:
            from hibrit_trader.jupiter import get_quote as jup_quote
            ham = jup_quote(self._http, inp, out, amount_raw, slippage_bps)
        except httpx.HTTPStatusError as e:
            metin = ""
            try:
                metin = e.response.text
            except Exception:
                pass
            if e.response is not None and e.response.status_code == 400 and (
                    "COULD_NOT_FIND_ANY_ROUTE" in metin):
                return None, "route_yok"
            return None, "quote_yok"
        except Exception:
            return None, "quote_yok"
        gecikme_ms = round((time.monotonic() - t0) * 1000, 1)
        try:
            out_raw = int(ham["outAmount"])
            if out_raw <= 0:
                return None, "quote_yok"
            if yon == "al":
                miktar_token = out_raw / 10 ** dec
                fiyat = miktar / miktar_token
            else:
                miktar_token = miktar
                fiyat = (out_raw / 1_000_000) / miktar
            route = [p.get("swapInfo", {}).get("label")
                     for p in ham.get("routePlan", [])]
        except Exception:
            return None, "quote_yok"
        return ExecQuote(fiyat=fiyat, miktar_token=miktar_token, route=route,
                         gecikme_ms=gecikme_ms, ham=ham), None

    def _simulate(self, ham_quote: dict, pubkey: str) -> tuple[bool, str | None]:
        """Swap tx'i kurar ve simulateTransaction ile dogrular. Imza YOK."""
        try:
            from hibrit_trader.jupiter import build_swap_tx
            tx_b64 = build_swap_tx(self._http, ham_quote, pubkey)
            resp = self._http.post(_rpc_url(), json={
                "jsonrpc": "2.0", "id": 1, "method": "simulateTransaction",
                "params": [tx_b64, {"encoding": "base64", "sigVerify": False,
                                    "replaceRecentBlockhash": True}],
            }, timeout=20)
            resp.raise_for_status()
            err = resp.json()["result"]["value"].get("err")
            if err is not None:
                return False, f"sim_fail: {err}"
            return True, None
        except Exception as e:
            return False, f"sim_fail: {e}"

    # -- arayuz --
    def get_quote(self, token_address: str, yon: str, miktar: float) -> ExecQuote | None:
        q, neden = self._quote(token_address, yon, miktar, slippage_bps=50)
        if q is None:
            log.warning("BROKER dryrun quote basarisiz %s %s: %s",
                        yon, token_address[:8], neden)
        return q

    def execute(self, order: ExecOrder) -> ExecFill:
        t0 = time.monotonic()
        miktar = order.usd if order.yon == "al" else order.amount_token
        q, neden = self._quote(order.token_address, order.yon, miktar,
                               order.slippage_bps)
        row = {
            "ts": round(time.time(), 3), "tur": "dryrun", "engine": order.engine,
            "yon": order.yon, "token": order.token_address,
            "paper_fiyat": order.ref_fiyat,
        }
        if q is None:
            row.update({"neden": neden,
                        "gecikme_ms": round((time.monotonic() - t0) * 1000, 1)})
            _fills_yaz(row)
            log.warning("BROKER dryrun %s %s fill YOK: %s",
                        order.engine, order.yon, neden)
            return ExecFill(ok=False, neden=neden, gecikme_ms=row["gecikme_ms"])
        row.update({
            "jup_fiyat": q.fiyat, "route": q.route,
            "fark_bps": round((q.fiyat / order.ref_fiyat - 1) * 10_000, 2)
            if order.ref_fiyat > 0 else None,
        })
        try:
            impact_bps = abs(float(q.ham.get("priceImpactPct") or 0.0)) * 10_000
        except (TypeError, ValueError):
            impact_bps = 0.0
        if impact_bps > order.slippage_bps:
            row.update({"neden": "slippage_asimi", "impact_bps": round(impact_bps, 1),
                        "gecikme_ms": round((time.monotonic() - t0) * 1000, 1)})
            _fills_yaz(row)
            log.warning("BROKER dryrun %s %s slippage asimi: %.1f bps > %d bps",
                        order.engine, order.yon, impact_bps, order.slippage_bps)
            return ExecFill(ok=False, neden="slippage_asimi",
                            gecikme_ms=row["gecikme_ms"], route=q.route)
        sim_durum = None
        pubkey = os.getenv("DRYRUN_PUBKEY", "").strip()
        if pubkey:
            sim_ok, sim_neden = self._simulate(q.ham, pubkey)
            sim_durum = "ok" if sim_ok else "fail"
            if not sim_ok:
                row.update({"neden": sim_neden, "sim": sim_durum,
                            "gecikme_ms": round((time.monotonic() - t0) * 1000, 1)})
                _fills_yaz(row)
                log.warning("BROKER dryrun %s %s %s", order.engine, order.yon, sim_neden)
                return ExecFill(ok=False, neden="sim_fail",
                                gecikme_ms=row["gecikme_ms"], route=q.route)
        gecikme_ms = round((time.monotonic() - t0) * 1000, 1)
        row.update({"sim": sim_durum, "gecikme_ms": gecikme_ms})
        _fills_yaz(row)
        log.info("BROKER dryrun %s %s atsaydim: fiyat %.8g, miktar %.6g, "
                 "fark %s bps, route %s (%.0f ms)",
                 order.engine, order.yon, q.fiyat, q.miktar_token,
                 row.get("fark_bps"), "+".join(str(r) for r in q.route), gecikme_ms)
        return ExecFill(ok=True, fiyat=q.fiyat, miktar_token=q.miktar_token,
                        fee_usd=0.0, tx_id=None, gecikme_ms=gecikme_ms, route=q.route)


# ---- Live: cift kilit arkasinda gercek islem ------------------------------------------

def _live_ticket_pct() -> float:
    """LIVE_TICKET_PCT: canli alim bileti, cuzdan MTM'sinin yuzdesi (varsayilan 25).
    Bozuk deger emniyetli varsayilana duser (25)."""
    try:
        return float(os.getenv("LIVE_TICKET_PCT", "25") or 25.0)
    except ValueError:
        return 25.0


def _max_giris_prim_pct() -> float:
    """Giris prim kapisi esigi (%). <=0 kapiyi devre disi birakir."""
    try:
        return float(os.getenv("V7_MAX_GIRIS_PRIM_PCT", "2") or 2.0)
    except ValueError:
        return 2.0


def _cuzdan_durum(http: httpx.Client, rpc, pubkey) -> tuple[float, float, float] | None:
    """Alim aninda taze cuzdan durumu: (mtm_usd, serbest_sol, sol_fiyat_usd).
    MTM = serbest SOL (RPC) x anlik fiyat + acik canli poz degeri (v7_state
    canli_miktar*last_price, dosyadan). Hesaplanamazsa None (fail-closed)."""
    from hibrit_trader.jupiter import LAMPORTS_PER_SOL, fetch_sol_price_usd

    try:
        lamports = rpc.get_balance(pubkey).value
    except Exception as e:
        log.warning("BROKER live: SOL bakiyesi alinamadi: %s", e)
        return None
    fiyat = fetch_sol_price_usd(http, fallback=0.0)
    if fiyat <= 0:
        log.warning("BROKER live: SOL fiyati alinamadi")
        return None
    from hibrit_trader import canli_gosterge

    serbest_sol = lamports / LAMPORTS_PER_SOL
    poz_usd = canli_gosterge._v7_canli_ozet()[0]
    return serbest_sol * fiyat + poz_usd, serbest_sol, fiyat


class LiveExecBroker(DryrunExecBroker):
    mode = "live"

    def __init__(self, http: httpx.Client | None = None):
        if not live_kilit_acik():
            raise RuntimeError(
                "live kilidi kapali: LIVE_UNLOCKED=1 ve LIVE_ONAY dosyasi gerekli")
        # acilis probu: hatali cuzdan konfigurasyonu ilk trade'de degil boot'ta
        # yakalansin (12 Tem InvalidChar(91) otopsisi). Sadece pubkey loglanir.
        keypair = _cuzdan_yukle("live")
        log.warning("BROKER live hazir, cuzdan %s", keypair.pubkey())
        self._belirsiz_kilit = False
        self._belirsiz_lock = threading.Lock()
        self._belirsiz_bekleyen: dict | None = None
        self._belirsiz_sonuc_kaydi: tuple[str, str, dict | None] | None = None
        super().__init__(http=http)

    def execute(self, order: ExecOrder) -> ExecFill:
        if self._belirsiz_kilit:
            # 12 Tem olayi: para cikmis olabilirken tekrar almak kasayi bosaltti.
            # Belirsiz islem sonrasi tekrar deneme yasak; sadece restart acar.
            log.error("BROKER live: belirsiz islem kilidi acik, islem reddedildi")
            return ExecFill(ok=False, neden="belirsiz_kilit")
        if not live_kilit_acik():  # her cagrida yeniden kontrol
            log.error("BROKER live: kilit kapali, islem reddedildi")
            return ExecFill(ok=False, neden="kilit_kapali")
        try:
            keypair = _cuzdan_yukle("live")
        except RuntimeError as e:
            log.error("BROKER live: %s", e)
            return ExecFill(ok=False, neden="cuzdan_yok")
        dec = self._decimals(order.token_address)
        if dec is None:
            return ExecFill(ok=False, neden="decimals_yok")
        from hibrit_trader import kota

        # Swap = 2 Jupiter istegi (quote + build). Satis kosulsuz gecer
        # (kova eksiye dusebilir); alim ancak kova alim tabaninin ustundeyse.
        sinif = "satis" if order.yon == "sat" else "alim"
        if not kota.izin("jupiter", sinif, maliyet=2.0):
            log.error("BROKER live: jupiter kota reddi (%s), islem reddedildi",
                      sinif)
            return ExecFill(ok=False, neden="kota_reddi")
        t0 = time.monotonic()
        try:
            from solana.rpc.api import Client as RpcClient

            # Kasa SOL cinsinden: alim SOL->token, satis token->SOL (11 Tem karari)
            from hibrit_trader.jupiter import (GirisPrimAsimi, swap_sol_to_token,
                                               swap_token_to_sol)
            rpc = RpcClient(_rpc_url())
            if order.yon == "al":
                # Canli bilet SABIT ORAN (12 Tem nihai karari): motorun paper
                # bileti (order.usd) canli tarafta KULLANILMAZ; bilet = MTM x
                # LIVE_TICKET_PCT, alim aninda taze hesap. Satis her zaman
                # gercek cuzdan miktari.
                from hibrit_trader.jupiter import SOL_GAS_RESERVE

                durum = _cuzdan_durum(self._http, rpc, keypair.pubkey())
                if durum is None:
                    # bilet hesabi yoksa canli alim yok (fail-closed)
                    log.error("BROKER live: cuzdan durumu hesaplanamadi, "
                              "alim reddedildi")
                    return ExecFill(ok=False, neden="bilet_hesap_yok")
                mtm, serbest_sol, sol_fiyat = durum
                pct = _live_ticket_pct()
                usd_exec = mtm * pct / 100.0
                if usd_exec <= 0:
                    log.error("BROKER live: bilet hesabi gecersiz (MTM $%.2f "
                              "x %%%g), alim reddedildi", mtm, pct)
                    return ExecFill(ok=False, neden="bilet_hesap_yok")
                gerekli_sol = usd_exec / sol_fiyat + SOL_GAS_RESERVE
                if serbest_sol < gerekli_sol:
                    log.error("BROKER live: serbest SOL yetersiz "
                              "(%.4f < %.4f, bilet $%.2f + gaz rezervi), "
                              "alim reddedildi", serbest_sol, gerekli_sol,
                              usd_exec)
                    return ExecFill(ok=False, neden="yetersiz_serbest")
                log.warning("BROKER LIVE %s bilet: MTM $%.2f x %%%g = $%.2f "
                            "(paper $%.2f kullanilmadi)", order.engine, mtm,
                            pct, usd_exec, order.usd)
                prim_esik = _max_giris_prim_pct()
                min_out_raw = None
                if prim_esik > 0 and order.ref_fiyat > 0:
                    min_out_raw = int(usd_exec
                                      / (order.ref_fiyat * (1 + prim_esik / 100.0))
                                      * 10 ** dec)
                try:
                    res = swap_sol_to_token(self._http, rpc, keypair,
                                            order.token_address, usd_exec,
                                            order.slippage_bps,
                                            min_out_raw=min_out_raw)
                except GirisPrimAsimi as e:
                    gecikme_ms = round((time.monotonic() - t0) * 1000, 1)
                    ima = (usd_exec / (e.out_raw / 10 ** dec)
                           if e.out_raw > 0 else 0.0)
                    prim = ((ima / order.ref_fiyat - 1) * 100
                            if order.ref_fiyat > 0 else 0.0)
                    log.warning("BROKER LIVE giris prim kapisi %s: ima fiyat "
                                "%.8g, karar %.8g, prim %+.2f%% > esik %%%.2f; "
                                "imza atilmadi, giris iptal", order.engine,
                                ima, order.ref_fiyat, prim, prim_esik)
                    return ExecFill(ok=False, neden="giris_prim_asimi",
                                    gecikme_ms=gecikme_ms)
                miktar_quote = res["out_amount"] / 10 ** dec
                # 14 Tem olayi: quote miktari kaydedilince gercek dolumla
                # sapma satisi kalici 6024'e (eksik) veya kirintiya (fazla)
                # dusurur. Kayit = zincir gercegi.
                gercek = _zincir_dolum(self._http, res["signature"],
                                       str(keypair.pubkey()), order.token_address)
                if gercek is not None and gercek > 0:
                    if abs(gercek - miktar_quote) > 1e-9:
                        log.warning("BROKER LIVE dolum farki: zincir %.6g vs "
                                    "quote %.6g (fark %+.6g), kayit zincir",
                                    gercek, miktar_quote, gercek - miktar_quote)
                    miktar_token = gercek
                else:
                    log.warning("BROKER LIVE dolum zincirden okunamadi, "
                                "quote miktari kaydedildi (%.6g)", miktar_quote)
                    miktar_token = miktar_quote
                fiyat = res["cost_usd"] / miktar_token if miktar_token > 0 else 0.0
            else:
                zincir = _zincir_token_bakiye(self._http, str(keypair.pubkey()),
                                              order.token_address)
                if (zincir is not None and zincir < order.amount_token
                        and order.acilis_ts is not None
                        and time.time() - order.acilis_ts < TAZE_POZISYON_SEC):
                    log.warning("BROKER LIVE: taze pozisyon (%.0fs), zincir "
                                "%.6g < kayit %.6g; RPC gecikmesi varsayildi, "
                                "satis kayitla denenecek",
                                time.time() - order.acilis_ts, zincir,
                                order.amount_token)
                    zincir = None
                miktar_sat = _satis_miktari(order.amount_token, zincir)
                if miktar_sat is None:
                    log.critical("BROKER live: zincirde token bakiyesi yok "
                                 "(kayit %.6g, mint %s); manuel islem suphesi, "
                                 "satis reddedildi", order.amount_token,
                                 order.token_address)
                    satis_stresi_bildir()
                    return ExecFill(ok=False, neden="zincir_bakiye_yok")
                if miktar_sat < order.amount_token:
                    log.warning("BROKER LIVE satis miktari zincire indirildi: "
                                "kayit %.6g -> zincir %.6g", order.amount_token,
                                miktar_sat)
                amount_raw = max(1, int(miktar_sat * 10 ** dec))
                res = swap_token_to_sol(self._http, rpc, keypair,
                                        order.token_address, amount_raw,
                                        order.slippage_bps)
                miktar_token = miktar_sat
                fiyat = (res["proceeds_usd"] / miktar_sat
                         if miktar_sat > 0 else 0.0)
        except Exception as e:
            gecikme_ms = round((time.monotonic() - t0) * 1000, 1)
            if order.yon == "sat":
                satis_stresi_bildir()
            if str(e).startswith("islem_belirsiz"):
                # tx zincirde olabilir; para cikmis olabilir, muhasebe yok.
                self._belirsiz_kilit = True
                sig_str = str(e).split(":", 1)[1] if ":" in str(e) else ""
                if order.yon == "al" and sig_str:
                    # R2-alim: arka plan uzlastirici zincire sorup karar verir;
                    # kilit sonuc motora teslim edilene kadar kapali kalir.
                    self._belirsiz_izle(order.engine, sig_str,
                                        order.token_address, usd_exec,
                                        str(keypair.pubkey()))
                    log.critical("BROKER live BELIRSIZ ISLEM %s %s: %s; canli "
                                 "islemler kilitlendi, zincir mutabakati "
                                 "arka planda basladi (tx %s)",
                                 order.engine, order.yon, e, sig_str)
                else:
                    log.critical("BROKER live BELIRSIZ ISLEM %s %s: %s; canli "
                                 "islemler durduruldu, restart gerekir",
                                 order.engine, order.yon, e)
                return ExecFill(ok=False, neden="islem_belirsiz",
                                gecikme_ms=gecikme_ms)
            log.error("BROKER live islem hatasi %s %s: %s",
                      order.engine, order.yon, e)
            return ExecFill(ok=False, neden="islem_hatasi",
                            gecikme_ms=gecikme_ms)
        gecikme_ms = round((time.monotonic() - t0) * 1000, 1)
        if order.yon == "sat":
            satis_stresi_temizle()
        log.warning("BROKER LIVE %s %s fiyat %.8g miktar %.6g tx %s (%.0f ms)",
                    order.engine, order.yon, fiyat, miktar_token,
                    res["signature"], gecikme_ms)
        return ExecFill(ok=True, fiyat=fiyat, miktar_token=miktar_token,
                        fee_usd=0.0, tx_id=res["signature"], gecikme_ms=gecikme_ms)

    # ---- R2-alim: belirsiz islem zincir mutabakati -------------------------------------

    def _belirsiz_izle(self, engine: str, sig: str, token: str,
                       usd_exec: float, pubkey: str) -> None:
        with self._belirsiz_lock:
            self._belirsiz_bekleyen = {
                "engine": engine, "sig": sig, "token": token,
                "usd_exec": usd_exec, "pubkey": pubkey,
                "ts": round(time.time(), 3),
            }
            self._belirsiz_sonuc_kaydi = None
        self._belirsiz_thread_baslat()

    def _belirsiz_thread_baslat(self) -> None:
        threading.Thread(target=self._belirsiz_uzlastir_worker,
                         daemon=True, name="belirsiz-uzlastir").start()

    def _belirsiz_sonucu_yaz(self, durum: str, detay: dict | None) -> None:
        with self._belirsiz_lock:
            engine = (self._belirsiz_bekleyen or {}).get("engine", "?")
            self._belirsiz_sonuc_kaydi = (engine, durum, detay)
            self._belirsiz_bekleyen = None

    def _belirsiz_uzlastir_worker(self) -> None:
        with self._belirsiz_lock:
            ctx = dict(self._belirsiz_bekleyen or {})
        if not ctx:
            return
        sig = ctx["sig"]
        baslangic = time.monotonic()
        yok_sayac = 0
        while True:
            time.sleep(_belirsiz_poll_sec())
            gecen = time.monotonic() - baslangic
            if gecen >= _belirsiz_cap_sec():
                self._belirsiz_sonucu_yaz("cozulemedi", None)
                log.critical("BROKER BELIRSIZ COZULEMEDI %s: %.0fs icinde "
                             "zincirden net cevap alinamadi; kilit kapali "
                             "kaliyor, manuel kontrol gerekir (tx %s)",
                             ctx["engine"], gecen, sig)
                return
            durum = _zincir_imza_durumu(self._http, sig)
            if durum == "onaylandi":
                miktar = _zincir_dolum(self._http, sig, ctx["pubkey"],
                                       ctx["token"])
                if miktar is not None and miktar > 0:
                    fiyat = ctx["usd_exec"] / miktar
                    self._belirsiz_sonucu_yaz("gerceklesti", {
                        "token_address": ctx["token"], "fiyat": fiyat,
                        "miktar_token": miktar, "tx_id": sig,
                        "usd_exec": ctx["usd_exec"],
                    })
                    log.warning("BROKER BELIRSIZ COZULDU %s: tx zincirde, "
                                "dolum %.6g @ %.8g; pozisyon motora "
                                "devrediliyor (tx %s)", ctx["engine"],
                                miktar, fiyat, sig)
                    return
                # para cikti ama dolum okunamadi: cap'e kadar tekrar dene
                log.warning("BROKER belirsiz: tx onayli ama dolum okunamadi, "
                            "tekrar denenecek (tx %s)", sig)
            elif durum == "hatali":
                # tx zincirde ama basarisiz: swap atomik, para cikmadi (yalniz fee)
                self._belirsiz_sonucu_yaz("yok", None)
                log.warning("BROKER BELIRSIZ COZULDU %s: tx zincirde ama "
                            "BASARISIZ, swap gecmedi, kayit yok (tx %s)",
                            ctx["engine"], sig)
                return
            elif durum == "yok":
                yok_sayac += 1
                if gecen >= _belirsiz_yok_sec() and yok_sayac >= 3:
                    self._belirsiz_sonucu_yaz("yok", None)
                    log.warning("BROKER BELIRSIZ COZULDU %s: tx zincirde YOK "
                                "(%d sorgu, %.0fs), para cikmadi, kayit yok "
                                "(tx %s)", ctx["engine"], yok_sayac, gecen, sig)
                    return
            # durum None: sorgu hatasi, sonraki turda tekrar

    def belirsiz_sonuc(self, engine: str) -> tuple[str, dict | None]:
        """Motor tarafi mutabakat sorgusu. Donus durumlari:
        'bekliyor' (uzlastirici calisiyor), 'gerceklesti' (detay ile; pozisyon
        motora devredilir), 'yok' (tx zincirde yok, kayit yazilmaz),
        'cozulemedi' (kilit kapali kalir), 'kayit_yok' (bekleyen is yok).
        gerceklesti/yok tuketiminde kilit otomatik acilir: para durumu netlesti."""
        with self._belirsiz_lock:
            kayit = self._belirsiz_sonuc_kaydi
            if kayit is not None and kayit[0] == engine:
                self._belirsiz_sonuc_kaydi = None
                _, durum, detay = kayit
                if durum in ("gerceklesti", "yok"):
                    self._belirsiz_kilit = False
                    log.warning("BROKER live: belirsiz kilidi acildi (%s)", durum)
                return durum, detay
            bekleyen = self._belirsiz_bekleyen
            if bekleyen is not None and bekleyen.get("engine") == engine:
                return "bekliyor", None
            return "kayit_yok", None


# ---- Fabrika ---------------------------------------------------------------------------

def make_exec_broker(mode: str | None = None, http: httpx.Client | None = None):
    """Yurutme brokeri fabrikasi. Varsayilan paper (mevcut davranis)."""
    mode = (mode or os.getenv("BROKER_MODE", "paper")).strip().lower()
    if mode == "paper":
        return PaperExecBroker()
    if mode == "dryrun":
        return DryrunExecBroker(http=http)
    if mode == "live":
        if not live_kilit_acik():
            raise RuntimeError(
                "live kilidi kapali: LIVE_UNLOCKED=1 ve data/LIVE_ONAY "
                "(icerik: canli-islem-onayliyorum) birlikte gerekli")
        return LiveExecBroker(http=http)
    raise ValueError(f"bilinmeyen broker modu: {mode}")


# ---- Golge olcum: paper fill aninda paralel dryrun quote kiyasi ------------------------

_golge_lock = threading.Lock()
_golge_broker: DryrunExecBroker | None = None


def _get_golge_broker() -> DryrunExecBroker:
    global _golge_broker
    with _golge_lock:
        if _golge_broker is None:
            _golge_broker = DryrunExecBroker()
        return _golge_broker


def _golge_worker(engine: str, yon: str, token_address: str, paper_fiyat: float,
                  usd: float | None, amount_token: float | None,
                  broker: DryrunExecBroker | None = None) -> None:
    try:
        br = broker or _get_golge_broker()
        miktar = usd if yon == "al" else amount_token
        t0 = time.monotonic()
        q, neden = br._quote(token_address, yon, miktar or 0.0, slippage_bps=50)
        gecikme_ms = round((time.monotonic() - t0) * 1000, 1)
        row = {
            "ts": round(time.time(), 3), "tur": "golge", "engine": engine,
            "yon": yon, "token": token_address, "paper_fiyat": paper_fiyat,
            "gecikme_ms": gecikme_ms,
        }
        if q is None:
            row["neden"] = neden
        else:
            fark = (round((q.fiyat / paper_fiyat - 1) * 10_000, 2)
                    if paper_fiyat > 0 else None)
            row.update({"jup_fiyat": q.fiyat, "fark_bps": fark, "route": q.route})
            log.info("GOLGE %s %s %s paper %.8g vs jup %.8g fark %s bps (%.0f ms)",
                     engine, yon, token_address[:6], paper_fiyat, q.fiyat,
                     fark, gecikme_ms)
        _fills_yaz(row)
    except Exception as e:
        log.warning("GOLGE olcum hatasi (%s %s): %s", engine, yon, e)


def jupiter_referans_fiyat(token_address: str, usd: float = 100.0) -> float | None:
    """Bagimsiz gercek-fiyat hakemi: Jupiter'den kucuk bir alis quote'u alip
    birim fiyati dondurur. Hata/eksik veri durumunda None (fail-closed:
    cagiran taraf hakem yoksa guvenme kararini kendi verir)."""
    if satis_stresi_aktif():
        log.info("HAKEM atlandi: canli satis stresi, Jupiter kotasi "
                 "satis hattina ayrildi")
        return None
    from hibrit_trader import kota

    if not kota.izin("jupiter", "tarama"):
        log.debug("HAKEM atlandi: jupiter kota reddi (tarama)")
        return None
    try:
        q, _neden = _get_golge_broker()._quote(token_address, "al", usd,
                                               slippage_bps=100)
        return q.fiyat if q is not None else None
    except Exception as e:
        log.warning("HAKEM: Jupiter referans alinamadi %s: %s", token_address[:8], e)
        return None


def golge_olcum(engine: str, yon: str, token_address: str, paper_fiyat: float, *,
                usd: float | None = None, amount_token: float | None = None) -> None:
    """Paper fill aninda arka planda Jupiter quote alip kiyas loglar.
    Motoru asla bloklamaz ve asla exception sizdirmaz."""
    try:
        if os.getenv("BROKER_GOLGE_OLCUM", "1").strip() == "0":
            return
        if satis_stresi_aktif():
            log.info("GOLGE atlandi (%s %s): canli satis stresi, Jupiter "
                     "kotasi satis hattina ayrildi", engine, yon)
            return
        from hibrit_trader import kota

        if not kota.izin("jupiter", "tarama"):
            log.debug("GOLGE atlandi (%s %s): jupiter kota reddi", engine, yon)
            return
        threading.Thread(
            target=_golge_worker,
            args=(engine, yon, token_address, paper_fiyat, usd, amount_token),
            daemon=True, name=f"golge-{engine}-{yon}",
        ).start()
    except Exception as e:
        log.warning("GOLGE baslatilamadi (%s %s): %s", engine, yon, e)
