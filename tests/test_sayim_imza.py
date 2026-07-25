"""Sayim imza kesinlestirme (25 Tem): CreateTokenAccount sahte-lansman fixi."""

import asyncio

from hibrit_trader.gozlem.sayim_r2 import SayimR2
from hibrit_trader.gozlem.ortak import Sayaclar


class _Bus:
    def __init__(self):
        self.olaylar = []

    async def yayinla(self, akis, kind, payload, **kw):
        self.olaylar.append((akis, kind))


def _isle(logs, etiket="pumpfun"):
    bus = _Bus()
    s = SayimR2(bus, Sayaclar())
    asyncio.run(s.isle("addr", etiket, {
        "result": {"value": {"logs": logs, "err": None,
                             "signature": "S"},
                   "context": {"slot": 1}}}))
    return [k for _, k in bus.olaylar]


def test_gercek_create_lansman_sayilir():
    assert _isle(["Program log: Instruction: Create"]) == ["LaunchObserved"]


def test_create_token_account_lansman_DEGIL():
    assert _isle(["Program log: Instruction: CreateTokenAccount",
                  "Program log: Instruction: BuyExactSolIn"]) == []


def test_migrate_son_eslesme():
    assert _isle(["Program log: Instruction: Migrate"]) == \
        ["GraduationObserved"]
    assert _isle(["Program log: Instruction: MigrateFunds"]) == []


def test_raydium_icinde_eslesme():
    assert _isle(["Program log: initialize2: InitializeInstruction2 {..}"],
                 "raydium") == ["PoolCreated"]
