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
    return load_keypair(p.read_text(encoding="utf-8"))


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

class LiveExecBroker(DryrunExecBroker):
    mode = "live"

    def __init__(self, http: httpx.Client | None = None):
        if not live_kilit_acik():
            raise RuntimeError(
                "live kilidi kapali: LIVE_UNLOCKED=1 ve LIVE_ONAY dosyasi gerekli")
        super().__init__(http=http)

    def execute(self, order: ExecOrder) -> ExecFill:
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
        t0 = time.monotonic()
        try:
            from solana.rpc.api import Client as RpcClient

            from hibrit_trader.jupiter import swap_token_to_usdc, swap_usdc_to_token
            rpc = RpcClient(_rpc_url())
            if order.yon == "al":
                res = swap_usdc_to_token(self._http, rpc, keypair,
                                         order.token_address, order.usd,
                                         order.slippage_bps)
                miktar_token = res["out_amount"] / 10 ** dec
                fiyat = order.usd / miktar_token if miktar_token > 0 else 0.0
            else:
                amount_raw = max(1, int(order.amount_token * 10 ** dec))
                res = swap_token_to_usdc(self._http, rpc, keypair,
                                         order.token_address, amount_raw,
                                         order.slippage_bps)
                miktar_token = order.amount_token
                fiyat = (res["out_amount"] / 1_000_000) / order.amount_token
        except Exception as e:
            log.error("BROKER live islem hatasi %s %s: %s",
                      order.engine, order.yon, e)
            return ExecFill(ok=False, neden="islem_hatasi",
                            gecikme_ms=round((time.monotonic() - t0) * 1000, 1))
        gecikme_ms = round((time.monotonic() - t0) * 1000, 1)
        log.warning("BROKER LIVE %s %s fiyat %.8g miktar %.6g tx %s (%.0f ms)",
                    order.engine, order.yon, fiyat, miktar_token,
                    res["signature"], gecikme_ms)
        return ExecFill(ok=True, fiyat=fiyat, miktar_token=miktar_token,
                        fee_usd=0.0, tx_id=res["signature"], gecikme_ms=gecikme_ms)


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
        threading.Thread(
            target=_golge_worker,
            args=(engine, yon, token_address, paper_fiyat, usd, amount_token),
            daemon=True, name=f"golge-{engine}-{yon}",
        ).start()
    except Exception as e:
        log.warning("GOLGE baslatilamadi (%s %s): %s", engine, yon, e)
