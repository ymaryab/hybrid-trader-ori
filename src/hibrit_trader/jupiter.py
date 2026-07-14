"""Jupiter v6 swap — Solana canlı yürütme (ücretsiz quote/swap API)."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Optional

import httpx
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from hibrit_trader.config import API

log = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
SOL_GAS_RESERVE = 0.05  # swap ücreti için cüzdanda bırakılacak SOL


def fetch_sol_price_usd(client: httpx.Client, *, fallback: float = 150.0) -> float:
    """Jupiter SOL→USDC quote ile spot USD fiyatı; hata olursa fallback."""
    try:
        lamports = LAMPORTS_PER_SOL // 10  # 0.1 SOL
        quote = get_quote(client, SOL_MINT, USDC_MINT, lamports, 50)
        usdc_out = int(quote["outAmount"]) / 1_000_000
        price = usdc_out / 0.1
        if price > 0:
            return price
    except Exception as e:
        log.warning("SOL fiyat Jupiter hatası, fallback %.2f: %s", fallback, e)
    return fallback


def usd_to_lamports(usd: float, sol_price_usd: float) -> int:
    if sol_price_usd <= 0:
        raise ValueError("SOL fiyatı geçersiz")
    return max(1, int(usd / sol_price_usd * LAMPORTS_PER_SOL))


def load_keypair(secret: str) -> Keypair:
    """Iki format: base58 string veya solana-keygen JSON bayt dizisi ([..64 sayi]).
    12 Tem InvalidChar(91) otopsisi: dosya JSON dizi iken base58 cozumu '['
    karakterinde patliyordu; iki format da desteklenir."""
    s = secret.strip()
    if s.startswith("["):
        data = bytes(json.loads(s))
        if len(data) != 64:
            raise ValueError(f"keypair JSON dizisi 64 bayt olmali ({len(data)} bayt)")
        return Keypair.from_bytes(data)
    return Keypair.from_base58_string(s)


def get_quote(
    client: httpx.Client,
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    slippage_bps: int,
) -> dict:
    url = f"{API['jupiter_quote']}/quote"
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": str(slippage_bps),
    }
    resp = client.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("outAmount"):
        raise ValueError("Jupiter quote boş yanıt")
    return data


def build_swap_tx(
    client: httpx.Client,
    quote: dict,
    user_pubkey: str,
    *,
    priority: bool = False,
) -> str:
    url = f"{API['jupiter_quote']}/swap"
    payload = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
    }
    if priority or os.getenv("JUPITER_PRIORITY_FEE", "0") == "1":
        payload["prioritizationFeeLamports"] = "auto"
    tip = os.getenv("JITO_TIP_LAMPORTS", "").strip()
    if tip.isdigit() and int(tip) > 0:
        payload["prioritizationFeeLamports"] = int(tip)
    resp = client.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    tx_b64 = resp.json().get("swapTransaction")
    if not tx_b64:
        raise ValueError("Jupiter swapTransaction yok")
    return tx_b64


def _zincir_durumu_ok(rpc: Client, sig, deneme: int = 3, bekleme_sn: float = 2.0) -> bool:
    """Onay adimi coktuyse son care: imza zincirde basarili mi diye sor."""
    for i in range(deneme):
        try:
            durum = rpc.get_signature_statuses([sig]).value[0]
            if durum is not None and durum.err is None:
                return True
        except Exception:
            pass
        if i < deneme - 1:
            time.sleep(bekleme_sn)
    return False


def sign_and_send(
    rpc: Client,
    swap_tx_b64: str,
    keypair: Keypair,
) -> str:
    raw = base64.b64decode(swap_tx_b64)
    unsigned = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(unsigned.message, [keypair])
    result = rpc.send_raw_transaction(bytes(signed))
    if result.value is None:
        raise RuntimeError(f"İşlem reddedildi: {result}")
    # 12 Tem olayi: sig str'a cevrilince confirm_transaction (Signature bekler)
    # patliyordu; tx zincire ulasmisken hata donuyor, motor alimi yok sayip
    # tekrar deniyordu. Nesne olarak birakilir, str sadece donuste.
    sig = result.value
    try:
        rpc.confirm_transaction(sig, commitment=Confirmed)
    except Exception as e:
        if _zincir_durumu_ok(rpc, sig):
            log.warning("onay adimi hata verdi ama tx zincirde: %s (%s)", sig, e)
            return str(sig)
        raise RuntimeError(f"islem_belirsiz:{sig}") from e
    return str(sig)


class GirisPrimAsimi(RuntimeError):
    """Quote ima fiyati karar fiyatinin izin verilen priminin ustunde; imza atilmadi."""

    def __init__(self, out_raw: int, min_out_raw: int):
        self.out_raw = out_raw
        self.min_out_raw = min_out_raw
        super().__init__(f"giris_prim_asimi: quote out {out_raw} < min {min_out_raw}")


def swap_sol_to_token(
    http: httpx.Client,
    rpc: Client,
    keypair: Keypair,
    token_mint: str,
    usd: float,
    slippage_bps: int,
    min_out_raw: int | None = None,
) -> dict:
    """SOL → token alımı (Jupiter wrapAndUnwrapSol)."""
    sol_price = fetch_sol_price_usd(http)
    amount_raw = usd_to_lamports(usd, sol_price)
    quote = get_quote(http, SOL_MINT, token_mint, amount_raw, slippage_bps)
    if min_out_raw is not None and int(quote["outAmount"]) < min_out_raw:
        raise GirisPrimAsimi(int(quote["outAmount"]), min_out_raw)
    tx_b64 = build_swap_tx(http, quote, str(keypair.pubkey()))
    sig = sign_and_send(rpc, tx_b64, keypair)
    in_lamports = int(quote["inAmount"])
    cost_usd = in_lamports / LAMPORTS_PER_SOL * sol_price
    return {
        "signature": sig,
        "in_amount": in_lamports,
        "out_amount": int(quote["outAmount"]),
        "input_mint": SOL_MINT,
        "output_mint": token_mint,
        "cost_usd": cost_usd,
        "sol_price_usd": sol_price,
    }


def swap_token_to_sol(
    http: httpx.Client,
    rpc: Client,
    keypair: Keypair,
    token_mint: str,
    amount_raw: int,
    slippage_bps: int,
) -> dict:
    """Token → SOL satışı."""
    sol_price = fetch_sol_price_usd(http)
    quote = get_quote(http, token_mint, SOL_MINT, amount_raw, slippage_bps)
    priority = os.getenv("JITO_EXIT", "1") != "0"
    tx_b64 = build_swap_tx(http, quote, str(keypair.pubkey()), priority=priority)
    sig = sign_and_send(rpc, tx_b64, keypair)
    out_lamports = int(quote["outAmount"])
    proceeds_usd = out_lamports / LAMPORTS_PER_SOL * sol_price
    return {
        "signature": sig,
        "in_amount": int(quote["inAmount"]),
        "out_amount": out_lamports,
        "proceeds_usd": proceeds_usd,
        "sol_price_usd": sol_price,
    }


def swap_usdc_to_token(
    http: httpx.Client,
    rpc: Client,
    keypair: Keypair,
    token_mint: str,
    usd: float,
    slippage_bps: int,
) -> dict:
    """USDC → token alımı. Dönüş: signature, in_amount, out_amount (raw)."""
    amount_raw = int(usd * 1_000_000)
    quote = get_quote(http, USDC_MINT, token_mint, amount_raw, slippage_bps)
    tx_b64 = build_swap_tx(http, quote, str(keypair.pubkey()))
    sig = sign_and_send(rpc, tx_b64, keypair)
    return {
        "signature": sig,
        "in_amount": int(quote["inAmount"]),
        "out_amount": int(quote["outAmount"]),
        "input_mint": USDC_MINT,
        "output_mint": token_mint,
    }


def swap_token_to_usdc(
    http: httpx.Client,
    rpc: Client,
    keypair: Keypair,
    token_mint: str,
    amount_raw: int,
    slippage_bps: int,
) -> dict:
    """Token → USDC satışı."""
    quote = get_quote(http, token_mint, USDC_MINT, amount_raw, slippage_bps)
    tx_b64 = build_swap_tx(http, quote, str(keypair.pubkey()))
    sig = sign_and_send(rpc, tx_b64, keypair)
    return {
        "signature": sig,
        "in_amount": int(quote["inAmount"]),
        "out_amount": int(quote["outAmount"]),
    }
