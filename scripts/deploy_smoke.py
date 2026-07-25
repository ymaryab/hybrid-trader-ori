#!/usr/bin/env python3
"""Deploy oncesi import-smoke (25 Tem; R2 banner olumu dersi).

Restart ATILMADAN once calistirilir: filo modullerinin tamami import
edilir ve motor siniflari yoklanir. Amac, NameError/SyntaxError gibi
"thread sessiz olur" sinifi hatalari yayin ONCESI yakalamak (R2 banner
vakasi: 2 gun sessiz olu thread).

Kullanim: python scripts/deploy_smoke.py   (cikis kodu 0 = temiz)
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

MODULLER = [
    "hibrit_trader.momentum_session", "hibrit_trader.canli_session",
    "hibrit_trader.panel", "hibrit_trader.otonom_secici",
    "hibrit_trader.senkron_bekcisi", "hibrit_trader.killswitch",
    "hibrit_trader.entry_fresh", "hibrit_trader.kosucu_ekg",
    "hibrit_trader.r1_session", "hibrit_trader.r2_session",
    "hibrit_trader.yz_session", "hibrit_trader.yzn1_session",
    "hibrit_trader.v7_session", "hibrit_trader.v7c_session",
    "hibrit_trader.v7d_session", "hibrit_trader.v7hizli_session",
    "hibrit_trader.v7ht_session", "hibrit_trader.v7new_session",
    "hibrit_trader.v7t_session", "hibrit_trader.m1_session",
    "hibrit_trader.edge.golge", "hibrit_trader.edge.edge_motoru",
    "hibrit_trader.gozlem.ana",
]


def main() -> int:
    hatalar = []
    for ad in MODULLER:
        try:
            importlib.import_module(ad)
        except Exception:  # noqa: BLE001
            hatalar.append((ad, traceback.format_exc(limit=3)))
    if hatalar:
        print(f"SMOKE KIRMIZI: {len(hatalar)} modul import edilemedi\n")
        for ad, iz in hatalar:
            print(f"--- {ad}\n{iz}")
        return 1
    print(f"SMOKE YESIL: {len(MODULLER)} modul temiz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
