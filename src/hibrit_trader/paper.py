"""Paper trading broker — sanal dolum, slippage + gas maliyeti dahil."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from hibrit_trader.config import GAS_COST_USD
from hibrit_trader.scanner import Pair


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Position:
    pair_name: str
    chain: str
    token_address: str
    pool_address: str
    entry_price: float
    amount_token: float
    cost_usd: float
    opened_at: str
    entry_score: float
    amount_raw: int = 0  # canlı mod: Jupiter outAmount (en küçük birim)
    peak_price_usd: float = 0.0
    breakeven_armed: bool = False
    tp1_done: bool = False
    tp2_done: bool = False
    trail_armed: bool = False
    runner_mode: bool = False
    boost500_partial_done: bool = False
    initial_amount_token: float = 0.0
    opened_ts: float = 0.0
    atr_pct_at_entry: float = 0.0
    # ---- Telemetri (giriş anı damgası + döngüde işlenen tepe/dip) ----
    trade_id: str = ""
    discovery_source: str = ""
    entry_regime: str = ""
    entry_fear_greed: int | None = None
    entry_macro_avg: float | None = None
    px_decision: float = 0.0          # "al" kararı anındaki fiyat
    decision_to_entry_sec: float = 0.0
    entry_drift_pct: float = 0.0      # (fill/px_decision - 1) * 100
    liq_entry: float = 0.0
    liq_min: float = 0.0              # tutuş boyunca görülen min likidite
    mfe_pct: float = 0.0              # max favorable excursion (tepe kâr %)
    mae_pct: float = 0.0              # max adverse excursion (dip zarar %)
    mfe_at_sec: float = 0.0           # MFE'ye ulaşıldığı tutuş süresi (sn)
    mae_at_sec: float = 0.0
    # ---- Runner zaman-profili (saf gözlem; trading kararını etkilemez) ----
    obs_peak_price: float = 0.0       # tutuş boyunca görülen en yüksek fiyat
    obs_peak_ts_ms: int = 0           # tepe epoch ms (time-to-peak = peak_ts - entry_ts)
    obs_trough_price: float = 0.0     # en düşük fiyat
    obs_trough_ts_ms: int = 0         # dip epoch ms
    early_ticks: list = field(default_factory=list)  # ilk ~5 tick: [[saniye, fiyat], ...]


@dataclass
class Trade:
    pair_name: str
    chain: str
    entry_price: float
    exit_price: float
    cost_usd: float
    proceeds_usd: float
    pnl_usd: float
    opened_at: str
    closed_at: str
    exit_reason: str
    # ---- Telemetri (Position'dan kopyalanır — attribution ile join için) ----
    trade_id: str = ""
    token_address: str = ""
    source: str = ""
    regime: str = ""
    fear_greed: int | None = None
    entry_score: float = 0.0
    pnl_pct: float = 0.0
    hold_sec: float = 0.0
    liq_entry: float = 0.0
    liq_min: float = 0.0
    liq_exit: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    mfe_at_sec: float = 0.0
    mae_at_sec: float = 0.0
    px_decision: float = 0.0
    dec_to_entry_sec: float = 0.0
    entry_drift: float = 0.0
    buyers: int | None = None
    sellers: int | None = None


def new_trade_id(pool_address: str, opened_ts: float) -> str:
    """Pozisyon başına kararlı id — attribution/decisions/trades join anahtarı."""
    return f"{(pool_address or 'x')[:10]}-{int(opened_ts)}"


def enrich_trade_from_position(trade: "Trade", pos: "Position", *, liq_exit: float = 0.0) -> "Trade":
    """Trade'e Position giriş-anı damgasını + tutuş metriklerini kopyalar (tüm modlar)."""
    trade.trade_id = pos.trade_id
    trade.token_address = pos.token_address
    trade.source = pos.discovery_source
    trade.regime = pos.entry_regime
    trade.fear_greed = pos.entry_fear_greed
    trade.entry_score = pos.entry_score
    trade.hold_sec = round(max(0.0, time.time() - pos.opened_ts), 1) if pos.opened_ts else 0.0
    trade.liq_entry = round(pos.liq_entry, 2)
    trade.liq_min = round(pos.liq_min or pos.liq_entry, 2)
    trade.liq_exit = round(liq_exit, 2)
    trade.mfe_pct = round(pos.mfe_pct, 3)
    trade.mae_pct = round(pos.mae_pct, 3)
    trade.mfe_at_sec = round(pos.mfe_at_sec, 1)
    trade.mae_at_sec = round(pos.mae_at_sec, 1)
    trade.px_decision = pos.px_decision
    trade.dec_to_entry_sec = round(pos.decision_to_entry_sec, 3)
    trade.entry_drift = round(pos.entry_drift_pct, 3)
    if trade.cost_usd:
        trade.pnl_pct = round(100 * trade.pnl_usd / max(trade.cost_usd, 0.01), 3)
    return trade


