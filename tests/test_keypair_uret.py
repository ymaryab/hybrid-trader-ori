"""keypair_uret testleri: BIP39 + SLIP-0010 resmi vektorler, guvenli yazim."""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "keypair_uret", Path(__file__).parent.parent / "scripts" / "keypair_uret.py")
ku = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ku)

# BIP39 resmi test vektoru (Trezor): "abandon x11 + about", parolasiz
_MNEMONIC = " ".join(["abandon"] * 11 + ["about"])
_BIP39_SEED = (
    "5eb00bbddcf069084889a8ab9155568165f5c453ccb85e70811aaed6f6da5fc1"
    "9a5ac40b389cd370d086206dec8aa6c43daea6690f20ad3d8d48b2d2ce9e38e4")


def test_bip39_resmi_vektor():
    assert ku.bip39_seed(_MNEMONIC).hex() == _BIP39_SEED


def test_bip39_bosluk_ve_buyuk_harf_normalize():
    assert ku.bip39_seed("  Abandon   ABANDON " + " ".join(["abandon"] * 9 + ["about"])) \
        == ku.bip39_seed(_MNEMONIC)


# SLIP-0010 ed25519 resmi test vektoru 1 (seed 000102...0f)
_SLIP_SEED = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


def test_slip10_master():
    assert ku.slip10_ed25519(_SLIP_SEED, ()).hex() == \
        "2b4be7f19ee27bbf30c667b642d5f4aa69fd169872f8fc3059c08ebae2eb19e7"


def test_slip10_derin_yol_ve_pubkey():
    key = ku.slip10_ed25519(_SLIP_SEED, (0, 1, 2, 2, 1000000000))
    assert key.hex() == \
        "8f94d394a8e8fd6b1bc2f3f49f5c47e385281d5c17e65324b0f62483e37e8793"
    from solders.keypair import Keypair
    assert bytes(Keypair.from_seed(key).pubkey()).hex() == \
        "3c24da049451555d51a7014a37337aa4e12d41e485abccfa46b47dfb2af54b7a"


def test_main_guvenli_yazim(tmp_path, monkeypatch, capsys):
    hedef = tmp_path / "solana" / "bot_cuzdan.json"
    monkeypatch.setattr(ku, "CIKTI", hedef)
    monkeypatch.setattr(ku.getpass, "getpass", lambda prompt: _MNEMONIC)
    ku.main()
    data = json.loads(hedef.read_text())
    assert len(data) == 64 and all(0 <= b <= 255 for b in data)
    assert stat.S_IMODE(hedef.stat().st_mode) == 0o600
    # tumce hicbir sekilde dosyaya yazilmadi
    assert "abandon" not in hedef.read_text()
    out = capsys.readouterr().out
    assert "Public key:" in out and "abandon" not in out
    # turetme deterministik: dosyadaki gizli seed beklenen anahtar
    beklenen = ku.slip10_ed25519(ku.bip39_seed(_MNEMONIC))
    assert bytes(data[:32]) == beklenen


def test_main_ustune_yazmaz(tmp_path, monkeypatch):
    hedef = tmp_path / "bot_cuzdan.json"
    hedef.write_text("[]")
    monkeypatch.setattr(ku, "CIKTI", hedef)
    with pytest.raises(SystemExit, match="ustune yazmam"):
        ku.main()
    assert hedef.read_text() == "[]"


def test_main_kelime_sayisi_kontrolu(tmp_path, monkeypatch):
    monkeypatch.setattr(ku, "CIKTI", tmp_path / "yok.json")
    monkeypatch.setattr(ku.getpass, "getpass", lambda prompt: "bir iki uc")
    with pytest.raises(SystemExit, match="12 veya 24"):
        ku.main()
    assert not (tmp_path / "yok.json").exists()
