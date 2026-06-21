"""Ortam + ağ yapılandırması. Tüm varsayılan API/RPC'ler ücretsiz katman."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local", override=True)

# Ücretsiz public RPC — SOLANA_RPC_URL / SOLANA_RPC_FALLBACK_URLS ile override (Helius artık ücretli)
DEFAULT_RPC = {
    "solana": "https://api.mainnet-beta.solana.com",
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "bsc": "https://bsc-dataseed.binance.org",
}

# Ücretsiz veri/yürütme API'leri (key gerekmez ya da ücretsiz katman)
API = {
    "dexscreener": "https://api.dexscreener.com",
    "geckoterminal": "https://api.geckoterminal.com/api/v2",
    "jupiter_quote": "https://lite-api.jup.ag/swap/v1",
    "goplus": "https://api.gopluslabs.io/api/v1",
}

# ETH ana ağ bilinçli olarak YOK (gas maliyeti — kullanıcı kararı)
SUPPORTED_CHAINS = ("solana", "base", "arbitrum", "bsc")

# Tarama sırası — varsayılan yalnız Sol (EVM slot yok)
DEFAULT_SCAN_CHAINS = ("solana",)

# Giriş yapılabilecek ağlar — EVM pozisyon açılmaz
DEFAULT_ENTRY_CHAINS = ("solana",)

# Giriş / watchlist önceliği — düşük = önce dene
CHAIN_ENTRY_PRIORITY = {
    "solana": 0,
    "arbitrum": 1,
    "base": 2,
    "bsc": 99,
}


def solana_only_enabled() -> bool:
    """Piyasa verisi yalnız Solana ağından çekilsin. Geri alınabilir: SOLANA_ONLY=0."""
    return os.getenv("SOLANA_ONLY", "1") != "0"


def restrict_chains(chains: tuple[str, ...]) -> tuple[str, ...]:
    """Merkezi kısıt: SOLANA_ONLY açıkken her zaman ('solana',), kapalıyken gelen chains."""
    if solana_only_enabled():
        return ("solana",)
    return tuple(chains)


def parse_scan_chains(raw: str | None = None) -> tuple[str, ...]:
    """SCAN_CHAINS env — SOLANA_ONLY açıkken sabit yalnız solana (override yok sayılır)."""
    if raw is None:
        raw = os.getenv("SCAN_CHAINS", ",".join(DEFAULT_SCAN_CHAINS))
    chains = tuple(c.strip().lower() for c in raw.split(",") if c.strip())
    return restrict_chains(chains if chains else DEFAULT_SCAN_CHAINS)


def parse_entry_chains(raw: str | None = None) -> tuple[str, ...]:
    """ENTRY_CHAINS env — SOLANA_ONLY açıkken sabit yalnız solana (override yok sayılır)."""
    if raw is None:
        raw = os.getenv("ENTRY_CHAINS", ",".join(DEFAULT_ENTRY_CHAINS))
    chains = tuple(c.strip().lower() for c in raw.split(",") if c.strip())
    return restrict_chains(chains if chains else DEFAULT_ENTRY_CHAINS)

# Yaklaşık swap başına gas maliyeti (USD) — fırsat skorunda net getiriye dahil
GAS_COST_USD = {
    "solana": 0.002,
    "base": 0.03,
    "arbitrum": 0.05,
    "bsc": 0.15,
}

# GoPlus chain id eşlemesi (EVM); Solana'nın ayrı endpoint'i var
GOPLUS_EVM_CHAIN_ID = {
    "base": "8453",
    "arbitrum": "42161",
    "bsc": "56",
}


@dataclass
class Settings:
    mode: str = "paper"  # paper | live
    solana_private_key: str = ""
    evm_private_key: str = ""
    watch_evm_address: str = ""
    watch_solana_address: str = ""
    rpc: dict[str, str] = field(default_factory=dict)
    max_position_usd: float = 20.0
    daily_loss_limit_usd: float = 30.0
    paper_start_balance_usd: float = 1000.0
    entry_score_min: float = 55.0
    min_edge_after_cost_pct: float = 4.0
    confluence_min: float = 58.0
    confluence_min_layers: int = 2
    confluence_required: bool = True
    paper_aggressive: bool = True
    max_slippage_bps: int = 100
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    zero_x_api_key: str = ""
    paper_live_quotes: bool = True
    scan_chains: tuple[str, ...] = field(default_factory=lambda: DEFAULT_SCAN_CHAINS)
    entry_chains: tuple[str, ...] = field(default_factory=lambda: DEFAULT_ENTRY_CHAINS)
    max_open_positions: int = 5
    capital_deploy_pct: float = 0.92
    helius_api_key: str = ""
    alpha_wallets: tuple[str, ...] = field(default_factory=tuple)
    phantom_trading: bool = True  # 1 = Phantom panel; SOLANA_PRIVATE_KEY yok sayılır

    @classmethod
    def from_env(cls) -> "Settings":
        from hibrit_trader.alpha_config import load_alpha_wallet_addresses, resolve_helius_api_key

        rpc = {
            chain: os.getenv(f"{chain.upper()}_RPC_URL") or DEFAULT_RPC[chain]
            for chain in SUPPORTED_CHAINS
        }
        return cls(
            mode=os.getenv("BOT_MODE", "paper").lower(),
            solana_private_key=os.getenv("SOLANA_PRIVATE_KEY", ""),
            evm_private_key=os.getenv("EVM_PRIVATE_KEY", ""),
            watch_evm_address=os.getenv("WATCH_EVM_ADDRESS", ""),
            watch_solana_address=os.getenv("WATCH_SOLANA_ADDRESS", ""),
            rpc=rpc,
            max_position_usd=float(os.getenv("MAX_POSITION_USD", "20")),
            daily_loss_limit_usd=float(os.getenv("DAILY_LOSS_LIMIT_USD", "30")),
            paper_start_balance_usd=float(os.getenv("PAPER_START_BALANCE_USD", "1000")),
            entry_score_min=float(os.getenv("ENTRY_SCORE_MIN", "55")),
            min_edge_after_cost_pct=float(os.getenv("MIN_EDGE_AFTER_COST_PCT", "4")),
            confluence_min=float(
                os.getenv(
                    "CONFLUENCE_MIN",
                    "52" if os.getenv("PAPER_AGGRESSIVE", "1") != "0" else "58",
                )
            ),
            confluence_min_layers=int(os.getenv("CONFLUENCE_MIN_LAYERS", "2")),
            confluence_required=os.getenv("CONFLUENCE_REQUIRED", "1") != "0",
            paper_aggressive=os.getenv("PAPER_AGGRESSIVE", "1") != "0",
            max_slippage_bps=int(os.getenv("MAX_SLIPPAGE_BPS", "100")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            zero_x_api_key=os.getenv("ZEROX_API_KEY", ""),
            paper_live_quotes=os.getenv("PAPER_LIVE_QUOTES", "1") == "1",
            scan_chains=parse_scan_chains(),
            entry_chains=parse_entry_chains(),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "5")),
            capital_deploy_pct=float(os.getenv("CAPITAL_DEPLOY_PCT", "0.92")),
            helius_api_key=resolve_helius_api_key(),
            alpha_wallets=tuple(load_alpha_wallet_addresses()),
            phantom_trading=os.getenv("PHANTOM_TRADING", "1") != "0",
        )

    def sol_server_signing_enabled(self) -> bool:
        """Sunucuda SOLANA_PRIVATE_KEY ile imza — PHANTOM_TRADING=0 gerekir."""
        return bool(self.solana_private_key) and not self.phantom_trading

    def live_chains(self) -> list[str]:
        """Canlı modda işlem yapılabilecek ağlar (key'e göre)."""
        chains: list[str] = []
        if self.sol_server_signing_enabled():
            chains.append("solana")
        if self.evm_private_key and self.zero_x_api_key:
            chains.extend(["base", "arbitrum", "bsc"])
        elif self.evm_private_key:
            pass  # EVM key var ama 0x key yok — EVM devre dışı
        return chains

    def validate(self) -> list[str]:
        """Eksik/riskli ayarları döndürür; boş liste = hazır."""
        sorunlar: list[str] = []
        if self.mode not in ("paper", "live"):
            sorunlar.append(f"BOT_MODE geçersiz: {self.mode!r} (paper|live)")
        if self.mode == "live":
            # Sunucu key yoksa Phantom panel imzası ile canlı (Sol-only)
            if self.evm_private_key and not self.zero_x_api_key:
                sorunlar.append("EVM canlı için ZEROX_API_KEY gerekli (ücretsiz: 0x.org)")
            if self.max_position_usd > 100:
                sorunlar.append("MAX_POSITION_USD > 100 — ilk canlı için $50-100 toplam sermaye kuralı")
        if self.alpha_wallets and not self.helius_api_key:
            if os.getenv("ALPHA_RPC_FALLBACK", "1") == "0":
                sorunlar.append(
                    "ALPHA_WALLETS tanımlı — HELIUS_API_KEY yok ve ALPHA_RPC_FALLBACK=0 (proxy only)"
                )
        return sorunlar

    def alpha_on_chain(self) -> bool:
        if not self.alpha_wallets:
            return False
        if self.helius_api_key:
            return True
        return os.getenv("ALPHA_RPC_FALLBACK", "1") != "0"

    def entry_allowed(self, chain: str) -> bool:
        return chain.lower() in self.entry_chains