def _slippage(usd: float, liquidity_usd: float) -> float:
    return min(usd / max(liquidity_usd, 1.0), 0.05)


class PaperBroker:
    def __init__(
        self,
        state_path: str = "data/paper_state.json",
        trades_path: str = "data/trades.jsonl",
        start_balance_usd: float = 1000.0,
    ) -> None:
        self.state_path = Path(state_path)
        self.trades_path = Path(trades_path)
        self.start_balance_usd = start_balance_usd
        self.balance: float = start_balance_usd
        self.positions: list[Position] = []
        self.realized_pnl: float = 0.0
        self.trades: list[Trade] = []
        self._start_balance_persisted: float = start_balance_usd
        self.phantom_pubkey: str | None = None
        self._load()

    def _buy_event_chains(self) -> list[str]:
        """Her pozisyon açılışı için ağ (kısmi satışlar tek alım sayılır)."""
        seen: set[str] = set()
        chains: list[str] = []
        for t in self.trades:
            if t.opened_at in seen:
                continue
            seen.add(t.opened_at)
            chains.append(t.chain)
        for p in self.positions:
            if p.opened_at in seen:
                continue
            seen.add(p.opened_at)
            chains.append(p.chain)
        return chains

    def estimated_buy_gas(self) -> float:
        return sum(GAS_COST_USD.get(c, 0.1) for c in self._buy_event_chains())

    def _infer_legacy_start_balance(self) -> float:
        """Eski state dosyalarında start_balance yoksa gas dahil geri hesapla."""
        return self.balance - self.realized_pnl + self.estimated_buy_gas()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text())
        self.balance = float(data.get("balance", self.start_balance_usd))
        self.realized_pnl = float(data.get("realized_pnl", 0.0))
        self.positions = [Position(**p) for p in data.get("positions", [])]
        if self.trades_path.exists():
            self.trades = [
                Trade(**json.loads(line))
                for line in self.trades_path.read_text().splitlines()
                if line.strip()
            ]
        if "start_balance" in data:
            self._start_balance_persisted = float(data["start_balance"])
        else:
            self._start_balance_persisted = self._infer_legacy_start_balance()
        pk = data.get("phantom_pubkey")
        self.phantom_pubkey = str(pk).strip() if pk else None

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {
                    "balance": self.balance,
                    "realized_pnl": self.realized_pnl,
                    "start_balance": round(self._start_balance_persisted, 2),
                    "phantom_pubkey": self.phantom_pubkey,
                    "positions": [asdict(p) for p in self.positions],
                },
                indent=2,
            )
        )

    def _append_trade(self, trade: Trade) -> None:
        self.trades.append(trade)
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trades_path.open("a") as f:
            f.write(json.dumps(asdict(trade)) + "\n")

    def buy(self, pair: Pair, usd: float, score: float) -> Position:
        gas = GAS_COST_USD.get(pair.chain, 0.1)
        total = usd + gas
        if self.balance < total:
            raise ValueError(f"Bakiye yetersiz: ${self.balance:.2f} < ${total:.2f}")

        slip = _slippage(usd, pair.liquidity_usd)
        effective_price = pair.price_usd * (1 + slip)
        amount = usd / effective_price

        pos = Position(
            pair_name=pair.name,
            chain=pair.chain,
            token_address=pair.token_address,
            pool_address=pair.pool_address,
            entry_price=effective_price,
            amount_token=amount,
            cost_usd=usd,
            opened_at=_now_iso(),
            entry_score=score,
            peak_price_usd=effective_price,
            initial_amount_token=amount,
            opened_ts=time.time(),
            discovery_source=pair.discovery_source or "geckoterminal",
            liq_entry=pair.liquidity_usd,
            liq_min=pair.liquidity_usd,
        )
        pos.trade_id = new_trade_id(pos.pool_address, pos.opened_ts)
        self.balance -= total
        self.positions.append(pos)
        self._save()
        return pos

    def _sell_amount(
        self,
        pos: Position,
        sell_tokens: float,
        sell_cost_basis: float,
        current_price: float,
        liquidity_usd: float,
        reason: str,
        *,
        close_position: bool,
    ) -> Trade:
        slip = _slippage(sell_cost_basis, liquidity_usd)
        effective_price = current_price * (1 - slip)
        gross = sell_tokens * effective_price
        gas = GAS_COST_USD.get(pos.chain, 0.1)
        proceeds = gross - gas
        pnl = proceeds - sell_cost_basis

        trade = Trade(
            pair_name=pos.pair_name,
            chain=pos.chain,
            entry_price=pos.entry_price,
            exit_price=effective_price,
            cost_usd=round(sell_cost_basis, 4),
            proceeds_usd=round(proceeds, 4),
            pnl_usd=round(pnl, 4),
            opened_at=pos.opened_at,
            closed_at=_now_iso(),
            exit_reason=reason,
        )
        enrich_trade_from_position(trade, pos, liq_exit=liquidity_usd)
        self.balance += proceeds
        self.realized_pnl += pnl
        if close_position:
            self.positions = [p for p in self.positions if p.pool_address != pos.pool_address]
        else:
            pos.amount_token = max(0.0, pos.amount_token - sell_tokens)
            pos.cost_usd = max(0.0, pos.cost_usd - sell_cost_basis)
        self._append_trade(trade)
        self._save()
        return trade

    def sell(self, pos: Position, current_price: float, liquidity_usd: float, reason: str) -> Trade:
        return self._sell_amount(
            pos,
            pos.amount_token,
            pos.cost_usd,
            current_price,
            liquidity_usd,
            reason,
            close_position=True,
        )

    def sell_partial(
        self,
        pos: Position,
        fraction: float,
        current_price: float,
        liquidity_usd: float,
        reason: str,
    ) -> Trade:
        fraction = max(0.0, min(1.0, fraction))
        if fraction >= 0.999:
            return self.sell(pos, current_price, liquidity_usd, reason)
        sell_tokens = pos.amount_token * fraction
        sell_cost = pos.cost_usd * fraction
        return self._sell_amount(
            pos,
            sell_tokens,
            sell_cost,
            current_price,
            liquidity_usd,
            reason,
            close_position=False,
        )

    @staticmethod
    def unrealized_pnl(pos: Position, current_price: float) -> float:
        return pos.amount_token * current_price - pos.cost_usd

    def apply_wallet_balance(self, usdc_usd: float, *, pubkey: str | None = None) -> float:
        """Phantom SOL bakiyesini (USD karşılığı) paper sermayeye yansıt."""
        if pubkey:
            self.phantom_pubkey = pubkey.strip() or None
        locked = sum(p.cost_usd for p in self.positions)
        deployable = max(0.0, float(usdc_usd) - locked)
        self.balance = round(deployable, 2)
        if not self.positions and not self.trades:
            self._start_balance_persisted = round(float(usdc_usd), 2)
            self.start_balance_usd = self._start_balance_persisted
        self._save()
        return self.balance

    def summary(self) -> dict:
        wins = sum(1 for t in self.trades if t.pnl_usd > 0)
        losses = sum(1 for t in self.trades if t.pnl_usd <= 0)
        total = len(self.trades)
        gross_profit = sum(t.pnl_usd for t in self.trades if t.pnl_usd > 0)
        gross_loss = sum(t.pnl_usd for t in self.trades if t.pnl_usd <= 0)
        start = round(self._start_balance_persisted, 2)
        cash_equity = self.balance + sum(p.cost_usd for p in self.positions)
        session_pnl = round(cash_equity - start, 2)
        return {
            "balance": round(self.balance, 2),
            "start_balance_usd": start,
            "equity": round(cash_equity, 2),
            "session_pnl": session_pnl,
            "session_pnl_pct": round(session_pnl / start * 100, 2) if start else 0.0,
            "open_positions": len(self.positions),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": 0.0,
            "gas_paid_est": round(self.estimated_buy_gas(), 2),
            "trade_count": total,
            "wins": wins,
            "losses": losses,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "win_rate": round(wins / total * 100, 1) if total else 0.0,
        }


def reset_paper_state(
    balance: float,
    state_path: str = "data/paper_state.json",
    trades_path: str = "data/trades.jsonl",
) -> None:
    """Paper cüzdanı sıfırla — yeni test oturumu."""
    sp = Path(state_path)
    tp = Path(trades_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        json.dumps(
            {
                "balance": balance,
                "realized_pnl": 0.0,
                "start_balance": round(balance, 2),
                "positions": [],
            },
            indent=2,
        )
    )
    tp.write_text("")
