#!/usr/bin/env python
"""Cuzdan baglanti kontrolu: keypair dosyasini okur, RPC'den bakiyeleri ceker.

SADECE OKUMA yapar: hicbir islem imzalamaz, hicbir sey gondermez.
- Keypair: SOL_KEYPAIR_PATH veya varsayilan ~/.solana/bot_cuzdan.json.
  Iki format desteklenir: solana-keygen JSON dizisi (64 tamsayi) ve base58.
- RPC: SOLANA_RPC_URL veya public mainnet.
- Cikti: public key, SOL bakiyesi, USDC bakiyesi.

Calistirma:
    .venv/bin/python scripts/cuzdan_kontrol.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
VARSAYILAN_YOL = Path.home() / ".solana" / "bot_cuzdan.json"
VARSAYILAN_RPC = "https://api.mainnet-beta.solana.com"


def keypair_yukle(yol: Path):
    """solana-keygen JSON dizisi (64 bayt) veya base58 tek satir."""
    from solders.keypair import Keypair

    ham = yol.read_text(encoding="utf-8").strip()
    if ham.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(ham)))
    return Keypair.from_base58_string(ham)


def _rpc(client: httpx.Client, url: str, method: str, params: list) -> dict:
    r = client.post(url, json={"jsonrpc": "2.0", "id": 1,
                               "method": method, "params": params})
    r.raise_for_status()
    veri = r.json()
    if "error" in veri:
        raise RuntimeError(f"RPC hatasi: {veri['error']}")
    return veri["result"]

def sol_bakiye(client: httpx.Client, url: str, pub: str) -> float:
    return _rpc(client, url, "getBalance", [pub])["value"] / 1e9


def usdc_bakiye(client: httpx.Client, url: str, pub: str) -> float:
    res = _rpc(client, url, "getTokenAccountsByOwner",
               [pub, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}])
    toplam = 0.0
    for hesap in res["value"]:
        ui = hesap["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
        toplam += ui or 0.0
    return toplam


def main() -> None:
    yol = Path(os.getenv("SOL_KEYPAIR_PATH") or VARSAYILAN_YOL)
    if not yol.exists():
        sys.exit(f"IPTAL: keypair dosyasi yok: {yol} (once scripts/keypair_uret.py)")
    kp = keypair_yukle(yol)
    pub = str(kp.pubkey())
    url = os.getenv("SOLANA_RPC_URL") or VARSAYILAN_RPC
    print(f"Keypair: {yol}")
    print(f"Public key: {pub} ({pub[:4]} ... {pub[-4:]})")
    print(f"RPC: {url}")
    with httpx.Client(timeout=15) as client:
        sol = sol_bakiye(client, url, pub)
        usdc = usdc_bakiye(client, url, pub)
    print(f"SOL bakiyesi : {sol:.6f} SOL")
    print(f"USDC bakiyesi: {usdc:.2f} USDC")
    if sol == 0:
        print("UYARI: SOL 0; islem ucretleri icin az miktar SOL gerekli.")


if __name__ == "__main__":
    main()
