#!/usr/bin/env python
"""Phantom kurtarma tumcesinden Solana keypair dosyasi uretir.

Turetme: BIP39 seed + SLIP-0010 ed25519, standart Phantom yolu m/44'/501'/0'/0'.
Cikti: ~/.solana/bot_cuzdan.json (repo DISI), izin 600, solana-keygen formati
(64 baytlik JSON tamsayi listesi: 32 bayt gizli seed + 32 bayt public key).

GUVENLIK:
- Tumce yalnizca getpass ile alinir: ekranda gorunmez, dosyaya/loga/komut
  gecmisine yazilmaz, bu scriptte saklanmaz.
- Var olan dosyanin ustune YAZMAZ (once elle tasi/sil).
- Uretim sonrasi public key ekrana basilir; Phantom'daki adresle karsilastir.

Calistirma:
    .venv/bin/python scripts/keypair_uret.py
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import sys
import unicodedata
from pathlib import Path

CIKTI = Path.home() / ".solana" / "bot_cuzdan.json"
PHANTOM_YOLU = (44, 501, 0, 0)  # m/44'/501'/0'/0' (tum adimlar hardened)


def bip39_seed(tumce: str, parola: str = "") -> bytes:
    """BIP39: kurtarma tumcesinden 64 baytlik seed (PBKDF2-HMAC-SHA512, 2048 tur)."""
    t = unicodedata.normalize("NFKD", " ".join(tumce.lower().split()))
    tuz = unicodedata.normalize("NFKD", "mnemonic" + parola)
    return hashlib.pbkdf2_hmac("sha512", t.encode(), tuz.encode(), 2048, 64)


def slip10_ed25519(seed: bytes, yol: tuple[int, ...] = PHANTOM_YOLU) -> bytes:
    """SLIP-0010 ed25519: seed'den verilen hardened yol boyunca 32 baytlik anahtar."""
    i = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    anahtar, zincir = i[:32], i[32:]
    for adim in yol:
        veri = b"\x00" + anahtar + (0x80000000 | adim).to_bytes(4, "big")
        i = hmac.new(zincir, veri, hashlib.sha512).digest()
        anahtar, zincir = i[:32], i[32:]
    return anahtar


def main() -> None:
    if CIKTI.exists():
        sys.exit(f"IPTAL: {CIKTI} zaten var, ustune yazmam. Once elle tasi veya sil.")

    tumce = getpass.getpass("Kurtarma tumcesi (ekranda gorunmez, sonra Enter): ")
    kelimeler = tumce.split()
    if len(kelimeler) not in (12, 24):
        sys.exit(f"IPTAL: {len(kelimeler)} kelime girildi, 12 veya 24 olmali.")

    from solders.keypair import Keypair

    kp = Keypair.from_seed(slip10_ed25519(bip39_seed(" ".join(kelimeler))))
    del tumce, kelimeler

    CIKTI.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(CIKTI, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(list(bytes(kp)), f)

    pub = str(kp.pubkey())
    print(f"Keypair yazildi: {CIKTI} (izin 600)")
    print(f"Public key: {pub}")
    print(f"Kisa kontrol: {pub[:4]} ... {pub[-4:]}")
    print("Bu adresi Phantom'daki hesap adresiyle karsilastir; eslesmiyorsa")
    print("dosyayi sil, tumceyi kontrol edip tekrar dene.")


if __name__ == "__main__":
    main()
