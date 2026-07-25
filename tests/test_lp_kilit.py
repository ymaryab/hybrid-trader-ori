"""LP kilit sensoru: v4 ofset ayristirma + oz-denetim."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hibrit_trader.gozlem.lp_kilit import _b58, v4_mintler


def test_v4_ofset_ayristirma():
    base = bytes(range(1, 33))
    quote = bytes(range(33, 65))
    lp = bytes(range(65, 97))
    ham = bytes(400) + base + quote + lp + bytes(100)
    m = v4_mintler(ham)
    assert m["base"] == _b58(base)
    assert m["quote"] == _b58(quote)
    assert m["lp"] == _b58(lp)
    assert v4_mintler(bytes(100)) == {}       # kisa veri: bos, patlamaz


def test_b58_bilinen_deger():
    # 32 sifir bayti -> 32 adet '1' (base58 onde-sifir kurali)
    assert _b58(bytes(32)) == "1" * 32
