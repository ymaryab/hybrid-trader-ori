"""CLI: python -m hibrit_trader [status|scan|run]"""

from __future__ import annotations

import argparse
import logging
import time

import httpx

from hibrit_trader.config import API, SUPPORTED_CHAINS, Settings


def cmd_status() -> None:
    s = Settings.from_env()
    print(f"hibrit-trader — mod: {s.mode}")
    print(f"Tarama: {', '.join(s.scan_chains)} · giriş: {', '.join(s.entry_chains)}")
    print(f"Desteklenen RPC: {', '.join(SUPPORTED_CHAINS)} (ETH ana ağ yok)")
    for chain in SUPPORTED_CHAINS:
        print(f"  RPC {chain:9s} {s.rpc[chain]}")
    print(f"API'ler: {', '.join(API)}")
    print(f"Limitler: pozisyon ${s.max_position_usd} · max açık {s.max_open_positions} · günlük zarar ${s.daily_loss_limit_usd} · slippage {s.max_slippage_bps}bps")
    if s.mode == "paper":
        print(f"Paper başlangıç: ${s.paper_start_balance_usd:.2f}")
    if s.phantom_trading:
        print("İşlem: Phantom panel (SOL paritesi) — SOLANA_PRIVATE_KEY devre dışı")
    elif s.sol_server_signing_enabled():
        print("İşlem: sunucu SOLANA_PRIVATE_KEY imzası")
    if s.alpha_on_chain():
        if s.helius_api_key:
            print(f"Helius alpha: {len(s.alpha_wallets)} cüzdan on-chain (Sol)")
        else:
            print(f"Alpha RPC fallback: {len(s.alpha_wallets)} cüzdan (public Solana RPC)")
    elif s.alpha_wallets:
        print(f"Alpha cüzdan: {len(s.alpha_wallets)} tanımlı — proxy mod (RPC fallback kapalı)")
        print("  → ALPHA_RPC_FALLBACK=1 (varsayılan) veya ücretli Helius key: setup-helius-alpha.sh")
    else:
        print("Alpha cüzdan: yok — config/alpha_wallets.txt veya ALPHA_WALLETS")
    sorunlar = s.validate()
    if sorunlar:
        print("Uyarılar:")
        for sorun in sorunlar:
            print(f"  - {sorun}")
    else:
        print("Config hazır ✓")


def cmd_scan(limit: int) -> None:
    from hibrit_trader.safety import check_token
    from hibrit_trader.scanner import scan_all
    from hibrit_trader.score import rank

    s = Settings.from_env()
    print(f"Tarama: GeckoTerminal + Dexscreener ({', '.join(s.scan_chains)})...")
    pairs = scan_all(s.scan_chains)
    print(f"  {len(pairs)} havuz bulundu")
    siralama = rank(pairs, s.max_position_usd)[:limit]
    if not siralama:
        print("Skor eşiğini geçen havuz yok.")
        return

    print(f"\nTop {len(siralama)} — güvenlik kontrolü (GoPlus)...\n")
    print(f"{'SKOR':>5}  {'AĞ':9} {'ÇİFT':24} {'LİK $':>10} {'H1%':>7} {'GÜVENLİK'}")
    with httpx.Client() as client:
        for i, (skor, p) in enumerate(siralama):
            if i:
                time.sleep(1.5)  # GoPlus ücretsiz katman rate limit'i
            rapor = check_token(client, p.chain, p.token_address)
            durum = "TEMİZ ✓" if rapor.ok else "RED: " + ", ".join(rapor.reasons[:3])
            print(f"{skor:5.1f}  {p.chain:9} {p.name[:24]:24} {p.liquidity_usd:>10,.0f} {p.chg_h1:>6.1f}% {durum}")
    print("\nNot: Bu liste izleme amaçlı — işlem yok (Faz 1).")


def cmd_reset_paper(balance: float) -> None:
    from hibrit_trader.paper import reset_paper_state

    reset_paper_state(balance)
    print(f"Paper cüzdan sıfırlandı: ${balance:.2f}")
    print("Panel/motor çalışıyorsa yeniden başlat (Ctrl+C → python -m hibrit_trader run)")


def cmd_run() -> None:
    s = Settings.from_env()
    sorunlar = s.validate()
    if s.mode == "live":
        if sorunlar:
            print("Live mod hataları:")
            for sorun in sorunlar:
                print(f"  - {sorun}")
            return
        print("⚠️  CANLI MOD — gerçek para (Solana/Jupiter). Max pozisyon: $%.0f" % s.max_position_usd)
    import uvicorn
    print("Motor + panel başlıyor → http://127.0.0.1:8643")
    uvicorn.run("hibrit_trader.panel:app", host="127.0.0.1", port=8643)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="hibrit_trader")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="config durumu")
    p_scan = sub.add_parser("scan", help="fırsat taraması (işlemsiz)")
    p_scan.add_argument("--limit", type=int, default=10, help="listelenecek havuz sayısı")
    sub.add_parser("run", help="paper motor + panel")
    p_reset = sub.add_parser("reset-paper", help="paper cüzdanı sıfırla")
    p_reset.add_argument("--balance", type=float, default=100.0, help="başlangıç USD")
    args = parser.parse_args()

    if args.cmd == "scan":
        cmd_scan(args.limit)
    elif args.cmd == "run":
        cmd_run()
    elif args.cmd == "reset-paper":
        cmd_reset_paper(args.balance)
    else:
        cmd_status()


if __name__ == "__main__":
    main()
