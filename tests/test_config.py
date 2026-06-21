from hibrit_trader.config import (
    CHAIN_ENTRY_PRIORITY,
    DEFAULT_ENTRY_CHAINS,
    DEFAULT_SCAN_CHAINS,
    DEFAULT_RPC,
    SUPPORTED_CHAINS,
    Settings,
    parse_entry_chains,
    parse_scan_chains,
)


def test_varsayilan_mod_paper():
    s = Settings.from_env()
    assert s.mode == "paper"
    assert s.validate() == []


def test_eth_ana_ag_yok():
    assert "ethereum" not in SUPPORTED_CHAINS
    assert set(SUPPORTED_CHAINS) == set(DEFAULT_RPC)


def test_scan_chains_sol_only_default():
    assert DEFAULT_SCAN_CHAINS == ("solana",)
    assert DEFAULT_ENTRY_CHAINS == ("solana",)
    assert parse_scan_chains("solana,base") == ("solana",)  # SOLANA_ONLY sabit: EVM override yok sayılır
    assert parse_entry_chains("solana") == ("solana",)
    assert CHAIN_ENTRY_PRIORITY["solana"] < CHAIN_ENTRY_PRIORITY["arbitrum"]
    s = Settings.from_env()
    assert "bsc" not in s.scan_chains


def test_entry_allowed():
    s = Settings(entry_chains=("solana",))
    assert s.entry_allowed("solana")
    assert not s.entry_allowed("arbitrum")


def test_phantom_trading_live_validate_ok():
    s = Settings(mode="live", phantom_trading=True)
    assert s.validate() == []


def test_sol_server_signing_enabled():
    s = Settings(solana_private_key="k", phantom_trading=False)
    assert s.sol_server_signing_enabled()
    s2 = Settings(solana_private_key="k", phantom_trading=True)
    assert not s2.sol_server_signing_enabled()
