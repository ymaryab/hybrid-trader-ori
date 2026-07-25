"""Yaratici sicili: Create olayi ayristirma yapisal testi."""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hibrit_trader.gozlem.yaratici_sicil import create_ayristir
from hibrit_trader.gozlem.lp_kilit import _b58


def _u32(n):
    return n.to_bytes(4, "little")


def test_create_ayristirma():
    # mint b58'i 'pump' ile bitecek sekilde kaba arama (test amacli kucuk uzay)
    mint = None
    for i in range(200000):
        aday = i.to_bytes(32, "big")
        if _b58(aday).endswith("pump"):
            mint = aday
            break
    if mint is None:
        import pytest
        pytest.skip("test uzayinda pump son-ekli mint bulunamadi")
    yaratici = bytes(range(2, 34))
    ham = (b"x" * 8 + _u32(3) + b"ABC" + _u32(2) + b"AB"
           + _u32(5) + b"u" * 5 + mint + bytes(32) + yaratici)
    logs = ["Program log: Instruction: Create",
            "Program data: " + base64.b64encode(ham).decode()]
    r = create_ayristir(logs)
    assert r is not None
    assert r["mint"] == _b58(mint)
    assert r["yaratici"] == _b58(yaratici)


def test_bozuk_veri_none():
    assert create_ayristir(["Program data: !!!"]) is None
    assert create_ayristir(["Program log: bos"]) is None
