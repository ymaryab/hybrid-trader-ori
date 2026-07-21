#!/usr/bin/env python3
"""Canli hat kaynak swap (10. motor mimarisi, 20 Tem revizyonu).

CANLI 10. motor sabittir; bu script SADECE kural kaynagini degistirir:
systemd drop-in'e CANLI_KAYNAK_MOTOR=<hedef> yazar ve servisi restart
eder. Hicbir state sifirlanmaz; birikimli defter surer, kural_degisim
satirini restart'ta canli_session._kural_kontrol kendisi atar.

Kullanim:
    python scripts/canli_swap.py v7hizli      # kaynagi v7hizli yap
    python scripts/canli_swap.py r1           # kaynagi r1 yap
    python scripts/canli_swap.py --status     # aktif kaynak + acik pozlar
    python scripts/canli_swap.py --dry-run r1 # plani goster, uygulama
    python scripts/canli_swap.py --zorla r1   # acik canli poza ragmen (RISK)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(os.getenv("MOMENTUM_DATA_DIR",
                          str(Path.home() / "yz/hybrid-trader-ori/data")))
UNIT_NAME = "momentum-trader.service"
UNIT_DIR = Path.home() / ".config/systemd/user/momentum-trader.service.d"
DROPIN_FILE = UNIT_DIR / "canli-motor.conf"


def desteklenen_kaynaklar() -> set[str]:
    """canli_session'daki gercek liste; import edilemezse bilinen ikili."""
    try:
        from hibrit_trader.canli_session import DESTEKLENEN_KAYNAKLAR
        return set(DESTEKLENEN_KAYNAKLAR)
    except Exception:
        return {"r1", "r2", "v7", "v7c", "v7d", "v7t", "v7hizli", "v7ht"}


def aktif_kaynak() -> str:
    if DROPIN_FILE.exists():
        for ln in DROPIN_FILE.read_text().splitlines():
            if ln.startswith("Environment=CANLI_KAYNAK_MOTOR="):
                return ln.split("=", 2)[-1].strip().lower()
    return "r1"


def acik_canli_pozlar() -> list[dict]:
    try:
        s = json.loads((DATA_DIR / "canli_state.json").read_text())
    except Exception:
        return []
    return [p for p in s.get("positions", [])
            if float(p.get("canli_miktar") or 0.0) > 0]


def dropin_yaz(kaynak: str) -> None:
    """Sadece CANLI_KAYNAK_MOTOR satirini degistir, diger satirlar korunur."""
    satirlar = []
    if DROPIN_FILE.exists():
        satirlar = [ln for ln in DROPIN_FILE.read_text().splitlines()
                    if ln.strip() and not ln.startswith("Environment=CANLI_KAYNAK_MOTOR=")]
    if not satirlar:
        satirlar = ["[Service]", "Environment=CANLI_MOTOR=canli",
                    "Environment=CANLI_ENABLED=1"]
    satirlar.append(f"Environment=CANLI_KAYNAK_MOTOR={kaynak}")
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    DROPIN_FILE.write_text("\n".join(satirlar) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Canli hat kaynak swap (10. motor)")
    ap.add_argument("kaynak", nargs="?",
                    help="hedef kural kaynagi: " + " | ".join(sorted(desteklenen_kaynaklar())))
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--zorla", action="store_true",
                    help="acik canli pozisyona ragmen devam (kural ortasi devir RISKI)")
    a = ap.parse_args()

    aktif = aktif_kaynak()
    if a.status:
        print(f"aktif kaynak : {aktif}")
        print(f"desteklenen  : {sorted(desteklenen_kaynaklar())}")
        pozlar = acik_canli_pozlar()
        print(f"acik canli poz: {len(pozlar)}")
        for p in pozlar:
            print(f"  - {p.get('pair')} canli_miktar={p.get('canli_miktar')}")
        return 0

    if not a.kaynak:
        ap.print_help()
        return 2
    hedef = a.kaynak.strip().lower()
    if hedef not in desteklenen_kaynaklar():
        print(f"HATA: {hedef} desteklenmiyor (mevcut: {sorted(desteklenen_kaynaklar())}). "
              "Yeni kaynak icin once canli_session.py'ye kural ithali gerekir.",
              file=sys.stderr)
        return 2
    if hedef == aktif:
        print(f"zaten aktif kaynak: {hedef}. hicbir sey yapilmadi.")
        return 0

    pozlar = acik_canli_pozlar()
    if pozlar and not a.zorla:
        print(f"HATA: {len(pozlar)} acik canli pozisyon var; kural ortasi devir riskli.")
        for p in pozlar:
            print(f"  - {p.get('pair')} canli_miktar={p.get('canli_miktar')}")
        print("Once pozisyonlar kapansin (veya --zorla).")
        return 3

    plan = [
        f"kaynak degisimi          : {aktif} -> {hedef}",
        "state/defter             : DOKUNULMAZ (birikimli, kural_degisim satiri motor atar)",
        f"drop-in                  : {DROPIN_FILE} (CANLI_KAYNAK_MOTOR={hedef})",
        f"servis restart           : {UNIT_NAME}",
    ]
    print("PLAN:")
    for ln in plan:
        print("  " + ln)
    if a.dry_run:
        print("--dry-run: hicbir sey uygulanmadi.")
        return 0

    dropin_yaz(hedef)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "restart", UNIT_NAME], check=True)
    print(f"OK. canli hat kaynagi artik {hedef.upper()}; defter aynen suruyor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
