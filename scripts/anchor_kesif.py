#!/usr/bin/env python3
"""Anchor layout KESFI (K3 on-adimi, blueprint bolum 2).

SwapObserved arsivi ETIKETLI veridir: her olayin token'i bellidir.
Bu yuzden layout tahmin edilmez, OGRENILIR:
  - discriminator gruplari cikarilir,
  - bilinen mint'in 32-bayt penceresi aranarak mint_ofs bulunur,
  - u64 pencerelerinde lamport-makul alanlar sol_ofs adaylari olur,
  - "Instruction: Buy/Sell" log korelasyonuyla tur atanir,
  - ornekler arasi DEGISEN 32B pencere user_ofs adayidir.
Cikti: data/gozlem/anchor_kayit.json (sv'li; mevcutsa YENI disc'ler
EKLENIR, eskiler degistirilmez: Madde 11) + stdout kesif raporu.

Kullanim: python scripts/anchor_kesif.py [--ornek 40000]
"""

from __future__ import annotations

import argparse
import base64
import glob
import io
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hibrit_trader.gozlem.anchor_decode import (SCHEMA_VERSION,       # noqa: E402
                                                SOL_MAX_LAMPORT,
                                                SOL_MIN_LAMPORT)
from hibrit_trader.gozlem.lp_kilit import _ABC                        # noqa: E402

_B58_INDEX = {c: i for i, c in enumerate(_ABC)}


def b58_coz32(s: str) -> bytes | None:
    n = 0
    for c in s:
        v = _B58_INDEX.get(c)
        if v is None:
            return None
        n = n * 58 + v
    ham = n.to_bytes((n.bit_length() + 7) // 8, "big")
    ham = b"\x00" * (len(s) - len(s.lstrip("1"))) + ham
    return ham if len(ham) == 32 else None


def olaylar(veri: Path, ornek: int):
    n = 0
    for yolad in sorted(glob.glob(
            str(veri / "gozlem/events/*/*.swap.jsonl*"))):
        if yolad.endswith(".zst"):
            p = subprocess.run(["zstd", "-dc", yolad],
                               capture_output=True, check=True)
            fh = io.BytesIO(p.stdout)
        else:
            fh = open(yolad, "rb")
        with fh:
            for ln in fh:
                if b"SwapObserved" not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("kind") != "SwapObserved":
                    continue
                yield e
                n += 1
                if n >= ornek:
                    return


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ornek", type=int, default=40000)
    ap.add_argument("--veri", default="data")
    a = ap.parse_args()
    veri = Path(a.veri)

    gruplar: dict = defaultdict(lambda: {
        "n": 0, "boylar": Counter(), "mint_ofs": Counter(),
        "sol_adaylari": Counter(), "buy_n": 0, "sell_n": 0,
        "degisen32": defaultdict(set)})
    for e in olaylar(veri, a.ornek):
        tok = e.get("token")
        mint_b = b58_coz32(tok) if tok else None
        logs = (e.get("payload") or {}).get("logs") or []
        yon = ("buy" if any("Instruction: Buy" in l for l in logs)
               else "sell" if any("Instruction: Sell" in l for l in logs)
               else None)
        for l in logs:
            if not l.startswith("Program data: "):
                continue
            try:
                ham = base64.b64decode(l[14:])
            except Exception:  # noqa: BLE001
                continue
            if len(ham) < 40:
                continue
            g = gruplar[ham[:8].hex()]
            g["n"] += 1
            g["boylar"][len(ham)] += 1
            if yon == "buy":
                g["buy_n"] += 1
            elif yon == "sell":
                g["sell_n"] += 1
            if mint_b:
                ofs = ham.find(mint_b)
                if ofs >= 8:
                    g["mint_ofs"][ofs] += 1
            for o in range(8, min(len(ham) - 7, 200), 8):
                v = int.from_bytes(ham[o:o + 8], "little")
                if SOL_MIN_LAMPORT <= v <= SOL_MAX_LAMPORT:
                    g["sol_adaylari"][o] += 1
            if g["n"] <= 50:
                for o in range(8, min(len(ham) - 31, 200), 8):
                    g["degisen32"][o].add(ham[o:o + 32])

    print("=== KESIF RAPORU (ornek=%d) ===" % a.ornek)
    oneriler = {}
    for disc, g in sorted(gruplar.items(), key=lambda x: -x[1]["n"]):
        if g["n"] < 100:
            continue
        boy = g["boylar"].most_common(1)[0][0]
        mint = g["mint_ofs"].most_common(1)[0] if g["mint_ofs"] else None
        yon = ("buy" if g["buy_n"] > 5 * max(g["sell_n"], 1) else
               "sell" if g["sell_n"] > 5 * max(g["buy_n"], 1) else "karma")
        print("disc=%s n=%d boy=%s yon=%s(b%d/s%d) mint_ofs=%s" %
              (disc, g["n"], boy, yon, g["buy_n"], g["sell_n"],
               mint))
        solar = [o for o, c in g["sol_adaylari"].items()
                 if c >= 0.9 * g["n"]]
        degisen = sorted(o for o, s in g["degisen32"].items()
                         if len(s) >= min(20, max(2, g["n"] // 3)))
        print("   sol_aday_ofs(>=%%90): %s" % sorted(solar)[:8])
        print("   degisen32_ofs: %s" % degisen[:8])
        if mint and mint[1] >= 0.9 * g["n"] and yon != "karma":
            m_ofs = mint[0]
            sol_ofs = min((o for o in solar if o != m_ofs),
                          default=None)
            user_aday = [o for o in degisen
                         if abs(o - m_ofs) >= 32]
            oneriler[disc] = {
                "tur": "pumpswap_" + yon, "mint_ofs": m_ofs,
                "sol_ofs": sol_ofs, "token_ofs": None,
                "user_ofs": (user_aday[0] if user_aday else None),
                "is_buy": yon == "buy", "boy_min": boy,
                "kesif_n": g["n"]}
    print("\n=== ONERILEN KAYITLAR ===")
    print(json.dumps(oneriler, indent=1))
    if os.getenv("KESIF_YAZ") == "1" and oneriler:
        yol = veri / "gozlem" / "anchor_kayit.json"
        try:
            mevcut = json.loads(yol.read_text())
        except (OSError, ValueError):
            mevcut = {"sv": SCHEMA_VERSION, "kayitlar": {}}
        for disc, k in oneriler.items():
            mevcut["kayitlar"].setdefault(disc, k)   # eskiye dokunma
        mevcut["kesif_ts"] = time.time()
        yol.write_text(json.dumps(mevcut, indent=1))
        print("anchor_kayit.json guncellendi (yalniz yeni disc'ler)")


if __name__ == "__main__":
    main()
