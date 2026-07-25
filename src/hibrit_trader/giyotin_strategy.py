"""Giyotin v4.2 — Dual-Profile Hunter (Solana DEX momentum avcısı).

Tier 1 eliminasyon → Profil A (erken) veya B (canlı chase) → bonuslu Billy (min 79) → giriş.
Zehirli chase: yüksek 5M + zayıf 1H/24H → veto. v3 kazananları (BILLY/FROGBULL) korunur.
Çıkış: failsafe → guillotine → hard stop → trail (MFE≥5%) → kademeli partial+runner (paper) → break-even.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from hibrit_trader.giyotin_btc_macro import btc_macro_blocks_entry, fetch_btc_m15_pct
from hibrit_trader.paper import Position
from hibrit_trader.scanner import Pair

def _state_file() -> Path:
    """Env her çağrıda okunur — testler GIYOTIN_STATE_FILE'ı import sonrası set eder."""
    return Path(os.getenv("GIYOTIN_STATE_FILE", "data/giyotin_state.json"))
_LOCK = threading.Lock()
_ENGINE = "giyotin_v45"
_VERSION = 4

MIN_ROUND_TRIP_FRICTION_PCT = float(os.getenv("GIYOTIN_MIN_FRICTION_PCT", "0.25"))
ExitTag = Literal["failsafe", "guillotine", "hard_stop", "break_even", "trailing"]


class GiyotinState(str, Enum):
    IDLE = "IDLE"
    ENTRY_PENDING = "ENTRY_PENDING"
    POSITION_OPEN = "POSITION_OPEN"
    BREAK_EVEN = "BREAK_EVEN"
    TRAILING = "TRAILING"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"


_VALID_TRANSITIONS: dict[str, set[str]] = {
    "": {"ENTRY_PENDING", "POSITION_OPEN"},
    "IDLE": {"ENTRY_PENDING"},
    "ENTRY_PENDING": {"POSITION_OPEN", "IDLE"},
    "POSITION_OPEN": {"BREAK_EVEN", "TRAILING", "EXIT_PENDING", "CLOSED"},
    "BREAK_EVEN": {"TRAILING", "EXIT_PENDING"},
    "TRAILING": {"EXIT_PENDING", "CLOSED"},
    "EXIT_PENDING": {"CLOSED"},
    "CLOSED": set(),
}


def giyotin_mode_enabled() -> bool:
    return os.getenv("GIYOTIN_MODE", "0") != "0"


def _f(env: str, default: str) -> float:
    return float(os.getenv(env, default))


def _i(env: str, default: str) -> int:
    return int(os.getenv(env, default))


# ---- v4.2 thresholds (env) ----

def giyotin_mcap_min() -> float:
    return _f("GIYOTIN_MCAP_MIN", "10000")


def giyotin_age_min_sec() -> float:
    return _f("GIYOTIN_AGE_MIN_SEC", "1800")


def giyotin_holders_min() -> int:
    return _i("GIYOTIN_HOLDERS_MIN", "3")


def giyotin_liq_min() -> float:
    return _f("GIYOTIN_LIQ_MIN", "50000")


def giyotin_mature_liq_min() -> float:
    """S3 — mature şerit liq tabanı; GIYOTIN_MATURE_LIQ_MIN=80000 paper optimizasyonu."""
    raw = os.getenv("GIYOTIN_MATURE_LIQ_MIN", "").strip()
    if raw:
        return float(raw)
    return giyotin_liq_min()


def giyotin_entry_score_min() -> float:
    """Billy fit score tabanı — trade entry_score ile aynı ölçek."""
    return _f("GIYOTIN_ENTRY_SCORE_MIN", "50")


def giyotin_genesis_lane_enabled() -> bool:
    return os.getenv("GIYOTIN_GENESIS_LANE", "0") != "0"


def giyotin_engine_label() -> str:
    """Panel / istatistik etiketi — _ENGINE ile senkron."""
    labels = {
        "giyotin_v45": "v4.5",
        "giyotin_v44": "v4.4",
        "giyotin_v43": "v4.3",
        "giyotin_v42": "v4.2",
        "giyotin_v41": "v4.1",
        "giyotin_v40": "v4.0",
    }
    return labels.get(_ENGINE, _ENGINE.replace("giyotin_", ""))


def giyotin_engine_subtitle() -> str:
    gen = giyotin_genesis_lane_enabled()
    br = giyotin_bridge_lane_enabled()
    p2 = giyotin_phase2_lane_enabled()
    dc = giyotin_dead_cat_lane_enabled()
    gap = giyotin_gap_lane_enabled()
    rev = giyotin_revival_lane_enabled()
    rs = giyotin_runner_scout_lane_enabled()
    am = giyotin_alpha_mimic_lane_enabled()
    w2 = giyotin_wallet2_lane_enabled()
    parts: list[str] = []
    if gen:
        parts.append("GENESIS")
    if rev:
        parts.append("REVIVAL")
    if rs:
        parts.append("RUNNER_SCOUT")
    if am:
        parts.append("ALPHA_MIMIC")
    if br:
        parts.append("BRIDGE")
    if gap:
        parts.append("GAP")
    if p2:
        parts.append("FAZ2")
    if dc:
        parts.append("DEAD_CAT")
    if w2:
        parts.append("WALLET2")
    if parts:
        return " + ".join(parts) + " + MATURE"
    return "DUAL-PROFILE HUNTER"


def giyotin_genesis_age_min_sec() -> float:
    return _f("GIYOTIN_GENESIS_AGE_MIN_SEC", "120")


def giyotin_genesis_age_max_sec() -> float:
    return _f("GIYOTIN_GENESIS_AGE_MAX_SEC", "1680")


def giyotin_genesis_liq_min() -> float:
    return _f("GIYOTIN_GENESIS_LIQ_MIN", "15000")


def giyotin_genesis_score_min() -> float:
    return _f("GIYOTIN_GENESIS_SCORE_MIN", "70")


def giyotin_genesis_turn_max() -> float:
    return _f("GIYOTIN_GENESIS_TURN_MAX", "40")


def giyotin_genesis_max_open() -> int:
    return _i("GIYOTIN_GENESIS_MAX_OPEN", "2")


def giyotin_genesis_holders_min(*, pump_mint: bool) -> int:
    if pump_mint:
        return _i("GIYOTIN_GENESIS_HOLDERS_MIN_PUMP", "0")
    return _i("GIYOTIN_GENESIS_HOLDERS_MIN", "3")


def is_giyotin_genesis_pair(pair: Pair) -> bool:
    from hibrit_trader.pump_fun_feed import is_pump_fun_mint

    token = pair.token_address or ""
    if is_pump_fun_mint(token):
        return True
    if getattr(pair, "discovery_source", "") == "pump_fun":
        return True
    return False


def is_giyotin_genesis_position(pos: Position) -> bool:
    regime = (getattr(pos, "entry_regime", "") or "").lower()
    return regime == "giyotin_genesis" or regime.endswith("_genesis")


def count_giyotin_genesis_open(positions: list[Position]) -> int:
    return sum(1 for p in positions if is_giyotin_genesis_position(p))


def giyotin_bridge_lane_enabled() -> bool:
    return os.getenv("GIYOTIN_BRIDGE_LANE", "0") != "0"


def giyotin_phase2_lane_enabled() -> bool:
    """FAZ2 ikinci dalga şeridi — varsayılan paper açık."""
    flag = os.getenv("GIYOTIN_PHASE2_LANE", "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    return os.getenv("BOT_MODE", "paper") == "paper"


def giyotin_phase2_age_min_sec() -> float:
    return _f("GIYOTIN_PHASE2_AGE_MIN_SEC", "2700")


def giyotin_phase2_age_max_sec() -> float:
    return _f("GIYOTIN_PHASE2_AGE_MAX_SEC", "18000")


def giyotin_phase2_liq_min() -> float:
    return _f("GIYOTIN_PHASE2_LIQ_MIN", "25000")


def giyotin_phase2_liq_max() -> float:
    return _f("GIYOTIN_PHASE2_LIQ_MAX", "80000")


def giyotin_phase2_h24_min() -> float:
    return _f("GIYOTIN_PHASE2_H24_MIN", "100")


def giyotin_phase2_h1_min() -> float:
    return _f("GIYOTIN_PHASE2_H1_MIN", "15")


def giyotin_phase2_h1_max() -> float:
    return _f("GIYOTIN_PHASE2_H1_MAX", "60")


def giyotin_phase2_m5_min() -> float:
    return _f("GIYOTIN_PHASE2_M5_MIN", "2")


def giyotin_phase2_m5_max() -> float:
    return _f("GIYOTIN_PHASE2_M5_MAX", "10")


def giyotin_phase2_pump_bl_decay_min() -> float:
    return _f("GIYOTIN_PHASE2_PUMP_BL_DECAY_MIN", "240")


def giyotin_phase2_max_open() -> int:
    return _i("GIYOTIN_PHASE2_MAX_OPEN", "2")


def giyotin_phase2_score_min() -> float:
    return _f("GIYOTIN_PHASE2_SCORE_MIN", "55")


def giyotin_runner_size_boost_enabled() -> bool:
    """S4 — yüksek Billy + liq runner pozisyon çarpanı; varsayılan paper açık."""
    flag = os.getenv("GIYOTIN_RUNNER_SIZE_BOOST", "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    return os.getenv("BOT_MODE", "paper") == "paper"


def giyotin_runner_size_boost_mult() -> float:
    return _f("GIYOTIN_RUNNER_SIZE_BOOST_MULT", "1.25")


def giyotin_runner_boost_liq_min() -> float:
    return _f("GIYOTIN_RUNNER_BOOST_LIQ_MIN", "150000")


def giyotin_runner_boost_billy_min() -> float:
    return _f("GIYOTIN_RUNNER_BOOST_BILLY_MIN", "90")


def runner_size_boost_scale(billy: float, liq: float, *, volatile: bool) -> float:
    """S4 — billy≥90, liq≥150k, volatil guard kapalı yol → ×1.25."""
    if not giyotin_runner_size_boost_enabled() or volatile:
        return 1.0
    if float(billy or 0) >= giyotin_runner_boost_billy_min() and float(liq or 0) >= giyotin_runner_boost_liq_min():
        return giyotin_runner_size_boost_mult()
    return 1.0


def giyotin_bridge_age_min_sec() -> float:
    """Alt sınır dahil değil — yaş > bu değer (varsayılan 28dk)."""
    return _f("GIYOTIN_BRIDGE_AGE_MIN_SEC", "1680")


def giyotin_bridge_age_max_sec() -> float:
    return _f("GIYOTIN_BRIDGE_AGE_MAX_SEC", "7200")


def giyotin_bridge_liq_min() -> float:
    return _f("GIYOTIN_BRIDGE_LIQ_MIN", "20000")


def giyotin_bridge_score_min() -> float:
    return _f("GIYOTIN_BRIDGE_SCORE_MIN", "55")


def giyotin_bridge_h1_min() -> float:
    return _f("GIYOTIN_BRIDGE_H1_MIN", "30")


def giyotin_bridge_h1_max() -> float:
    return _f("GIYOTIN_BRIDGE_H1_MAX", "100")


def giyotin_bridge_m5_min() -> float:
    return _f("GIYOTIN_BRIDGE_M5_MIN", "2")


def giyotin_bridge_m5_max() -> float:
    return _f("GIYOTIN_BRIDGE_M5_MAX", "8")


def giyotin_bridge_turn_max() -> float:
    return _f("GIYOTIN_BRIDGE_TURN_MAX", "40")


def giyotin_bridge_max_open() -> int:
    return _i("GIYOTIN_BRIDGE_MAX_OPEN", "1")


def giyotin_bridge_position_usd() -> float:
    return _f("GIYOTIN_BRIDGE_POSITION_USD", "50")


def is_giyotin_bridge_position(pos: Position) -> bool:
    regime = (getattr(pos, "entry_regime", "") or "").lower()
    return regime == "giyotin_bridge" or regime.endswith("_bridge")


def count_giyotin_bridge_open(positions: list[Position]) -> int:
    return sum(1 for p in positions if is_giyotin_bridge_position(p))


def is_giyotin_phase2_position(pos: Position) -> bool:
    regime = (getattr(pos, "entry_regime", "") or "").lower()
    return regime == "giyotin_phase2" or regime.endswith("_phase2")


def count_giyotin_phase2_open(positions: list[Position]) -> int:
    return sum(1 for p in positions if is_giyotin_phase2_position(p))


def giyotin_dead_cat_lane_enabled() -> bool:
    """Ölü kedi sıçraması — dump sonrası m5 toparlanma; GIYOTIN_DEAD_CAT_LANE=1."""
    return os.getenv("GIYOTIN_DEAD_CAT_LANE", "0") != "0"


def giyotin_dead_cat_age_min_sec() -> float:
    return _f("GIYOTIN_DEAD_CAT_AGE_MIN_SEC", "2700")


def giyotin_dead_cat_age_max_sec() -> float:
    return _f("GIYOTIN_DEAD_CAT_AGE_MAX_SEC", "21600")


def giyotin_dead_cat_liq_min() -> float:
    return _f("GIYOTIN_DEAD_CAT_LIQ_MIN", "15000")


def giyotin_dead_cat_liq_max() -> float:
    return _f("GIYOTIN_DEAD_CAT_LIQ_MAX", "70000")


def giyotin_dead_cat_h24_min() -> float:
    return _f("GIYOTIN_DEAD_CAT_H24_MIN", "80")


def giyotin_dead_cat_h1_max() -> float:
    """h1 bu değerin altında = dump fazı."""
    return _f("GIYOTIN_DEAD_CAT_H1_MAX", "-15")


def giyotin_dead_cat_h1_pump_bl_max() -> float:
    """pump_bl varken giriş için daha derin dump."""
    return _f("GIYOTIN_DEAD_CAT_H1_PUMP_BL_MAX", "-25")


def giyotin_dead_cat_m5_min() -> float:
    return _f("GIYOTIN_DEAD_CAT_M5_MIN", "2")


def giyotin_dead_cat_m5_max() -> float:
    return _f("GIYOTIN_DEAD_CAT_M5_MAX", "10")


def giyotin_dead_cat_m5_pump_bl_max() -> float:
    return _f("GIYOTIN_DEAD_CAT_M5_PUMP_BL_MAX", "8")


def giyotin_dead_cat_score_min() -> float:
    return _f("GIYOTIN_DEAD_CAT_SCORE_MIN", "50")


def giyotin_dead_cat_max_open() -> int:
    return _i("GIYOTIN_DEAD_CAT_MAX_OPEN", "1")


def giyotin_dead_cat_position_usd() -> float:
    return _f("GIYOTIN_DEAD_CAT_POSITION_USD", "35")


def giyotin_dead_cat_hard_stop_pct() -> float:
    return _f("GIYOTIN_DEAD_CAT_HARD_STOP_PCT", "-1.5")


def giyotin_dead_cat_trail_arm_mfe_pct() -> float:
    return _f("GIYOTIN_DEAD_CAT_TRAIL_ARM_MFE_PCT", "4.0")


def giyotin_dead_cat_trail_pct() -> float:
    return _f("GIYOTIN_DEAD_CAT_TRAIL_PCT", "0.8")


def is_giyotin_dead_cat_position(pos: Position) -> bool:
    regime = (getattr(pos, "entry_regime", "") or "").lower()
    return regime == "giyotin_dead_cat" or regime.endswith("_dead_cat")


def count_giyotin_dead_cat_open(positions: list[Position]) -> int:
    return sum(1 for p in positions if is_giyotin_dead_cat_position(p))


def giyotin_gap_lane_enabled() -> bool:
    """28–45dk geçiş boşluğu — köprü h1 kaçanı; GIYOTIN_GAP_LANE=1."""
    return os.getenv("GIYOTIN_GAP_LANE", "0") != "0"


def giyotin_gap_age_min_sec() -> float:
    return _f("GIYOTIN_GAP_AGE_MIN_SEC", "1680")


def giyotin_gap_age_max_sec() -> float:
    return _f("GIYOTIN_GAP_AGE_MAX_SEC", "2700")


def giyotin_gap_liq_min() -> float:
    return _f("GIYOTIN_GAP_LIQ_MIN", "18000")


def giyotin_gap_liq_max() -> float:
    return _f("GIYOTIN_GAP_LIQ_MAX", "55000")


def giyotin_gap_h24_min() -> float:
    return _f("GIYOTIN_GAP_H24_MIN", "40")


def giyotin_gap_h1_min() -> float:
    return _f("GIYOTIN_GAP_H1_MIN", "5")


def giyotin_gap_h1_max() -> float:
    return _f("GIYOTIN_GAP_H1_MAX", "75")


def giyotin_gap_m5_min() -> float:
    return _f("GIYOTIN_GAP_M5_MIN", "0")


def giyotin_gap_m5_max() -> float:
    return _f("GIYOTIN_GAP_M5_MAX", "10")


def giyotin_gap_score_min() -> float:
    return _f("GIYOTIN_GAP_SCORE_MIN", "55")


def giyotin_gap_max_open() -> int:
    return _i("GIYOTIN_GAP_MAX_OPEN", "1")


def giyotin_gap_position_usd() -> float:
    return _f("GIYOTIN_GAP_POSITION_USD", "40")


def is_giyotin_gap_position(pos: Position) -> bool:
    regime = (getattr(pos, "entry_regime", "") or "").lower()
    return regime == "giyotin_gap" or regime.endswith("_gap")


def count_giyotin_gap_open(positions: list[Position]) -> int:
    return sum(1 for p in positions if is_giyotin_gap_position(p))


def giyotin_wallet2_lane_enabled() -> bool:
    """2-cüzdan mature scalp — veto counterfactual EV; GIYOTIN_WALLET2_LANE=1."""
    return os.getenv("GIYOTIN_WALLET2_LANE", "0") != "0"


def giyotin_wallet2_age_min_sec() -> float:
    return _f("GIYOTIN_WALLET2_AGE_MIN_SEC", "1800")


def giyotin_wallet2_liq_min() -> float:
    return _f("GIYOTIN_WALLET2_LIQ_MIN", "25000")


def giyotin_wallet2_h1_min() -> float:
    return _f("GIYOTIN_WALLET2_H1_MIN", "10")


def giyotin_wallet2_h1_max() -> float:
    return _f("GIYOTIN_WALLET2_H1_MAX", "65")


def giyotin_wallet2_m5_min() -> float:
    return _f("GIYOTIN_WALLET2_M5_MIN", "2")


def giyotin_wallet2_m5_max() -> float:
    return _f("GIYOTIN_WALLET2_M5_MAX", "25")


def giyotin_wallet2_score_min() -> float:
    return _f("GIYOTIN_WALLET2_SCORE_MIN", "70")


def giyotin_wallet2_max_open() -> int:
    return _i("GIYOTIN_WALLET2_MAX_OPEN", "2")


def giyotin_wallet2_position_usd() -> float:
    return _f("GIYOTIN_WALLET2_POSITION_USD", "30")


def is_giyotin_wallet2_position(pos: Position) -> bool:
    regime = (getattr(pos, "entry_regime", "") or "").lower()
    return regime == "giyotin_wallet2" or regime.endswith("_wallet2")


def count_giyotin_wallet2_open(positions: list[Position]) -> int:
    return sum(1 for p in positions if is_giyotin_wallet2_position(p))


def giyotin_revival_lane_enabled() -> bool:
    """PFP mature momentum pickup — veto counterfactual; GIYOTIN_REVIVAL_LANE=1."""
    return os.getenv("GIYOTIN_REVIVAL_LANE", "0") != "0"


def giyotin_revival_age_min_sec() -> float:
    return _f("GIYOTIN_REVIVAL_AGE_MIN_SEC", "1800")


def giyotin_revival_liq_min() -> float:
    return _f("GIYOTIN_REVIVAL_LIQ_MIN", "80000")


def giyotin_revival_h1_min() -> float:
    return _f("GIYOTIN_REVIVAL_H1_MIN", "12")


def giyotin_revival_m5_min() -> float:
    return _f("GIYOTIN_REVIVAL_M5_MIN", "3")


def giyotin_revival_wallet_min() -> int:
    return _i("GIYOTIN_REVIVAL_WALLET_MIN", "4")


def giyotin_revival_score_min() -> float:
    return _f("GIYOTIN_REVIVAL_SCORE_MIN", "72")


def giyotin_revival_wallet_cluster_min() -> int:
    """wallet≥N iken daha düşük billy (BatCat tipi)."""
    return _i("GIYOTIN_REVIVAL_WALLET_CLUSTER_MIN", "5")


def giyotin_revival_score_min_cluster() -> float:
    return _f("GIYOTIN_REVIVAL_SCORE_MIN_CLUSTER", "70")


def giyotin_revival_score_min_for(wallet_count: int) -> float:
    if wallet_count >= giyotin_revival_wallet_cluster_min():
        return giyotin_revival_score_min_cluster()
    return giyotin_revival_score_min()


def giyotin_revival_max_open() -> int:
    return _i("GIYOTIN_REVIVAL_MAX_OPEN", "1")


def giyotin_revival_position_usd() -> float:
    return _f("GIYOTIN_REVIVAL_POSITION_USD", "40")


def is_giyotin_revival_position(pos: Position) -> bool:
    regime = (getattr(pos, "entry_regime", "") or "").lower()
    return regime == "giyotin_revival" or regime.endswith("_revival")


def count_giyotin_revival_open(positions: list[Position]) -> int:
    return sum(1 for p in positions if is_giyotin_revival_position(p))


def giyotin_runner_scout_lane_enabled() -> bool:
    """pump_bl micro chase — veto counterfactual; GIYOTIN_RUNNER_SCOUT_LANE=1."""
    return os.getenv("GIYOTIN_RUNNER_SCOUT_LANE", "0") != "0"


def giyotin_runner_scout_age_min_sec() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_AGE_MIN_SEC", "480")


def giyotin_runner_scout_age_max_sec() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_AGE_MAX_SEC", "1500")


def giyotin_runner_scout_liq_min() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_LIQ_MIN", "12000")


def giyotin_runner_scout_liq_max() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_LIQ_MAX", "30000")


def giyotin_runner_scout_h1_min() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_H1_MIN", "15")


def giyotin_runner_scout_h1_max() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_H1_MAX", "70")


def giyotin_runner_scout_m5_min() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_M5_MIN", "3")


def giyotin_runner_scout_m5_max() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_M5_MAX", "20")


def giyotin_runner_scout_score_min() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_SCORE_MIN", "85")


def giyotin_runner_scout_wallet_min() -> int:
    return _i("GIYOTIN_RUNNER_SCOUT_WALLET_MIN", "2")


def giyotin_runner_scout_max_open() -> int:
    return _i("GIYOTIN_RUNNER_SCOUT_MAX_OPEN", "1")


def giyotin_runner_scout_position_usd() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_POSITION_USD", "15")


def giyotin_runner_scout_hard_stop_pct() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_HARD_STOP_PCT", "-3.0")


def giyotin_runner_scout_trail_arm_mfe_pct() -> float:
    return _f("GIYOTIN_RUNNER_SCOUT_TRAIL_ARM_MFE_PCT", "12.0")


def is_giyotin_runner_scout_position(pos: Position) -> bool:
    regime = (getattr(pos, "entry_regime", "") or "").lower()
    return regime == "giyotin_runner_scout" or regime.endswith("_runner_scout")


def count_giyotin_runner_scout_open(positions: list[Position]) -> int:
    return sum(1 for p in positions if is_giyotin_runner_scout_position(p))


def giyotin_alpha_mimic_lane_enabled() -> bool:
    """Top KOL son alım event'i taklit — GIYOTIN_ALPHA_MIMIC_LANE=1."""
    return os.getenv("GIYOTIN_ALPHA_MIMIC_LANE", "0") != "0"


def giyotin_alpha_mimic_age_min_sec() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_AGE_MIN_SEC", "480")


def giyotin_alpha_mimic_age_max_sec() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_AGE_MAX_SEC", "1500")


def giyotin_alpha_mimic_liq_min() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_LIQ_MIN", "12000")


def giyotin_alpha_mimic_liq_max() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_LIQ_MAX", "80000")


def giyotin_alpha_mimic_h1_min() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_H1_MIN", "10")


def giyotin_alpha_mimic_h1_max() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_H1_MAX", "70")


def giyotin_alpha_mimic_m5_min() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_M5_MIN", "2")


def giyotin_alpha_mimic_m5_max() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_M5_MAX", "25")


def giyotin_alpha_mimic_score_min() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_SCORE_MIN", "72")


def giyotin_alpha_mimic_max_open() -> int:
    return _i("GIYOTIN_ALPHA_MIMIC_MAX_OPEN", "1")


def giyotin_alpha_mimic_position_usd() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_POSITION_USD", "20")


def giyotin_alpha_mimic_hard_stop_pct() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_HARD_STOP_PCT", "-3.0")


def giyotin_alpha_mimic_trail_arm_mfe_pct() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_TRAIL_ARM_MFE_PCT", "12.0")


def giyotin_alpha_mimic_window_sec() -> float:
    return _f("GIYOTIN_ALPHA_MIMIC_WINDOW_SEC", "900")


def is_giyotin_alpha_mimic_position(pos: Position) -> bool:
    regime = (pos.entry_regime or "").lower()
    return regime == "giyotin_alpha_mimic" or regime.endswith("_alpha_mimic")


def count_giyotin_alpha_mimic_open(positions: list[Position]) -> int:
    return sum(1 for p in positions if is_giyotin_alpha_mimic_position(p))


def giyotin_high_conviction_guillotine_mfe_pct() -> float:
    """−1 = guillotine kapalı (TROLL/VINE $400+ poz)."""
    return _f("GIYOTIN_HC_GUILLOTINE_MFE_PCT", "-1")


def giyotin_hc_guillotine_liq_min() -> float:
    return _f("GIYOTIN_HC_GUILLOTINE_LIQ_MIN", "500000")


def giyotin_hc_guillotine_score_min() -> float:
    return _f("GIYOTIN_HC_GUILLOTINE_SCORE_MIN", "90")


def giyotin_qualifies_high_conviction(bonus_billy: float, liq_usd: float) -> bool:
    hc_pct = giyotin_high_conviction_pct()
    if hc_pct <= 0:
        return False
    return (
        float(liq_usd or 0) >= giyotin_high_conviction_liq_min()
        and float(bonus_billy or 0) >= giyotin_high_conviction_billy_min()
    )


def is_giyotin_high_conviction_position(pos: Position) -> bool:
    if getattr(pos, "giyotin_high_conviction", False):
        return True
    liq = float(getattr(pos, "liq_entry", 0) or 0)
    score = float(getattr(pos, "entry_score", 0) or 0)
    return (
        liq >= giyotin_hc_guillotine_liq_min()
        and score >= giyotin_hc_guillotine_score_min()
    )


def dead_cat_pump_bl_waiver(h1: float, m5: float) -> bool:
    """pump_bl oturumunda yalnız derin dump + kontrollü m5 sıçrama."""
    return (
        h1 <= giyotin_dead_cat_h1_pump_bl_max()
        and giyotin_dead_cat_m5_min() <= m5 <= giyotin_dead_cat_m5_pump_bl_max()
    )


def giyotin_turn_tier1_max() -> float:
    """Sadece aşırı turnover'da hard veto (varsayılan 80x)."""
    return _f("GIYOTIN_TURN_TIER1_MAX", "80")


def giyotin_turn_tier1_reduce_at() -> float:
    """Bu eşiğin üstünde giriş veto değil, pozisyon küçültme."""
    return _f("GIYOTIN_TURN_TIER1_REDUCE", "40")


def giyotin_turn_size_scale(turn: float) -> float:
    """turn > reduce_at → slot payını düşür (1.0 → 0.40 arası)."""
    reduce_at = giyotin_turn_tier1_reduce_at()
    veto_at = giyotin_turn_tier1_max()
    t = float(turn or 0)
    if t <= reduce_at:
        return 1.0
    if t >= veto_at:
        return 0.40
    span = max(veto_at - reduce_at, 1.0)
    frac = (t - reduce_at) / span
    return max(0.40, 1.0 - frac * 0.60)


def giyotin_liq_mcap_max() -> float:
    return _f("GIYOTIN_LIQ_MCAP_MAX", "0.80")


def giyotin_m5_forbid() -> float:
    return _f("GIYOTIN_M5_FORBID", "15.0")


def giyotin_h24_min() -> float:
    return _f("GIYOTIN_H24_MIN", "-50")


def giyotin_tier2_min() -> float:
    return _f("GIYOTIN_TIER2_MIN", "85")


def giyotin_billy_min() -> float:
    adaptive = _adaptive_thresholds()
    return float(adaptive.get("a_billy_min") or adaptive.get("billy_min") or _f("GIYOTIN_A_BILLY_MIN", "75"))


def giyotin_m5_max() -> float:
    adaptive = _adaptive_thresholds()
    return float(adaptive.get("a_m5_max") or adaptive.get("m5_max") or _f("GIYOTIN_A_M5_MAX", "5.0"))


def giyotin_h1_min() -> float:
    return giyotin_a_h1_min()


def giyotin_turn_max() -> float:
    adaptive = _adaptive_thresholds()
    return float(adaptive.get("a_turn_max") or adaptive.get("turn_max") or _f("GIYOTIN_A_TURN_MAX", "20"))


def giyotin_a_m5_max() -> float:
    adaptive = _adaptive_thresholds()
    return float(adaptive.get("a_m5_max") or adaptive.get("m5_max") or _f("GIYOTIN_A_M5_MAX", "5.0"))


def giyotin_a_h1_min() -> float:
    adaptive = _adaptive_thresholds()
    return float(adaptive.get("a_h1_min") or _f("GIYOTIN_A_H1_MIN", "10.0"))


def giyotin_a_h24_min() -> float:
    return _f("GIYOTIN_A_H24_MIN", "-10.0")


def giyotin_a_turn_max() -> float:
    adaptive = _adaptive_thresholds()
    return float(adaptive.get("a_turn_max") or adaptive.get("turn_max") or _f("GIYOTIN_A_TURN_MAX", "20"))


def giyotin_a_billy_min() -> float:
    adaptive = _adaptive_thresholds()
    return float(adaptive.get("a_billy_min") or adaptive.get("billy_min") or _f("GIYOTIN_A_BILLY_MIN", "75"))


def giyotin_b_m5_min() -> float:
    return _f("GIYOTIN_B_M5_MIN", "5.01")


def giyotin_b_h1_min() -> float:
    adaptive = _adaptive_thresholds()
    return float(adaptive.get("b_h1_min") or _f("GIYOTIN_B_H1_MIN", "6.0"))


def giyotin_b_h24_min() -> float:
    return _f("GIYOTIN_B_H24_MIN", "20.0")


def giyotin_b_turn_max() -> float:
    adaptive = _adaptive_thresholds()
    return float(adaptive.get("b_turn_max") or _f("GIYOTIN_B_TURN_MAX", "25"))


def giyotin_b_billy_min() -> float:
    adaptive = _adaptive_thresholds()
    return float(adaptive.get("b_billy_min") or _f("GIYOTIN_B_BILLY_MIN", "64"))


def giyotin_bonus_a() -> float:
    return _f("GIYOTIN_BONUS_A", "20")


def giyotin_bonus_b() -> float:
    return _f("GIYOTIN_BONUS_B", "15")


def giyotin_bonus_ab() -> float:
    return _f("GIYOTIN_BONUS_AB", "35")


def giyotin_bonus_entry_min() -> float:
    return _f("GIYOTIN_BONUS_ENTRY_MIN", "79")


def giyotin_chase_m5_min() -> float:
    return _f("GIYOTIN_CHASE_M5_MIN", "8.0")


def giyotin_chase_h1_floor() -> float:
    return _f("GIYOTIN_CHASE_H1_FLOOR", "10.0")


def giyotin_m5_toxic() -> float:
    return _f("GIYOTIN_M5_TOXIC", "8.0")


def giyotin_turn_toxic() -> float:
    return _f("GIYOTIN_TURN_TOXIC", "30")


def giyotin_age_toxic_hours() -> float:
    return _f("GIYOTIN_AGE_TOXIC_H", "1.0")


def giyotin_sym_cooldown_sec() -> float:
    return _f("GIYOTIN_SYM_COOLDOWN_SEC", "3600")


def giyotin_max_position_usd() -> float:
    """0 = sabit USD tavan yok (güvenilirlik + bakiye %)."""
    return _f("GIYOTIN_MAX_POSITION_USD", "0")


def giyotin_max_position_pct() -> float:
    return _f("GIYOTIN_MAX_POSITION_PCT", "0.35")


def giyotin_min_position_usd() -> float:
    return _f("GIYOTIN_MIN_POSITION_USD", "10")


def giyotin_high_conviction_pct() -> float:
    """0=kapalı · bakiye oranı (ör. 0.60 → $500'de ~$300)."""
    return max(0.0, min(1.0, _f("GIYOTIN_HIGH_CONVICTION_PCT", "0")))


def giyotin_high_conviction_liq_min() -> float:
    return _f("GIYOTIN_HIGH_CONVICTION_LIQ_MIN", "80000")


def giyotin_high_conviction_billy_min() -> float:
    return _f("GIYOTIN_HIGH_CONVICTION_BILLY_MIN", "72")


def giyotin_size_by_reliability() -> bool:
    return os.getenv("GIYOTIN_SIZE_BY_RELIABILITY", "1") != "0"


def giyotin_hard_stop_pct() -> float:
    return _f("GIYOTIN_HARD_STOP_PCT", "-2.0")


def giyotin_volatile_guard_enabled() -> bool:
    return os.getenv("GIYOTIN_VOLATILE_GUARD", "1") != "0"


def giyotin_volatile_h1_pct() -> float:
    from hibrit_trader.giyotin_drought_guard import clamp_volatile_h1_pct

    return clamp_volatile_h1_pct(_f("GIYOTIN_VOLATILE_H1_PCT", "30"))


def giyotin_volatile_m5_pct() -> float:
    return _f("GIYOTIN_VOLATILE_M5_PCT", "8")


def giyotin_volatile_tight_stop_pct() -> float:
    return _f("GIYOTIN_VOLATILE_STOP_PCT", "-1.5")


def giyotin_volatile_size_scale() -> float:
    return max(0.25, min(1.0, _f("GIYOTIN_VOLATILE_SIZE_SCALE", "0.65")))


def is_volatile_giyotin_entry(*, chg_m5: float, chg_h1: float) -> bool:
    """Yüksek h1 veya m5 — blacklist chase profiline yakın giriş."""
    if not giyotin_volatile_guard_enabled():
        return False
    return float(chg_m5 or 0) > giyotin_volatile_m5_pct() or float(chg_h1 or 0) > giyotin_volatile_h1_pct()


def volatile_entry_guard_for_pair(pair: Pair) -> tuple[bool, float, float, str]:
    """(active, size_scale, hard_stop_pct, note)"""
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    if not is_volatile_giyotin_entry(chg_m5=m5, chg_h1=h1):
        return False, 1.0, giyotin_hard_stop_pct(), ""
    stop = giyotin_volatile_tight_stop_pct()
    scale = giyotin_volatile_size_scale()
    note = f"volatile guard m5={m5:.1f}% h1={h1:.1f}% stop={stop:.1f}% size×{scale:.2f}"
    return True, scale, stop, note


def position_hard_stop_pct(pos: Position) -> float:
    custom = float(getattr(pos, "giyotin_hard_stop_pct", 0) or 0)
    if custom < 0:
        return custom
    if is_giyotin_runner_scout_position(pos):
        return giyotin_runner_scout_hard_stop_pct()
    if is_giyotin_alpha_mimic_position(pos):
        return giyotin_alpha_mimic_hard_stop_pct()
    return giyotin_hard_stop_pct()


def apply_volatile_position_scale(position_usd: float, size_scale: float) -> float:
    if size_scale >= 0.999:
        return position_usd
    lo = giyotin_min_position_usd()
    return round(max(lo, position_usd * size_scale), 2)


def giyotin_guillotine_sec() -> float:
    return _f("GIYOTIN_MOMENTUM_SEC", "180")


def giyotin_missing_ticks_exit() -> int:
    """Watchlist dışı tick sayacı — veri kayboldu failsafe (~3dk: 6@30sn · 18@10sn)."""
    return _i("GIYOTIN_MISSING_TICKS_EXIT", "18")


def giyotin_guillotine_mfe_pct() -> float:
    return _f("GIYOTIN_GUILLOTINE_MFE_PCT", "1.0")


def giyotin_wallet2_guillotine_mfe_pct() -> float:
    """wallet2 şeridi guillotine MFE eşiği (varsayılan 0.5%, mature 1%)."""
    return _f("GIYOTIN_WALLET2_GUILLOTINE_MFE_PCT", "0.5")


def giyotin_revival_guillotine_mfe_pct() -> float:
    """revival şeridi guillotine MFE eşiği (varsayılan 0.5%, mature 1%)."""
    return _f("GIYOTIN_REVIVAL_GUILLOTINE_MFE_PCT", "0.5")


def guillotine_mfe_pct_for(pos: Position) -> float:
    if is_giyotin_high_conviction_position(pos):
        return giyotin_high_conviction_guillotine_mfe_pct()
    if is_giyotin_runner_scout_position(pos):
        return -1.0
    if is_giyotin_alpha_mimic_position(pos):
        return -1.0
    if is_giyotin_wallet2_position(pos):
        return giyotin_wallet2_guillotine_mfe_pct()
    if is_giyotin_revival_position(pos):
        return giyotin_revival_guillotine_mfe_pct()
    return giyotin_guillotine_mfe_pct()


def trail_arm_mfe_pct_for(pos: Position) -> float:
    if is_giyotin_runner_scout_position(pos):
        return giyotin_runner_scout_trail_arm_mfe_pct()
    if is_giyotin_alpha_mimic_position(pos):
        return giyotin_alpha_mimic_trail_arm_mfe_pct()
    if is_giyotin_dead_cat_position(pos):
        return giyotin_dead_cat_trail_arm_mfe_pct()
    return giyotin_trail_arm_mfe_pct()


def giyotin_be_trigger_pct() -> float:
    return _f("GIYOTIN_MFE_ARM_PCT", "3.0")


def giyotin_trail_arm_mfe_pct() -> float:
    return _f("GIYOTIN_TRAIL_ARM_MFE_PCT", "5.0")


def giyotin_trail_pct() -> float:
    return _f("GIYOTIN_TRAIL_PCT", "1.0")


def giyotin_partial_exit_enabled() -> bool:
    """Kademeli çıkış: varsayılan yalnız paper; live için GIYOTIN_PARTIAL_EXIT=1 açıkça."""
    flag = os.getenv("GIYOTIN_PARTIAL_EXIT", "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    return os.getenv("BOT_MODE", "paper") == "paper"


def giyotin_trail_partial_frac() -> float:
    return _f("GIYOTIN_TRAIL_PARTIAL_FRAC", "0.65")


def giyotin_runner_trail_pct() -> float:
    return _f("GIYOTIN_RUNNER_TRAIL_PCT", "12")


def giyotin_runner_min_mfe_pct() -> float:
    return _f("GIYOTIN_RUNNER_MIN_MFE_PCT", "8")


def giyotin_runner_floor_pct() -> float:
    """Partial sonrası runner — break-even yerine kâr tabanında kapat."""
    return _f("GIYOTIN_RUNNER_FLOOR_PCT", "0.5")


def giyotin_runner_profit_giveback_pct() -> float:
    """Peak'ten geri çekilme; geniş trail beklemeden kârdayken runner kapat."""
    return _f("GIYOTIN_RUNNER_PROFIT_GIVEBACK_PCT", "6")


def giyotin_runner_be_disabled() -> bool:
    """Runner bacağında pnl≤0 break-even yerine floor kullan."""
    flag = os.getenv("GIYOTIN_RUNNER_BE_DISABLE", "1").strip().lower()
    return flag not in ("0", "false", "no")


def giyotin_runner_tp_pct() -> float:
    """Kalan bacak PnL% bu eşiğe gelince anında kapat (0=kapalı)."""
    return _f("GIYOTIN_RUNNER_TP_PCT", "8")


def giyotin_runner_tp_usd() -> float:
    """Kalan bacak unrealized USD >= bu → kapat (0=kapalı)."""
    return _f("GIYOTIN_RUNNER_TP_USD", "2.5")


def giyotin_runner_tp_arm_pnl_pct() -> float:
    """PnL giveback için runner tepe pnl minimumu."""
    return _f("GIYOTIN_RUNNER_TP_ARM_PNL_PCT", "5")


def giyotin_runner_pnl_giveback_pct() -> float:
    """Runner tepe pnl'den bu kadar düşünce hâlâ yeşilken kapat."""
    return _f("GIYOTIN_RUNNER_PNL_GIVEBACK_PCT", "2")


def giyotin_stale_alarm_limit() -> int:
    return _i("GIYOTIN_STALE_ALARM", "30")


def giyotin_adaptive_review_n() -> int:
    return _i("GIYOTIN_ADAPTIVE_REVIEW_N", "10")


def net_pnl_after_friction(raw_pnl_pct: float) -> float:
    return raw_pnl_pct - MIN_ROUND_TRIP_FRICTION_PCT


def trade_beats_friction(raw_pnl_pct: float) -> bool:
    return net_pnl_after_friction(raw_pnl_pct) > 0


def _load_session() -> dict:
    state = _state_file()
    if not state.is_file():
        return {}
    try:
        return json.loads(state.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_save(data: dict) -> None:
    state = _state_file()
    state.parent.mkdir(parents=True, exist_ok=True)
    tmp = state.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, state)


def _mutate_session(mutator) -> dict:
    with _LOCK:
        data = _load_session()
        mutator(data)
        data["version"] = _VERSION
        data["engine"] = _ENGINE
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_save(data)
        return data


def _adaptive_thresholds() -> dict:
    return dict(_load_session().get("adaptive") or {})


def ensure_giyotin_shipped() -> str:
    if not giyotin_mode_enabled():
        return ""
    with _LOCK:
        data = _load_session()
        prev_engine = data.get("engine")
        if data.get("ship_at") and prev_engine == _ENGINE:
            return data["ship_at"]
        ship_at = datetime.now(timezone.utc).isoformat()
        data.update(
            {
                "ship_at": ship_at,
                "mode": "giyotin",
                "version": _VERSION,
                "engine": _ENGINE,
                "pump_blacklist": data.get("pump_blacklist", []),
                "guillotine_blacklist": data.get("guillotine_blacklist", []),
                "positions": data.get("positions", {}),
                "sym_last_buy": data.get("sym_last_buy", {}),
                "adaptive": data.get("adaptive") or {
                    "a_billy_min": giyotin_a_billy_min(),
                    "a_m5_max": float(os.getenv("GIYOTIN_A_M5_MAX", "5.0")),
                    "a_turn_max": float(os.getenv("GIYOTIN_A_TURN_MAX", "20")),
                    "b_h1_min": float(os.getenv("GIYOTIN_B_H1_MIN", "6.0")),
                    "b_billy_min": float(os.getenv("GIYOTIN_B_BILLY_MIN", "64")),
                },
                "winner_profiles": data.get("winner_profiles", []),
                "btc_m15_last": data.get("btc_m15_last"),
                "entry_halted_until": None if prev_engine != _ENGINE else data.get("entry_halted_until"),
            }
        )
        if prev_engine and prev_engine != _ENGINE:
            data["adaptive"] = {
                "a_billy_min": float(os.getenv("GIYOTIN_A_BILLY_MIN", "75")),
                "a_m5_max": float(os.getenv("GIYOTIN_A_M5_MAX", "5.0")),
                "a_turn_max": float(os.getenv("GIYOTIN_A_TURN_MAX", "20")),
                "b_h1_min": float(os.getenv("GIYOTIN_B_H1_MIN", "6.0")),
                "b_billy_min": float(os.getenv("GIYOTIN_B_BILLY_MIN", "64")),
            }
        _atomic_save(data)
        return ship_at


def ship_timestamp() -> str:
    if not giyotin_mode_enabled():
        return ""
    data = _load_session()
    if data.get("ship_at"):
        return data["ship_at"]
    return ensure_giyotin_shipped()


def _blacklist_key(token_address: str) -> str:
    return (token_address or "").strip().lower()


def blacklist_pump_token(token_address: str, *, pool_created_at: float | None = None) -> None:
    if not token_address:
        return
    key = _blacklist_key(token_address)

    def _m(d: dict) -> None:
        bl = set(d.get("pump_blacklist") or [])
        bl.add(key)
        d["pump_blacklist"] = sorted(bl)

    _mutate_session(_m)
    try:
        from hibrit_trader.mint_lifecycle import record_pump_bl

        record_pump_bl(token_address, pool_created_at=pool_created_at)
    except OSError:
        pass


def blacklist_guillotine_token(token_address: str) -> None:
    if not token_address:
        return
    key = _blacklist_key(token_address)

    def _m(d: dict) -> None:
        bl = set(d.get("guillotine_blacklist") or [])
        bl.add(key)
        d["guillotine_blacklist"] = sorted(bl)

    _mutate_session(_m)


def is_pump_blacklisted(token_address: str) -> bool:
    return _blacklist_key(token_address) in set(_load_session().get("pump_blacklist") or [])


def is_guillotine_blacklisted(token_address: str) -> bool:
    return _blacklist_key(token_address) in set(_load_session().get("guillotine_blacklist") or [])


def is_sym_on_cooldown(token_address: str) -> bool:
    key = _blacklist_key(token_address)
    if not key:
        return False
    last = float((_load_session().get("sym_last_buy") or {}).get(key) or 0)
    return last > 0 and (time.time() - last) < giyotin_sym_cooldown_sec()


def record_sym_buy(token_address: str) -> None:
    key = _blacklist_key(token_address)
    if not key:
        return

    def _m(d: dict) -> None:
        sym = dict(d.get("sym_last_buy") or {})
        sym[key] = time.time()
        d["sym_last_buy"] = sym

    _mutate_session(_m)


def entry_halted() -> tuple[bool, str]:
    until = _load_session().get("entry_halted_until")
    if not until:
        return False, ""
    try:
        end = datetime.fromisoformat(str(until).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return False, ""
    if time.time() < end:
        return True, f"giyotin v4.2 giriş durdu — {until} (10t 0 win)"
    return False, ""


def _valid_transition(old: str, new: str) -> bool:
    if old == new:
        return True
    return new in _VALID_TRANSITIONS.get(old or "", set())


def transition_giyotin_state(pos: Position, new_state: GiyotinState) -> bool:
    old = pos.giyotin_phase or GiyotinState.POSITION_OPEN.value
    new = new_state.value
    if not _valid_transition(old, new):
        return False
    pos.giyotin_phase = new
    persist_position_state(pos)
    return True


def remove_position_from_state(token_address: str) -> None:
    key = _blacklist_key(token_address)
    if not key:
        return

    def _m(d: dict) -> None:
        positions = dict(d.get("positions") or {})
        positions.pop(key, None)
        d["positions"] = positions

    _mutate_session(_m)


def persist_position_state(pos: Position, *, billy: float = 0.0, profile: str = "") -> None:
    key = _blacklist_key(pos.token_address)
    if not key:
        return
    b = billy if billy > 0 else float(pos.entry_score or 0)
    prof = profile or (pos.entry_regime or "").replace("giyotin_", "").upper()

    def _m(d: dict) -> None:
        positions = dict(d.get("positions") or {})
        row = {
            "pair": pos.pair_name,
            "pool": pos.pool_address,
            "phase": pos.giyotin_phase,
            "entry_price": pos.entry_price,
            "opened_ts": pos.opened_ts,
            "mfe_pct": round(float(pos.mfe_pct or 0), 3),
            "breakeven_armed": pos.breakeven_armed,
            "trail_armed": pos.trail_armed,
            "trail_peak_usd": pos.giyotin_trail_peak_usd,
            "runner_armed": bool(getattr(pos, "giyotin_runner_armed", False)),
            "billy": round(b, 1),
        }
        if prof:
            row["profile"] = prof
        if getattr(pos, "giyotin_volatile_entry", False):
            row["volatile_entry"] = True
            hsp = float(getattr(pos, "giyotin_hard_stop_pct", 0) or 0)
            if hsp < 0:
                row["hard_stop_pct"] = hsp
        positions[key] = row
        d["positions"] = positions

    _mutate_session(_m)


def record_btc_m15_snapshot(m15: float | None) -> None:
    def _m(d: dict) -> None:
        d["btc_m15_last"] = m15

    _mutate_session(_m)


def giyotin_reliability_scale(bonus_billy: float, profile: str | None) -> float:
    """Bonus Billy + profil → slot payı (≈0.30–1.0)."""
    b = max(giyotin_bonus_entry_min(), float(bonus_billy or 0))
    scale = 0.30 + min(1.0, (b - 79.0) / 51.0) * 0.70
    prof = (profile or "").upper()
    if prof == "AB":
        scale = min(1.0, scale * 1.12)
    elif prof == "A":
        scale = min(1.0, scale * 1.06)
    return scale


def giyotin_slot_budget(balance: float, max_open: int, open_count: int) -> float:
    slots = max(1, max_open - max(0, open_count))
    return max(0.0, balance) / slots


def compute_giyotin_position_usd(
    balance: float,
    bonus_billy: float,
    profile: str | None = None,
    *,
    open_count: int = 0,
    max_open: int | None = None,
    turn_size_scale: float = 1.0,
    liq_usd: float = 0.0,
) -> float:
    mo = max_open if max_open is not None else giyotin_max_open()
    budget = giyotin_slot_budget(balance, mo, open_count)
    t_scale = max(0.0, min(1.0, float(turn_size_scale or 1.0)))
    scale = giyotin_reliability_scale(bonus_billy, profile) * t_scale
    if (profile or "").lower() == "bridge":
        return round(giyotin_bridge_position_usd(), 2)
    if (profile or "").lower() == "dead_cat":
        return round(giyotin_dead_cat_position_usd(), 2)
    if (profile or "").lower() == "gap":
        return round(giyotin_gap_position_usd(), 2)
    if (profile or "").lower() == "wallet2":
        return round(giyotin_wallet2_position_usd(), 2)
    if (profile or "").lower() == "revival":
        return round(giyotin_revival_position_usd(), 2)
    if (profile or "").lower() == "runner_scout":
        return round(giyotin_runner_scout_position_usd(), 2)
    if (profile or "").lower() == "alpha_mimic":
        return round(giyotin_alpha_mimic_position_usd(), 2)
    lo = giyotin_min_position_usd()
    cap_fixed = giyotin_max_position_usd()
    cap_pct = balance * giyotin_max_position_pct()
    if cap_fixed > 0:
        hi = min(cap_pct, cap_fixed)
    else:
        hi = cap_pct
    hc_pct = giyotin_high_conviction_pct()
    if (
        hc_pct > 0
        and float(liq_usd or 0) >= giyotin_high_conviction_liq_min()
        and float(bonus_billy or 0) >= giyotin_high_conviction_billy_min()
    ):
        size = balance * hc_pct
        return round(max(lo, min(size, hi)), 2)
    size = budget * scale
    if cap_fixed > 0:
        hi_slot = min(cap_pct, cap_fixed, budget)
    else:
        hi_slot = min(cap_pct, budget)
    return round(max(lo, min(size, hi_slot)), 2)


def giyotin_provisional_position_usd(
    balance: float,
    *,
    open_count: int = 0,
    max_open: int | None = None,
) -> float:
    """Kapı/tier1 prob — slot tavanı (sabit $20 yok)."""
    mo = max_open if max_open is not None else giyotin_max_open()
    return round(giyotin_slot_budget(balance, mo, open_count), 2)


def giyotin_cap_position_usd(computed_usd: float) -> float:
    lo = giyotin_min_position_usd()
    cap_fixed = giyotin_max_position_usd()
    if cap_fixed > 0:
        return max(lo, min(computed_usd, cap_fixed))
    return max(lo, computed_usd)


def init_giyotin_position(
    pos: Position,
    *,
    billy: float = 0.0,
    profile: str = "",
    volatile_entry: bool = False,
    hard_stop_pct: float = 0.0,
) -> None:
    if pos.opened_ts <= 0:
        pos.opened_ts = time.time()
    pos.giyotin_trail_peak_usd = 0.0
    pos.giyotin_runner_armed = False
    pos.breakeven_armed = False
    pos.trail_armed = False
    pos.giyotin_phase = GiyotinState.POSITION_OPEN.value
    pos.giyotin_volatile_entry = bool(volatile_entry)
    pos.giyotin_hard_stop_pct = float(hard_stop_pct) if hard_stop_pct < 0 else 0.0
    if profile:
        pos.entry_regime = f"giyotin_{profile.lower()}"
    if billy > 0:
        pos.entry_score = billy
    persist_position_state(pos, billy=billy, profile=profile)
    record_sym_buy(pos.token_address)


def pair_turnover_estimate(pair: Pair) -> float:
    return float(pair.vol_h24 or 0) / max(float(pair.liquidity_usd or 0), 1.0)


def pair_age_hours(pair: Pair) -> float | None:
    if not pair.pool_created_at:
        return None
    return max(0.0, (time.time() - pair.pool_created_at) / 3600.0)


def pair_age_minutes(pair: Pair, age_hours: float | None = None) -> float | None:
    ah = age_hours if age_hours is not None else pair_age_hours(pair)
    if ah is None:
        return None
    return ah * 60.0


def vol_mcap_ratio(pair: Pair) -> float:
    mcap = float(pair.market_cap_usd or 0)
    if mcap <= 0:
        return 0.0
    return float(pair.vol_h24 or 0) / mcap


def liq_mcap_ratio(pair: Pair) -> float:
    mcap = float(pair.market_cap_usd or 0)
    if mcap <= 0:
        return 0.0
    return float(pair.liquidity_usd or 0) / mcap


def billy_fit_score(
    *,
    chg_m5: float,
    moonshot_score: float,
    turnover: float,
    age_hours: float | None = None,
    txns_h1: int = 0,
) -> float:
    pts = 0.0
    moon = float(moonshot_score or 0)
    turn = float(turnover or 0)
    m5 = float(chg_m5 or 0)

    if moon <= 25:
        pts += 25
    elif moon <= 50:
        pts += 15
    elif moon <= 70:
        pts += 5

    if turn <= 3:
        pts += 25
    elif turn <= 10:
        pts += 15
    elif turn <= 25:
        pts += 5

    if 0.0 <= m5 <= 5.0:
        pts += max(0.0, 25.0 - abs(m5 - 2.5) * 5.0)
    elif 2.0 <= m5 <= 8.0:
        pts += max(0.0, 15.0 - abs(m5 - 5.0) * 3.0)

    if age_hours is None:
        pts += 8
    elif age_hours >= 24.0:
        pts += min(25.0, 10.0 + age_hours / 20.0)
    elif age_hours >= 0.5:
        pts += 10.0

    if 50 <= txns_h1 <= 800:
        pts += 5.0

    return round(min(100.0, max(0.0, pts)), 1)


def billy_fit_note(score: float) -> str:
    if score >= 75:
        return "BILLY-runner profili güçlü"
    if score >= 50:
        return "kısmi BILLY uyumu"
    return "chase/hype profili — dikkat"


@dataclass
class GiyotinEvalResult:
    ok: bool
    reason: str
    tier_failed: str | None = None
    billy: float = 0.0
    tier2_score: float = 0.0
    golden_ok: bool = False
    m5: float = 0.0
    h1: float = 0.0
    h24: float = 0.0
    turn: float = 0.0
    btc_m15: float | None = None
    tier2_parts: dict = field(default_factory=dict)
    profile_a_ok: bool = False
    profile_b_ok: bool = False
    deadly_chase: bool = False
    bonus_billy: float = 0.0
    entry_profile: str | None = None
    turn_size_scale: float = 1.0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "tier_failed": self.tier_failed,
            "billy": self.billy,
            "tier2_score": round(self.tier2_score, 1),
            "golden_ok": self.golden_ok,
            "m5": round(self.m5, 2),
            "h1": round(self.h1, 2),
            "h24": round(self.h24, 2),
            "turn": round(self.turn, 2),
            "btc_m15": round(self.btc_m15, 2) if self.btc_m15 is not None else None,
            "tier2_parts": self.tier2_parts,
            "profile_a_ok": self.profile_a_ok,
            "profile_b_ok": self.profile_b_ok,
            "deadly_chase": self.deadly_chase,
            "bonus_billy": round(self.bonus_billy, 1),
            "entry_profile": self.entry_profile,
            "turn_size_scale": round(self.turn_size_scale, 2),
        }


def _eval_ctx(
    pair: Pair,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
) -> tuple[float, float, float, float | None, float, float]:
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    h24 = float(pair.chg_h24 or 0)
    turn = float(turnover if turnover is not None else pair_turnover_estimate(pair))
    age_h = age_hours if age_hours is not None else pair_age_hours(pair)
    billy = billy_fit_score(
        chg_m5=m5,
        moonshot_score=float(moonshot_score or 0),
        turnover=turn,
        age_hours=age_h,
        txns_h1=int(pair.txns_h1 or 0),
    )
    return m5, h1, h24, age_h, turn, billy


def evaluate_toxic_intersection(
    pair: Pair,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
) -> tuple[bool, str]:
    """Zehirli kesişim — 4'lünün hepsi TRUE ise Billy ne olursa olsun GİRME."""
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    turn = float(turnover if turnover is not None else pair_turnover_estimate(pair))
    age_h = age_hours if age_hours is not None else pair_age_hours(pair)
    toxic = (
        m5 > giyotin_m5_toxic()
        and h1 < 0
        and turn > giyotin_turn_toxic()
        and age_h is not None
        and age_h < giyotin_age_toxic_hours()
    )
    if toxic:
        return (
            True,
            f"zehirli ✗ m5 %{m5:.1f} h1 %{h1:.1f} turn {turn:.1f}x age {age_h:.1f}h",
        )
    return False, ""


def evaluate_tier1_elimination(
    pair: Pair,
    position_usd: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> tuple[bool, str]:
    token = pair.token_address or ""
    m5, h1, h24, age_h, turn, _b = _eval_ctx(
        pair, turnover=turnover, age_hours=age_hours, wallet_count=wallet_count
    )

    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()
    record_btc_m15_snapshot(m15)
    blocked, macro_note = btc_macro_blocks_entry(m15)
    if blocked:
        return False, f"tier1 {macro_note}"

    halted, halt_note = entry_halted()
    if halted:
        return False, f"tier1 {halt_note}"

    if is_guillotine_blacklisted(token):
        return False, "tier1 guillotine blacklist (oturum)"
    if is_pump_blacklisted(token):
        from hibrit_trader.giyotin_drought_guard import drought_pump_chase_blocked

        chase_blocked, chase_note = drought_pump_chase_blocked(h1, pump_blacklisted=True)
        if chase_blocked:
            return False, f"tier1 {chase_note}"
        return False, "tier1 pump blacklist (oturum)"
    if is_sym_on_cooldown(token):
        return False, f"tier1 sym cooldown {giyotin_sym_cooldown_sec()/60:.0f}dk"

    mcap = float(pair.market_cap_usd or 0)
    if mcap > 0 and mcap < giyotin_mcap_min():
        return False, f"tier1 mcap ${mcap:.0f} < ${giyotin_mcap_min():.0f}"

    age_min = pair_age_minutes(pair, age_h)
    if age_min is not None and age_min < giyotin_age_min_sec() / 60.0:
        return False, f"tier1 age {age_min:.0f}dk < {giyotin_age_min_sec()/60:.0f}dk"

    if wallet_count < giyotin_holders_min():
        return False, f"tier1 cüzdan {wallet_count} < {giyotin_holders_min()}"

    liq = float(pair.liquidity_usd or 0)
    liq_min = giyotin_mature_liq_min()
    if liq_min > 0 and liq < liq_min:
        return False, f"tier1 liq ${liq:,.0f} < ${liq_min:,.0f}"

    if turn > giyotin_turn_tier1_max():
        return False, f"tier1 turn {turn:.1f}x > {giyotin_turn_tier1_max():.0f}x"

    lmc = liq_mcap_ratio(pair)
    if mcap > 0 and lmc > giyotin_liq_mcap_max():
        return False, f"tier1 liq/mcap {lmc:.0%} > {giyotin_liq_mcap_max():.0%}"

    if m5 > giyotin_m5_forbid():
        blacklist_pump_token(token, pool_created_at=pair.pool_created_at)
        return False, f"tier1 m5 %{m5:.1f} > {giyotin_m5_forbid():.0f}% (blacklist)"

    if h1 < 0:
        return False, f"tier1 h1 %{h1:.1f} < 0 (momentum yok)"

    if h24 < giyotin_h24_min():
        return False, f"tier1 h24 %{h24:.1f} < {giyotin_h24_min():.0f}%"

    cap = giyotin_max_position_usd()
    if cap > 0 and position_usd > cap:
        return False, f"tier1 poz ${position_usd:.0f} > ${cap:.0f}"

    turn_note = ""
    if turn > giyotin_turn_tier1_reduce_at():
        t_scale = giyotin_turn_size_scale(turn)
        turn_note = f" · turn {turn:.1f}x → poz %{t_scale * 100:.0f}"
    return True, f"tier1 ✓ · {macro_note}{turn_note}"


def evaluate_tier1_genesis(
    pair: Pair,
    position_usd: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> tuple[bool, str]:
    """Genesis şeridi tier1 — yaş 2–28dk, düşük liq, gevşek cüzdan (pump mint)."""
    token = pair.token_address or ""
    m5, h1, h24, age_h, turn, _b = _eval_ctx(
        pair, turnover=turnover, age_hours=age_hours, wallet_count=wallet_count
    )

    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()
    record_btc_m15_snapshot(m15)
    blocked, macro_note = btc_macro_blocks_entry(m15)
    if blocked:
        return False, f"genesis tier1 {macro_note}"

    halted, halt_note = entry_halted()
    if halted:
        return False, f"genesis tier1 {halt_note}"

    if is_guillotine_blacklisted(token):
        return False, "genesis tier1 guillotine blacklist (oturum)"
    if is_pump_blacklisted(token):
        return False, "genesis tier1 pump blacklist (oturum)"
    if is_sym_on_cooldown(token):
        return False, f"genesis tier1 sym cooldown {giyotin_sym_cooldown_sec()/60:.0f}dk"

    mcap = float(pair.market_cap_usd or 0)
    if mcap > 0 and mcap < giyotin_mcap_min():
        return False, f"genesis tier1 mcap ${mcap:.0f} < ${giyotin_mcap_min():.0f}"

    age_min = pair_age_minutes(pair, age_h)
    age_lo = giyotin_genesis_age_min_sec() / 60.0
    age_hi = giyotin_genesis_age_max_sec() / 60.0
    if age_min is None:
        return False, "genesis tier1 age bilinmiyor"
    if age_min < age_lo:
        return False, f"genesis tier1 age {age_min:.0f}dk < {age_lo:.0f}dk"
    if age_min > age_hi:
        return False, f"genesis tier1 age {age_min:.0f}dk > {age_hi:.0f}dk"

    pump_mint = is_giyotin_genesis_pair(pair)
    holders_min = giyotin_genesis_holders_min(pump_mint=pump_mint)
    if wallet_count < holders_min:
        return False, f"genesis tier1 cüzdan {wallet_count} < {holders_min}"

    liq = float(pair.liquidity_usd or 0)
    liq_min = giyotin_genesis_liq_min()
    if liq_min > 0 and liq < liq_min:
        return False, f"genesis tier1 liq ${liq:,.0f} < ${liq_min:,.0f}"

    turn_max = giyotin_genesis_turn_max()
    if turn > turn_max:
        return False, f"genesis tier1 turn {turn:.1f}x > {turn_max:.0f}x"

    lmc = liq_mcap_ratio(pair)
    if mcap > 0 and lmc > giyotin_liq_mcap_max():
        return False, f"genesis tier1 liq/mcap {lmc:.0%} > {giyotin_liq_mcap_max():.0%}"

    if m5 > giyotin_m5_forbid():
        blacklist_pump_token(token, pool_created_at=pair.pool_created_at)
        return False, f"genesis tier1 m5 %{m5:.1f} > {giyotin_m5_forbid():.0f}% (blacklist)"

    if h1 < 0:
        return False, f"genesis tier1 h1 %{h1:.1f} < 0 (momentum yok)"

    if h24 < giyotin_h24_min():
        return False, f"genesis tier1 h24 %{h24:.1f} < {giyotin_h24_min():.0f}%"

    cap = giyotin_max_position_usd()
    if cap > 0 and position_usd > cap:
        return False, f"genesis tier1 poz ${position_usd:.0f} > ${cap:.0f}"

    pump_note = " · pump" if pump_mint else ""
    return True, f"genesis tier1 ✓ · {macro_note}{pump_note}"


def evaluate_tier1_bridge(
    pair: Pair,
    position_usd: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> tuple[bool, str]:
    """Köprü şeridi tier1 — 28–120dk, liq≥20k, mature liq boşluğu."""
    token = pair.token_address or ""
    m5, h1, h24, age_h, turn, _b = _eval_ctx(
        pair, turnover=turnover, age_hours=age_hours, wallet_count=wallet_count
    )

    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()
    record_btc_m15_snapshot(m15)
    blocked, macro_note = btc_macro_blocks_entry(m15)
    if blocked:
        return False, f"bridge tier1 {macro_note}"

    halted, halt_note = entry_halted()
    if halted:
        return False, f"bridge tier1 {halt_note}"

    if is_guillotine_blacklisted(token):
        return False, "bridge tier1 guillotine blacklist (oturum)"
    if is_pump_blacklisted(token):
        return False, "bridge tier1 pump blacklist (oturum)"
    if is_sym_on_cooldown(token):
        return False, f"bridge tier1 sym cooldown {giyotin_sym_cooldown_sec()/60:.0f}dk"

    mcap = float(pair.market_cap_usd or 0)
    if mcap > 0 and mcap < giyotin_mcap_min():
        return False, f"bridge tier1 mcap ${mcap:.0f} < ${giyotin_mcap_min():.0f}"

    age_min = pair_age_minutes(pair, age_h)
    age_lo = giyotin_bridge_age_min_sec() / 60.0
    age_hi = giyotin_bridge_age_max_sec() / 60.0
    if age_min is None:
        return False, "bridge tier1 age bilinmiyor"
    if age_min <= age_lo:
        return False, f"bridge tier1 age {age_min:.0f}dk ≤ {age_lo:.0f}dk"
    if age_min > age_hi:
        return False, f"bridge tier1 age {age_min:.0f}dk > {age_hi:.0f}dk"

    if wallet_count < giyotin_holders_min():
        return False, f"bridge tier1 cüzdan {wallet_count} < {giyotin_holders_min()}"

    liq = float(pair.liquidity_usd or 0)
    liq_min = giyotin_bridge_liq_min()
    if liq_min > 0 and liq < liq_min:
        return False, f"bridge tier1 liq ${liq:,.0f} < ${liq_min:,.0f}"

    turn_max = giyotin_bridge_turn_max()
    if turn > turn_max:
        return False, f"bridge tier1 turn {turn:.1f}x > {turn_max:.0f}x"

    lmc = liq_mcap_ratio(pair)
    if mcap > 0 and lmc > giyotin_liq_mcap_max():
        return False, f"bridge tier1 liq/mcap {lmc:.0%} > {giyotin_liq_mcap_max():.0%}"

    if m5 > giyotin_m5_forbid():
        blacklist_pump_token(token, pool_created_at=pair.pool_created_at)
        return False, f"bridge tier1 m5 %{m5:.1f} > {giyotin_m5_forbid():.0f}% (blacklist)"

    if h1 < 0:
        return False, f"bridge tier1 h1 %{h1:.1f} < 0 (momentum yok)"

    if h24 < giyotin_h24_min():
        return False, f"bridge tier1 h24 %{h24:.1f} < {giyotin_h24_min():.0f}%"

    cap = giyotin_bridge_position_usd()
    if cap > 0 and position_usd > cap:
        return False, f"bridge tier1 poz ${position_usd:.0f} > ${cap:.0f}"

    return True, f"bridge tier1 ✓ · {macro_note}"


def score_tier2(
    pair: Pair,
    billy: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
) -> tuple[float, dict]:
    m5, h1, _h24, age_h, turn, _ = _eval_ctx(pair, turnover=turnover, age_hours=age_hours)
    parts: dict[str, float] = {"billy": billy}

    if h1 > 10.0:
        parts["h1_momentum"] = 10.0
    if 0.0 <= m5 <= 5.0:
        parts["m5_fresh"] = 10.0
    if turn < 10:
        parts["turn"] = 10.0
    elif turn < 20:
        parts["turn"] = 5.0
    else:
        parts["turn"] = 0.0

    vmc = vol_mcap_ratio(pair)
    if 2.0 <= vmc <= 10.0:
        parts["vol_mcap"] = 5.0

    age_min = pair_age_minutes(pair, age_h)
    if age_min is not None and giyotin_age_min_sec() / 60.0 <= age_min <= 360.0:
        parts["age"] = 5.0

    total = sum(parts.values())
    return total, parts


def evaluate_golden_intersection(
    pair: Pair,
    billy: float,
    *,
    turnover: float | None = None,
) -> tuple[bool, str]:
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    turn = float(turnover if turnover is not None else pair_turnover_estimate(pair))
    bmin = giyotin_billy_min()
    m5max = giyotin_m5_max()
    h1min = giyotin_h1_min()
    tmax = giyotin_turn_max()

    checks: list[tuple[bool, str]] = [
        (billy >= bmin, f"billy {billy:.0f}≥{bmin:.0f}"),
        (0.0 <= m5 <= m5max, f"m5 %{m5:.1f}∈[0,{m5max:.0f}]"),
        (h1 >= h1min, f"h1 %{h1:.1f}≥{h1min:.0f}"),
        (turn < tmax, f"turn {turn:.1f}x<{tmax:.0f}x"),
    ]
    fails = [note for ok, note in checks if not ok]
    if fails:
        return False, "altın ✗ " + " · ".join(fails)
    return True, "altın ✓ " + " · ".join(note for _, note in checks)


def evaluate_deadly_chase(pair: Pair) -> tuple[bool, str]:
    """Zehirli chase — yüksek 5M + zayıf 1H veya negatif 24H → GİRME."""
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    h24 = float(pair.chg_h24 or 0)
    chase_m5 = giyotin_chase_m5_min()
    h1_floor = giyotin_chase_h1_floor()
    deadly = m5 > chase_m5 and (h1 < h1_floor or h24 < 0)
    if deadly:
        return True, f"zehirli chase ✗ m5 %{m5:.1f}>{chase_m5:.0f} · h1 %{h1:.1f} · h24 %{h24:.1f}"
    return False, ""


def evaluate_profile_a(
    pair: Pair,
    billy: float,
    *,
    turnover: float | None = None,
) -> tuple[bool, dict[str, bool], str]:
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    h24 = float(pair.chg_h24 or 0)
    turn = float(turnover if turnover is not None else pair_turnover_estimate(pair))
    checks = {
        "m5": 0.0 <= m5 <= giyotin_a_m5_max(),
        "h1": h1 >= giyotin_a_h1_min(),
        "h24": h24 >= giyotin_a_h24_min(),
        "turn": turn < giyotin_a_turn_max(),
        "billy": billy >= giyotin_a_billy_min(),
    }
    ok = all(checks.values())
    note = "A " + ("✓" if ok else "✗") + " · " + " · ".join(
        f"{k}={'✓' if v else '✗'}" for k, v in checks.items()
    )
    return ok, checks, note


def evaluate_profile_b(
    pair: Pair,
    billy: float,
    *,
    turnover: float | None = None,
) -> tuple[bool, dict[str, bool], str]:
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    h24 = float(pair.chg_h24 or 0)
    turn = float(turnover if turnover is not None else pair_turnover_estimate(pair))
    checks = {
        "m5": m5 > giyotin_b_m5_min(),
        "h1": h1 >= giyotin_b_h1_min(),
        "h24": h24 >= giyotin_b_h24_min(),
        "turn": turn < giyotin_b_turn_max(),
        "billy": billy >= giyotin_b_billy_min(),
    }
    ok = all(checks.values())
    note = "B " + ("✓" if ok else "✗") + " · " + " · ".join(
        f"{k}={'✓' if v else '✗'}" for k, v in checks.items()
    )
    return ok, checks, note


def compute_bonus_billy(billy: float, profile_a: bool, profile_b: bool) -> float:
    if profile_a and profile_b:
        return billy + giyotin_bonus_ab()
    if profile_a:
        return billy + giyotin_bonus_a()
    if profile_b:
        return billy + giyotin_bonus_b()
    return billy


def entry_profile_label(profile_a: bool, profile_b: bool) -> str | None:
    if profile_a and profile_b:
        return "AB"
    if profile_a:
        return "A"
    if profile_b:
        return "B"
    return None


def evaluate_giyotin_hunter(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> GiyotinEvalResult:
    m5, h1, h24, age_h, turn, billy = _eval_ctx(
        pair,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
    )
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    t1_ok, t1_note = evaluate_tier1_elimination(
        pair,
        position_usd,
        turnover=turn,
        age_hours=age_h,
        wallet_count=wallet_count,
        btc_m15=m15,
    )
    if not t1_ok:
        return GiyotinEvalResult(False, f"giyotin {t1_note}", "tier1", billy, 0, False, m5, h1, h24, turn, m15)

    score_min = giyotin_entry_score_min()
    if score_min > 0 and billy < score_min:
        return GiyotinEvalResult(
            False,
            f"giyotin score {billy:.0f}<{score_min:.0f}",
            "score",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    deadly, deadly_note = evaluate_deadly_chase(pair)
    if deadly:
        return GiyotinEvalResult(
            False,
            f"giyotin {deadly_note}",
            "chase",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            deadly_chase=True,
        )

    a_ok, a_checks, a_note = evaluate_profile_a(pair, billy, turnover=turn)
    b_ok, b_checks, b_note = evaluate_profile_b(pair, billy, turnover=turn)
    if not a_ok and not b_ok:
        return GiyotinEvalResult(
            False,
            f"giyotin profil yok · {a_note} · {b_note}",
            "profile",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            profile_a_ok=a_ok,
            profile_b_ok=b_ok,
            tier2_parts={"a": a_checks, "b": b_checks},
        )

    bonus = compute_bonus_billy(billy, a_ok, b_ok)
    prof = entry_profile_label(a_ok, b_ok)
    bmin = giyotin_bonus_entry_min()
    if bonus < bmin:
        return GiyotinEvalResult(
            False,
            f"giyotin bonus Billy {bonus:.0f}<{bmin:.0f} · {a_note} · {b_note}",
            "bonus",
            billy,
            bonus,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            profile_a_ok=a_ok,
            profile_b_ok=b_ok,
            bonus_billy=bonus,
            entry_profile=prof,
            tier2_parts={"a": a_checks, "b": b_checks},
        )

    turn_scale = giyotin_turn_size_scale(turn)
    return GiyotinEvalResult(
        True,
        f"giyotin v4.2 ✓ profil={prof} bonus={bonus:.0f} · {a_note if a_ok else ''}{' · ' if a_ok and b_ok else ''}{b_note if b_ok else ''}".strip(),
        None,
        billy,
        bonus,
        True,
        m5,
        h1,
        h24,
        turn,
        m15,
        profile_a_ok=a_ok,
        profile_b_ok=b_ok,
        bonus_billy=bonus,
        entry_profile=prof,
        tier2_parts={"a": a_checks, "b": b_checks},
        turn_size_scale=turn_scale,
    )


def evaluate_giyotin_genesis_hunter(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> GiyotinEvalResult:
    """Genesis şeridi — genç coin (2–28dk), yüksek skor, düşük liq; profil A/B yok."""
    m5, h1, h24, age_h, turn, billy = _eval_ctx(
        pair,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
    )
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    t1_ok, t1_note = evaluate_tier1_genesis(
        pair,
        position_usd,
        turnover=turn,
        age_hours=age_h,
        wallet_count=wallet_count,
        btc_m15=m15,
    )
    if not t1_ok:
        return GiyotinEvalResult(
            False, f"giyotin {t1_note}", "genesis_tier1", billy, 0, False, m5, h1, h24, turn, m15
        )

    score_min = giyotin_genesis_score_min()
    if billy < score_min:
        return GiyotinEvalResult(
            False,
            f"giyotin genesis score {billy:.0f}<{score_min:.0f}",
            "genesis_score",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    deadly, deadly_note = evaluate_deadly_chase(pair)
    if deadly:
        return GiyotinEvalResult(
            False,
            f"giyotin genesis {deadly_note}",
            "chase",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            deadly_chase=True,
        )

    toxic, toxic_note = evaluate_toxic_intersection(pair, turnover=turn, age_hours=age_h)
    if toxic:
        return GiyotinEvalResult(
            False,
            f"giyotin genesis {toxic_note}",
            "toxic",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    return GiyotinEvalResult(
        True,
        f"giyotin genesis ✓ score={billy:.0f} · {t1_note}",
        None,
        billy,
        billy,
        True,
        m5,
        h1,
        h24,
        turn,
        m15,
        bonus_billy=billy,
        entry_profile="genesis",
        turn_size_scale=1.0,
    )


def evaluate_giyotin_bridge_hunter(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> GiyotinEvalResult:
    """Köprü şeridi V6-R — 28–120dk, liq 20–50k boşluğu, pullback momentum."""
    m5, h1, h24, age_h, turn, billy = _eval_ctx(
        pair,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
    )
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    t1_ok, t1_note = evaluate_tier1_bridge(
        pair,
        position_usd,
        turnover=turn,
        age_hours=age_h,
        wallet_count=wallet_count,
        btc_m15=m15,
    )
    if not t1_ok:
        return GiyotinEvalResult(
            False, f"giyotin {t1_note}", "bridge_tier1", billy, 0, False, m5, h1, h24, turn, m15
        )

    score_min = giyotin_bridge_score_min()
    if billy < score_min:
        return GiyotinEvalResult(
            False,
            f"giyotin bridge score {billy:.0f}<{score_min:.0f}",
            "bridge_score",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    h1_lo = giyotin_bridge_h1_min()
    h1_hi = giyotin_bridge_h1_max()
    if h1 < h1_lo:
        return GiyotinEvalResult(
            False,
            f"giyotin bridge h1 %{h1:.1f} < {h1_lo:.0f}%",
            "bridge_h1",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )
    if h1 > h1_hi:
        return GiyotinEvalResult(
            False,
            f"giyotin bridge h1 %{h1:.1f} > {h1_hi:.0f}%",
            "bridge_h1",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    m5_lo = giyotin_bridge_m5_min()
    m5_hi = giyotin_bridge_m5_max()
    if m5 < m5_lo or m5 > m5_hi:
        return GiyotinEvalResult(
            False,
            f"giyotin bridge m5 %{m5:.1f} ∉ [{m5_lo:.0f},{m5_hi:.0f}]",
            "bridge_m5",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    deadly, deadly_note = evaluate_deadly_chase(pair)
    if deadly:
        return GiyotinEvalResult(
            False,
            f"giyotin bridge {deadly_note}",
            "chase",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            deadly_chase=True,
        )

    toxic, toxic_note = evaluate_toxic_intersection(pair, turnover=turn, age_hours=age_h)
    if toxic:
        return GiyotinEvalResult(
            False,
            f"giyotin bridge {toxic_note}",
            "toxic",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    return GiyotinEvalResult(
        True,
        f"giyotin bridge ✓ score={billy:.0f} · {t1_note}",
        None,
        billy,
        0,
        False,
        m5,
        h1,
        h24,
        turn,
        m15,
        entry_profile="bridge",
        turn_size_scale=1.0,
    )


def _is_mature_world_pair(pair: Pair, billy: float) -> bool:
    """Yüksek liq + skor — mature şeritte kalmalı, FAZ2 karışmaz."""
    liq = float(pair.liquidity_usd or 0)
    return liq >= 150_000 and float(billy or 0) >= 85


def _phase2_age_window(age_hours: float | None, pair: Pair) -> bool:
    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False
    lo = giyotin_phase2_age_min_sec() / 60.0
    hi = giyotin_phase2_age_max_sec() / 60.0
    return lo <= age_min <= hi


def evaluate_tier1_phase2(
    pair: Pair,
    position_usd: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> tuple[bool, str]:
    """FAZ2 şeridi tier1 — 45dk-5s, liq 25-80k, pump_bl decay istisnası."""
    from hibrit_trader.mint_lifecycle import liq_growing, phase2_pump_bl_decay_ok, mint_phase, MintPhase

    token = pair.token_address or ""
    m5, h1, h24, age_h, turn, _b = _eval_ctx(
        pair, turnover=turnover, age_hours=age_hours, wallet_count=wallet_count
    )

    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()
    record_btc_m15_snapshot(m15)
    blocked, macro_note = btc_macro_blocks_entry(m15)
    if blocked:
        return False, f"faz2 tier1 {macro_note}"

    halted, halt_note = entry_halted()
    if halted:
        return False, f"faz2 tier1 {halt_note}"

    if is_guillotine_blacklisted(token):
        return False, "faz2 tier1 guillotine blacklist (oturum)"

    age_min = pair_age_minutes(pair, age_h)
    if age_min is None:
        return False, "faz2 tier1 age bilinmiyor"

    phase = mint_phase(pair)
    if phase == MintPhase.FAZ3:
        return False, f"faz2 tier1 FAZ3 geç chase red (age {age_min:.0f}dk)"

    if is_pump_blacklisted(token):
        decay_ok = phase2_pump_bl_decay_ok(
            token, age_min, decay_min=giyotin_phase2_pump_bl_decay_min()
        )
        if not decay_ok:
            return False, "faz2 tier1 pump blacklist (oturum)"

    if is_sym_on_cooldown(token):
        return False, f"faz2 tier1 sym cooldown {giyotin_sym_cooldown_sec()/60:.0f}dk"

    age_lo = giyotin_phase2_age_min_sec() / 60.0
    age_hi = giyotin_phase2_age_max_sec() / 60.0
    if age_min < age_lo:
        return False, f"faz2 tier1 age {age_min:.0f}dk < {age_lo:.0f}dk"
    if age_min > age_hi:
        return False, f"faz2 tier1 age {age_min:.0f}dk > {age_hi:.0f}dk"

    if wallet_count < giyotin_holders_min():
        return False, f"faz2 tier1 cüzdan {wallet_count} < {giyotin_holders_min()}"

    liq = float(pair.liquidity_usd or 0)
    liq_lo = giyotin_phase2_liq_min()
    liq_hi = giyotin_phase2_liq_max()
    if liq < liq_lo:
        return False, f"faz2 tier1 liq ${liq:,.0f} < ${liq_lo:,.0f}"
    if liq > liq_hi:
        return False, f"faz2 tier1 liq ${liq:,.0f} > ${liq_hi:,.0f} (mature karışmaz)"

    if not liq_growing(token, liq):
        return False, f"faz2 tier1 liq düşüyor/durgun ${liq:,.0f}"

    h24_min = giyotin_phase2_h24_min()
    if h24 < h24_min:
        return False, f"faz2 tier1 h24 %{h24:.1f} < {h24_min:.0f}%"

    h1_lo = giyotin_phase2_h1_min()
    h1_hi = giyotin_phase2_h1_max()
    if h1 < h1_lo or h1 > h1_hi:
        return False, f"faz2 tier1 h1 %{h1:.1f} ∉ [{h1_lo:.0f},{h1_hi:.0f}]"

    m5_lo = giyotin_phase2_m5_min()
    m5_hi = giyotin_phase2_m5_max()
    if m5 < m5_lo or m5 > m5_hi:
        return False, f"faz2 tier1 m5 %{m5:.1f} ∉ [{m5_lo:.0f},{m5_hi:.0f}]"

    if m5 > giyotin_m5_forbid():
        blacklist_pump_token(token, pool_created_at=pair.pool_created_at)
        return False, f"faz2 tier1 m5 %{m5:.1f} > {giyotin_m5_forbid():.0f}% (blacklist)"

    cap = giyotin_max_position_usd()
    if cap > 0 and position_usd > cap:
        return False, f"faz2 tier1 poz ${position_usd:.0f} > ${cap:.0f}"

    return True, f"faz2 tier1 ✓ · {macro_note}"


def evaluate_giyotin_phase2_hunter(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> GiyotinEvalResult:
    """FAZ2 ikinci dalga — 45dk-5s, orta liq, momentum filtresi."""
    m5, h1, h24, age_h, turn, billy = _eval_ctx(
        pair,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
    )
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    if _is_mature_world_pair(pair, billy):
        return GiyotinEvalResult(
            False,
            f"giyotin faz2 mature world liq≥150k billy≥85 — mature şerit",
            "faz2_mature_world",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    t1_ok, t1_note = evaluate_tier1_phase2(
        pair,
        position_usd,
        turnover=turn,
        age_hours=age_h,
        wallet_count=wallet_count,
        btc_m15=m15,
    )
    if not t1_ok:
        return GiyotinEvalResult(
            False, f"giyotin {t1_note}", "faz2_tier1", billy, 0, False, m5, h1, h24, turn, m15
        )

    score_min = giyotin_phase2_score_min()
    if billy < score_min:
        return GiyotinEvalResult(
            False,
            f"giyotin faz2 score {billy:.0f}<{score_min:.0f}",
            "faz2_score",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    deadly, deadly_note = evaluate_deadly_chase(pair)
    if deadly:
        return GiyotinEvalResult(
            False,
            f"giyotin faz2 {deadly_note}",
            "chase",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            deadly_chase=True,
        )

    return GiyotinEvalResult(
        True,
        f"giyotin faz2 ✓ score={billy:.0f} · {t1_note}",
        None,
        billy,
        billy,
        False,
        m5,
        h1,
        h24,
        turn,
        m15,
        bonus_billy=billy,
        entry_profile="phase2",
        turn_size_scale=1.0,
    )


def _dead_cat_age_window(age_hours: float | None, pair: Pair) -> bool:
    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False
    lo = giyotin_dead_cat_age_min_sec() / 60.0
    hi = giyotin_dead_cat_age_max_sec() / 60.0
    return lo <= age_min <= hi


def _gap_age_window(age_hours: float | None, pair: Pair) -> bool:
    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False
    lo = giyotin_gap_age_min_sec() / 60.0
    hi = giyotin_gap_age_max_sec() / 60.0
    return lo < age_min < hi


def evaluate_tier1_gap(
    pair: Pair,
    position_usd: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> tuple[bool, str]:
    """Geçiş şeridi tier1 — 28–45dk, köprü h1 kaçanı, orta liq."""
    token = pair.token_address or ""
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    h24 = float(pair.chg_h24 or 0)
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    blocked, macro_note = btc_macro_blocks_entry(m15)
    if blocked:
        return False, f"gap tier1 {macro_note}"

    halted, halt_note = entry_halted()
    if halted:
        return False, f"gap tier1 {halt_note}"

    if is_guillotine_blacklisted(token):
        return False, "gap tier1 guillotine blacklist (oturum)"

    if is_pump_blacklisted(token):
        return False, "gap tier1 pump blacklist (oturum)"

    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False, "gap tier1 age bilinmiyor"

    age_lo = giyotin_gap_age_min_sec() / 60.0
    age_hi = giyotin_gap_age_max_sec() / 60.0
    if age_min <= age_lo:
        return False, f"gap tier1 age {age_min:.0f}dk ≤ {age_lo:.0f}dk"
    if age_min >= age_hi:
        return False, f"gap tier1 age {age_min:.0f}dk ≥ {age_hi:.0f}dk"

    if is_sym_on_cooldown(token):
        return False, f"gap tier1 sym cooldown {giyotin_sym_cooldown_sec()/60:.0f}dk"

    if wallet_count < giyotin_holders_min():
        return False, f"gap tier1 cüzdan {wallet_count} < {giyotin_holders_min()}"

    liq = float(pair.liquidity_usd or 0)
    liq_lo = giyotin_gap_liq_min()
    liq_hi = giyotin_gap_liq_max()
    if liq < liq_lo:
        return False, f"gap tier1 liq ${liq:,.0f} < ${liq_lo:,.0f}"
    if liq > liq_hi:
        return False, f"gap tier1 liq ${liq:,.0f} > ${liq_hi:,.0f}"

    h24_min = giyotin_gap_h24_min()
    if h24 < h24_min:
        return False, f"gap tier1 h24 %{h24:.1f} < {h24_min:.0f}%"

    h1_lo = giyotin_gap_h1_min()
    h1_hi = giyotin_gap_h1_max()
    if h1 < h1_lo or h1 > h1_hi:
        return False, f"gap tier1 h1 %{h1:.1f} ∉ [{h1_lo:.0f},{h1_hi:.0f}]"

    m5_lo = giyotin_gap_m5_min()
    m5_hi = giyotin_gap_m5_max()
    if m5 < m5_lo or m5 > m5_hi:
        return False, f"gap tier1 m5 %{m5:.1f} ∉ [{m5_lo:.0f},{m5_hi:.0f}]"

    if m5 > giyotin_m5_forbid():
        blacklist_pump_token(token, pool_created_at=pair.pool_created_at)
        return False, f"gap tier1 m5 %{m5:.1f} > {giyotin_m5_forbid():.0f}% (blacklist)"

    cap = giyotin_max_position_usd()
    if cap > 0 and position_usd > cap:
        return False, f"gap tier1 poz ${position_usd:.0f} > ${cap:.0f}"

    return True, f"gap tier1 ✓ geçiş 28–45dk h1={h1:.1f}% m5={m5:.1f}% · {macro_note}"


def evaluate_tier1_dead_cat(
    pair: Pair,
    position_usd: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> tuple[bool, str]:
    """Ölü kedi tier1 — dump (h1<0) + m5 sıçrama; pump_bl dar waiver."""
    token = pair.token_address or ""
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    h24 = float(pair.chg_h24 or 0)
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    blocked, macro_note = btc_macro_blocks_entry(m15)
    if blocked:
        return False, f"dead_cat tier1 {macro_note}"

    halted, halt_note = entry_halted()
    if halted:
        return False, f"dead_cat tier1 {halt_note}"

    if is_guillotine_blacklisted(token):
        return False, "dead_cat tier1 guillotine blacklist (oturum)"

    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False, "dead_cat tier1 age bilinmiyor"

    if is_pump_blacklisted(token):
        if not dead_cat_pump_bl_waiver(h1, m5):
            return False, "dead_cat tier1 pump blacklist (waiver yok)"

    if is_sym_on_cooldown(token):
        return False, f"dead_cat tier1 sym cooldown {giyotin_sym_cooldown_sec()/60:.0f}dk"

    age_lo = giyotin_dead_cat_age_min_sec() / 60.0
    age_hi = giyotin_dead_cat_age_max_sec() / 60.0
    if age_min < age_lo:
        return False, f"dead_cat tier1 age {age_min:.0f}dk < {age_lo:.0f}dk"
    if age_min > age_hi:
        return False, f"dead_cat tier1 age {age_min:.0f}dk > {age_hi:.0f}dk"

    if wallet_count < giyotin_holders_min():
        return False, f"dead_cat tier1 cüzdan {wallet_count} < {giyotin_holders_min()}"

    liq = float(pair.liquidity_usd or 0)
    liq_lo = giyotin_dead_cat_liq_min()
    liq_hi = giyotin_dead_cat_liq_max()
    if liq < liq_lo:
        return False, f"dead_cat tier1 liq ${liq:,.0f} < ${liq_lo:,.0f}"
    if liq > liq_hi:
        return False, f"dead_cat tier1 liq ${liq:,.0f} > ${liq_hi:,.0f}"

    h24_min = giyotin_dead_cat_h24_min()
    if h24 < h24_min:
        return False, f"dead_cat tier1 h24 %{h24:.1f} < {h24_min:.0f}%"

    h1_max = giyotin_dead_cat_h1_max()
    if h1 > h1_max:
        return False, f"dead_cat tier1 h1 %{h1:.1f} > {h1_max:.0f}% (dump yok)"

    m5_lo = giyotin_dead_cat_m5_min()
    m5_hi = giyotin_dead_cat_m5_max()
    if m5 < m5_lo or m5 > m5_hi:
        return False, f"dead_cat tier1 m5 %{m5:.1f} ∉ [{m5_lo:.0f},{m5_hi:.0f}]"

    if m5 > giyotin_m5_forbid():
        return False, f"dead_cat tier1 m5 %{m5:.1f} > {giyotin_m5_forbid():.0f}% (chase)"

    cap = giyotin_max_position_usd()
    if cap > 0 and position_usd > cap:
        return False, f"dead_cat tier1 poz ${position_usd:.0f} > ${cap:.0f}"

    return True, f"dead_cat tier1 ✓ dump h1={h1:.1f}% m5={m5:.1f}% · {macro_note}"


def evaluate_giyotin_gap_hunter(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> GiyotinEvalResult:
    """28–45dk geçiş şeridi — genesis sonrası, FAZ2 öncesi."""
    m5, h1, h24, age_h, turn, billy = _eval_ctx(
        pair,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
    )
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    liq = float(pair.liquidity_usd or 0)
    if liq >= giyotin_mature_liq_min():
        return GiyotinEvalResult(
            False,
            f"giyotin gap mature liq ${liq:,.0f} ≥ ${giyotin_mature_liq_min():,.0f}",
            "gap_mature",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    t1_ok, t1_note = evaluate_tier1_gap(
        pair,
        position_usd,
        turnover=turn,
        age_hours=age_h,
        wallet_count=wallet_count,
        btc_m15=m15,
    )
    if not t1_ok:
        return GiyotinEvalResult(
            False, f"giyotin {t1_note}", "gap_tier1", billy, 0, False, m5, h1, h24, turn, m15
        )

    score_min = giyotin_gap_score_min()
    if billy < score_min:
        return GiyotinEvalResult(
            False,
            f"giyotin gap score {billy:.0f}<{score_min:.0f}",
            "gap_score",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    deadly, deadly_note = evaluate_deadly_chase(pair)
    if deadly:
        return GiyotinEvalResult(
            False,
            f"giyotin gap {deadly_note}",
            "chase",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            deadly_chase=True,
        )

    return GiyotinEvalResult(
        True,
        f"giyotin gap ✓ score={billy:.0f} · {t1_note}",
        None,
        billy,
        billy,
        False,
        m5,
        h1,
        h24,
        turn,
        m15,
        bonus_billy=billy,
        entry_profile="gap",
        turn_size_scale=1.0,
    )


def evaluate_giyotin_dead_cat_hunter(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> GiyotinEvalResult:
    """Ölü kedi sıçraması — h1 dump + m5 toparlanma scalp şeridi."""
    m5, h1, h24, age_h, turn, billy = _eval_ctx(
        pair,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
    )
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    if _is_mature_world_pair(pair, billy):
        return GiyotinEvalResult(
            False,
            "giyotin dead_cat mature world — mature şerit",
            "dead_cat_mature",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    t1_ok, t1_note = evaluate_tier1_dead_cat(
        pair,
        position_usd,
        turnover=turn,
        age_hours=age_h,
        wallet_count=wallet_count,
        btc_m15=m15,
    )
    if not t1_ok:
        return GiyotinEvalResult(
            False, f"giyotin {t1_note}", "dead_cat_tier1", billy, 0, False, m5, h1, h24, turn, m15
        )

    score_min = giyotin_dead_cat_score_min()
    if billy < score_min:
        return GiyotinEvalResult(
            False,
            f"giyotin dead_cat score {billy:.0f}<{score_min:.0f}",
            "dead_cat_score",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    deadly, deadly_note = evaluate_deadly_chase(pair)
    if deadly:
        return GiyotinEvalResult(
            False,
            f"giyotin dead_cat {deadly_note}",
            "chase",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            deadly_chase=True,
        )

    return GiyotinEvalResult(
        True,
        f"giyotin dead_cat ✓ score={billy:.0f} · {t1_note}",
        None,
        billy,
        billy,
        False,
        m5,
        h1,
        h24,
        turn,
        m15,
        bonus_billy=billy,
        entry_profile="dead_cat",
        turn_size_scale=1.0,
    )


def _wallet2_age_window(age_hours: float | None, pair: Pair) -> bool:
    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False
    lo = giyotin_wallet2_age_min_sec() / 60.0
    return age_min >= lo


def _revival_age_window(age_hours: float | None, pair: Pair) -> bool:
    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False
    lo = giyotin_revival_age_min_sec() / 60.0
    return age_min >= lo


def evaluate_tier1_wallet2(
    pair: Pair,
    position_usd: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> tuple[bool, str]:
    """2-cüzdan mature scalp tier1 — wallet_count tam 2, orta liq/h1/m5."""
    token = pair.token_address or ""
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    blocked, macro_note = btc_macro_blocks_entry(m15)
    if blocked:
        return False, f"wallet2 tier1 {macro_note}"

    halted, halt_note = entry_halted()
    if halted:
        return False, f"wallet2 tier1 {halt_note}"

    if is_guillotine_blacklisted(token):
        return False, "wallet2 tier1 guillotine blacklist (oturum)"

    if is_pump_blacklisted(token):
        return False, "wallet2 tier1 pump blacklist (oturum)"

    if is_sym_on_cooldown(token):
        return False, f"wallet2 tier1 sym cooldown {giyotin_sym_cooldown_sec()/60:.0f}dk"

    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False, "wallet2 tier1 age bilinmiyor"

    age_lo = giyotin_wallet2_age_min_sec() / 60.0
    if age_min < age_lo:
        return False, f"wallet2 tier1 age {age_min:.0f}dk < {age_lo:.0f}dk"

    if wallet_count != 2:
        return False, f"wallet2 tier1 cüzdan {wallet_count} ≠ 2"

    liq = float(pair.liquidity_usd or 0)
    liq_lo = giyotin_wallet2_liq_min()
    if liq < liq_lo:
        return False, f"wallet2 tier1 liq ${liq:,.0f} < ${liq_lo:,.0f}"

    h1_lo = giyotin_wallet2_h1_min()
    h1_hi = giyotin_wallet2_h1_max()
    if h1 < h1_lo or h1 > h1_hi:
        return False, f"wallet2 tier1 h1 %{h1:.1f} ∉ [{h1_lo:.0f},{h1_hi:.0f}]"

    m5_lo = giyotin_wallet2_m5_min()
    m5_hi = giyotin_wallet2_m5_max()
    if m5 < m5_lo or m5 > m5_hi:
        return False, f"wallet2 tier1 m5 %{m5:.1f} ∉ [{m5_lo:.0f},{m5_hi:.0f}]"

    if m5 > giyotin_m5_forbid():
        return False, f"wallet2 tier1 m5 %{m5:.1f} > {giyotin_m5_forbid():.0f}% (chase)"

    cap = giyotin_max_position_usd()
    if cap > 0 and position_usd > cap:
        return False, f"wallet2 tier1 poz ${position_usd:.0f} > ${cap:.0f}"

    return True, f"wallet2 tier1 ✓ 2-cüzdan h1={h1:.1f}% m5={m5:.1f}% · {macro_note}"


def evaluate_tier1_revival(
    pair: Pair,
    position_usd: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> tuple[bool, str]:
    """Revival tier1 — mature PFP momentum, yüksek liq + cüzdan kümesi."""
    token = pair.token_address or ""
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    blocked, macro_note = btc_macro_blocks_entry(m15)
    if blocked:
        return False, f"revival tier1 {macro_note}"

    halted, halt_note = entry_halted()
    if halted:
        return False, f"revival tier1 {halt_note}"

    if is_guillotine_blacklisted(token):
        return False, "revival tier1 guillotine blacklist (oturum)"

    if is_pump_blacklisted(token):
        return False, "revival tier1 pump blacklist (oturum)"

    if is_sym_on_cooldown(token):
        return False, f"revival tier1 sym cooldown {giyotin_sym_cooldown_sec()/60:.0f}dk"

    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False, "revival tier1 age bilinmiyor"

    age_lo = giyotin_revival_age_min_sec() / 60.0
    if age_min < age_lo:
        return False, f"revival tier1 age {age_min:.0f}dk < {age_lo:.0f}dk"

    wallet_min = giyotin_revival_wallet_min()
    if wallet_count < wallet_min:
        return False, f"revival tier1 cüzdan {wallet_count} < {wallet_min}"

    liq = float(pair.liquidity_usd or 0)
    liq_lo = giyotin_revival_liq_min()
    if liq < liq_lo:
        return False, f"revival tier1 liq ${liq:,.0f} < ${liq_lo:,.0f}"

    h1_min = giyotin_revival_h1_min()
    if h1 < h1_min:
        return False, f"revival tier1 h1 %{h1:.1f} < {h1_min:.0f}%"

    m5_min = giyotin_revival_m5_min()
    if m5 < m5_min:
        return False, f"revival tier1 m5 %{m5:.1f} < {m5_min:.0f}%"

    if m5 > giyotin_m5_forbid():
        return False, f"revival tier1 m5 %{m5:.1f} > {giyotin_m5_forbid():.0f}% (chase)"

    cap = giyotin_max_position_usd()
    if cap > 0 and position_usd > cap:
        return False, f"revival tier1 poz ${position_usd:.0f} > ${cap:.0f}"

    return True, f"revival tier1 ✓ PFP h1={h1:.1f}% m5={m5:.1f}% · {macro_note}"


def _runner_scout_age_window(age_hours: float | None, pair: Pair) -> bool:
    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False
    lo = giyotin_runner_scout_age_min_sec() / 60.0
    hi = giyotin_runner_scout_age_max_sec() / 60.0
    return lo <= age_min <= hi


def evaluate_tier1_runner_scout(
    pair: Pair,
    position_usd: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> tuple[bool, str]:
    """RUNNER_SCOUT tier1 — pump_bl zorunlu micro chase."""
    token = pair.token_address or ""
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    blocked, macro_note = btc_macro_blocks_entry(m15)
    if blocked:
        return False, f"runner_scout tier1 {macro_note}"

    halted, halt_note = entry_halted()
    if halted:
        return False, f"runner_scout tier1 {halt_note}"

    if is_guillotine_blacklisted(token):
        return False, "runner_scout tier1 guillotine blacklist (oturum)"

    if not is_pump_blacklisted(token):
        return False, "runner_scout tier1 pump_bl yok (şerit yalnız pump)"

    if is_sym_on_cooldown(token):
        return False, f"runner_scout tier1 sym cooldown {giyotin_sym_cooldown_sec()/60:.0f}dk"

    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False, "runner_scout tier1 age bilinmiyor"

    age_lo = giyotin_runner_scout_age_min_sec() / 60.0
    age_hi = giyotin_runner_scout_age_max_sec() / 60.0
    if age_min < age_lo or age_min > age_hi:
        return False, f"runner_scout tier1 age {age_min:.0f}dk ∉ [{age_lo:.0f},{age_hi:.0f}]"

    wallet_min = giyotin_runner_scout_wallet_min()
    if wallet_count < wallet_min:
        return False, f"runner_scout tier1 cüzdan {wallet_count} < {wallet_min}"

    liq = float(pair.liquidity_usd or 0)
    liq_lo = giyotin_runner_scout_liq_min()
    liq_hi = giyotin_runner_scout_liq_max()
    if liq < liq_lo:
        return False, f"runner_scout tier1 liq ${liq:,.0f} < ${liq_lo:,.0f}"
    if liq > liq_hi:
        return False, f"runner_scout tier1 liq ${liq:,.0f} > ${liq_hi:,.0f}"

    h1_lo = giyotin_runner_scout_h1_min()
    h1_hi = giyotin_runner_scout_h1_max()
    if h1 < h1_lo or h1 > h1_hi:
        return False, f"runner_scout tier1 h1 %{h1:.1f} ∉ [{h1_lo:.0f},{h1_hi:.0f}]"

    m5_lo = giyotin_runner_scout_m5_min()
    m5_hi = giyotin_runner_scout_m5_max()
    if m5 < m5_lo or m5 > m5_hi:
        return False, f"runner_scout tier1 m5 %{m5:.1f} ∉ [{m5_lo:.0f},{m5_hi:.0f}]"

    if m5 > giyotin_m5_forbid():
        return False, f"runner_scout tier1 m5 %{m5:.1f} > {giyotin_m5_forbid():.0f}% (chase)"

    cap = giyotin_max_position_usd()
    if cap > 0 and position_usd > cap:
        return False, f"runner_scout tier1 poz ${position_usd:.0f} > ${cap:.0f}"

    return True, f"runner_scout tier1 ✓ pump_bl micro h1={h1:.1f}% m5={m5:.1f}% · {macro_note}"


def evaluate_giyotin_runner_scout_hunter(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> GiyotinEvalResult:
    """pump_bl micro chase — küçük poz, sıkı filtre."""
    m5, h1, h24, age_h, turn, billy = _eval_ctx(
        pair,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
    )
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    t1_ok, t1_note = evaluate_tier1_runner_scout(
        pair,
        position_usd,
        turnover=turn,
        age_hours=age_h,
        wallet_count=wallet_count,
        btc_m15=m15,
    )
    if not t1_ok:
        return GiyotinEvalResult(
            False, f"giyotin {t1_note}", "runner_scout_tier1", billy, 0, False, m5, h1, h24, turn, m15
        )

    score_min = giyotin_runner_scout_score_min()
    if billy < score_min:
        return GiyotinEvalResult(
            False,
            f"giyotin runner_scout score {billy:.0f}<{score_min:.0f}",
            "runner_scout_score",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    deadly, deadly_note = evaluate_deadly_chase(pair)
    if deadly:
        return GiyotinEvalResult(
            False,
            f"giyotin runner_scout {deadly_note}",
            "chase",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            deadly_chase=True,
        )

    return GiyotinEvalResult(
        True,
        f"giyotin runner_scout ✓ score={billy:.0f} · {t1_note}",
        None,
        billy,
        billy,
        False,
        m5,
        h1,
        h24,
        turn,
        m15,
        bonus_billy=billy,
        entry_profile="runner_scout",
        turn_size_scale=1.0,
    )


def _alpha_mimic_age_window(age_hours: float | None, pair: Pair) -> bool:
    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False
    lo = giyotin_alpha_mimic_age_min_sec() / 60.0
    hi = giyotin_alpha_mimic_age_max_sec() / 60.0
    return lo <= age_min <= hi


def evaluate_tier1_alpha_mimic(
    pair: Pair,
    position_usd: float,
    *,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
    kol_buy: dict | None = None,
) -> tuple[bool, str]:
    """ALPHA_MIMIC tier1 — whitelist KOL son BUY zorunlu."""
    token = pair.token_address or ""
    m5 = float(pair.chg_m5 or 0)
    h1 = float(pair.chg_h1 or 0)
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    if not kol_buy:
        return False, "alpha_mimic tier1 KOL alım yok (whitelist pencere)"

    blocked, macro_note = btc_macro_blocks_entry(m15)
    if blocked:
        return False, f"alpha_mimic tier1 {macro_note}"

    halted, halt_note = entry_halted()
    if halted:
        return False, f"alpha_mimic tier1 {halt_note}"

    if is_guillotine_blacklisted(token):
        return False, "alpha_mimic tier1 guillotine blacklist (oturum)"

    if is_sym_on_cooldown(token):
        return False, f"alpha_mimic tier1 sym cooldown {giyotin_sym_cooldown_sec()/60:.0f}dk"

    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False, "alpha_mimic tier1 age bilinmiyor"

    age_lo = giyotin_alpha_mimic_age_min_sec() / 60.0
    age_hi = giyotin_alpha_mimic_age_max_sec() / 60.0
    if age_min < age_lo or age_min > age_hi:
        return False, f"alpha_mimic tier1 age {age_min:.0f}dk ∉ [{age_lo:.0f},{age_hi:.0f}]"

    liq = float(pair.liquidity_usd or 0)
    liq_lo = giyotin_alpha_mimic_liq_min()
    liq_hi = giyotin_alpha_mimic_liq_max()
    if liq < liq_lo:
        return False, f"alpha_mimic tier1 liq ${liq:,.0f} < ${liq_lo:,.0f}"
    if liq > liq_hi:
        return False, f"alpha_mimic tier1 liq ${liq:,.0f} > ${liq_hi:,.0f}"

    h1_lo = giyotin_alpha_mimic_h1_min()
    h1_hi = giyotin_alpha_mimic_h1_max()
    if h1 < h1_lo or h1 > h1_hi:
        return False, f"alpha_mimic tier1 h1 %{h1:.1f} ∉ [{h1_lo:.0f},{h1_hi:.0f}]"

    m5_lo = giyotin_alpha_mimic_m5_min()
    m5_hi = giyotin_alpha_mimic_m5_max()
    if m5 < m5_lo or m5 > m5_hi:
        return False, f"alpha_mimic tier1 m5 %{m5:.1f} ∉ [{m5_lo:.0f},{m5_hi:.0f}]"

    if m5 > giyotin_m5_forbid():
        return False, f"alpha_mimic tier1 m5 %{m5:.1f} > {giyotin_m5_forbid():.0f}% (chase)"

    cap = giyotin_max_position_usd()
    if cap > 0 and position_usd > cap:
        return False, f"alpha_mimic tier1 poz ${position_usd:.0f} > ${cap:.0f}"

    kol_name = kol_buy.get("name") or kol_buy.get("wallet", "")[:8]
    kol_age = int(kol_buy.get("age_sec") or 0)
    return True, f"alpha_mimic tier1 ✓ KOL {kol_name} {kol_age}s önce aldı · {macro_note}"


def evaluate_giyotin_alpha_mimic_hunter(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
    kol_buy: dict | None = None,
) -> GiyotinEvalResult:
    """Top KOL alım event taklit — $20 micro."""
    m5, h1, h24, age_h, turn, billy = _eval_ctx(
        pair,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
    )
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    t1_ok, t1_note = evaluate_tier1_alpha_mimic(
        pair,
        position_usd,
        turnover=turn,
        age_hours=age_h,
        wallet_count=wallet_count,
        btc_m15=m15,
        kol_buy=kol_buy,
    )
    if not t1_ok:
        return GiyotinEvalResult(
            False, f"giyotin {t1_note}", "alpha_mimic_tier1", billy, 0, False, m5, h1, h24, turn, m15
        )

    score_min = giyotin_alpha_mimic_score_min()
    if billy < score_min:
        return GiyotinEvalResult(
            False,
            f"giyotin alpha_mimic score {billy:.0f}<{score_min:.0f}",
            "alpha_mimic_score",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    deadly, deadly_note = evaluate_deadly_chase(pair)
    if deadly:
        return GiyotinEvalResult(
            False,
            f"giyotin alpha_mimic {deadly_note}",
            "chase",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            deadly_chase=True,
        )

    return GiyotinEvalResult(
        True,
        f"giyotin alpha_mimic ✓ score={billy:.0f} · {t1_note}",
        None,
        billy,
        billy,
        False,
        m5,
        h1,
        h24,
        turn,
        m15,
        bonus_billy=billy,
        entry_profile="alpha_mimic",
        turn_size_scale=1.0,
    )


def evaluate_giyotin_wallet2_hunter(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> GiyotinEvalResult:
    """2-cüzdan mature scalp — veto counterfactual birincil EV şeridi."""
    m5, h1, h24, age_h, turn, billy = _eval_ctx(
        pair,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
    )
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    t1_ok, t1_note = evaluate_tier1_wallet2(
        pair,
        position_usd,
        turnover=turn,
        age_hours=age_h,
        wallet_count=wallet_count,
        btc_m15=m15,
    )
    if not t1_ok:
        return GiyotinEvalResult(
            False, f"giyotin {t1_note}", "wallet2_tier1", billy, 0, False, m5, h1, h24, turn, m15
        )

    score_min = giyotin_wallet2_score_min()
    if billy < score_min:
        return GiyotinEvalResult(
            False,
            f"giyotin wallet2 score {billy:.0f}<{score_min:.0f}",
            "wallet2_score",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    deadly, deadly_note = evaluate_deadly_chase(pair)
    if deadly:
        return GiyotinEvalResult(
            False,
            f"giyotin wallet2 {deadly_note}",
            "chase",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            deadly_chase=True,
        )

    return GiyotinEvalResult(
        True,
        f"giyotin wallet2 ✓ score={billy:.0f} · {t1_note}",
        None,
        billy,
        billy,
        False,
        m5,
        h1,
        h24,
        turn,
        m15,
        bonus_billy=billy,
        entry_profile="wallet2",
        turn_size_scale=1.0,
    )


def evaluate_giyotin_revival_hunter(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> GiyotinEvalResult:
    """PFP mature momentum pickup — profil A/B zorunlu."""
    m5, h1, h24, age_h, turn, billy = _eval_ctx(
        pair,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
    )
    m15 = btc_m15 if btc_m15 is not None else fetch_btc_m15_pct()

    t1_ok, t1_note = evaluate_tier1_revival(
        pair,
        position_usd,
        turnover=turn,
        age_hours=age_h,
        wallet_count=wallet_count,
        btc_m15=m15,
    )
    if not t1_ok:
        return GiyotinEvalResult(
            False, f"giyotin {t1_note}", "revival_tier1", billy, 0, False, m5, h1, h24, turn, m15
        )

    score_min = giyotin_revival_score_min_for(wallet_count)
    if billy < score_min:
        return GiyotinEvalResult(
            False,
            f"giyotin revival score {billy:.0f}<{score_min:.0f}",
            "revival_score",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
        )

    a_ok, a_checks, a_note = evaluate_profile_a(pair, billy, turnover=turn)
    b_ok, b_checks, b_note = evaluate_profile_b(pair, billy, turnover=turn)
    if not a_ok and not b_ok:
        return GiyotinEvalResult(
            False,
            f"giyotin revival profil yok · {a_note} · {b_note}",
            "revival_profile",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            profile_a_ok=a_ok,
            profile_b_ok=b_ok,
            tier2_parts={"a": a_checks, "b": b_checks},
        )

    deadly, deadly_note = evaluate_deadly_chase(pair)
    if deadly:
        return GiyotinEvalResult(
            False,
            f"giyotin revival {deadly_note}",
            "chase",
            billy,
            0,
            False,
            m5,
            h1,
            h24,
            turn,
            m15,
            deadly_chase=True,
        )

    return GiyotinEvalResult(
        True,
        f"giyotin revival ✓ score={billy:.0f} · {t1_note}",
        None,
        billy,
        billy,
        False,
        m5,
        h1,
        h24,
        turn,
        m15,
        profile_a_ok=a_ok,
        profile_b_ok=b_ok,
        bonus_billy=billy,
        entry_profile="revival",
        turn_size_scale=1.0,
    )


def _genesis_age_window(age_hours: float | None, pair: Pair) -> bool:
    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False
    lo = giyotin_genesis_age_min_sec() / 60.0
    hi = giyotin_genesis_age_max_sec() / 60.0
    return lo <= age_min <= hi


def _bridge_age_window(age_hours: float | None, pair: Pair) -> bool:
    age_min = pair_age_minutes(pair, age_hours)
    if age_min is None:
        return False
    lo = giyotin_bridge_age_min_sec() / 60.0
    hi = giyotin_bridge_age_max_sec() / 60.0
    return lo < age_min <= hi


def evaluate_giyotin_entry(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
    genesis_open_count: int = 0,
    bridge_open_count: int = 0,
    phase2_open_count: int = 0,
    dead_cat_open_count: int = 0,
    gap_open_count: int = 0,
    wallet2_open_count: int = 0,
    revival_open_count: int = 0,
    runner_scout_open_count: int = 0,
    alpha_mimic_open_count: int = 0,
) -> GiyotinEvalResult:
    """Mature v5 → revival → runner_scout → alpha_mimic → genesis → … → wallet2 → red."""
    mature = evaluate_giyotin_hunter(
        pair,
        position_usd,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
        btc_m15=btc_m15,
    )
    if mature.ok:
        return mature

    last = mature
    if giyotin_revival_lane_enabled() and _revival_age_window(age_hours, pair):
        max_rev = giyotin_revival_max_open()
        if revival_open_count >= max_rev:
            last = GiyotinEvalResult(
                False,
                f"giyotin revival slot dolu ({revival_open_count}/{max_rev}) · {mature.reason}",
                "revival_slot",
                mature.billy,
                0,
                False,
                mature.m5,
                mature.h1,
                mature.h24,
                mature.turn,
                mature.btc_m15,
            )
        else:
            rev_pos = giyotin_revival_position_usd()
            last = evaluate_giyotin_revival_hunter(
                pair,
                rev_pos,
                moonshot_score=moonshot_score,
                turnover=turnover,
                age_hours=age_hours,
                wallet_count=wallet_count,
                btc_m15=btc_m15,
            )
            if last.ok:
                return last

    if giyotin_runner_scout_lane_enabled() and _runner_scout_age_window(age_hours, pair):
        max_rs = giyotin_runner_scout_max_open()
        if runner_scout_open_count >= max_rs:
            last = GiyotinEvalResult(
                False,
                f"giyotin runner_scout slot dolu ({runner_scout_open_count}/{max_rs}) · {last.reason}",
                "runner_scout_slot",
                last.billy,
                0,
                False,
                last.m5,
                last.h1,
                last.h24,
                last.turn,
                last.btc_m15,
            )
        else:
            rs_pos = giyotin_runner_scout_position_usd()
            last = evaluate_giyotin_runner_scout_hunter(
                pair,
                rs_pos,
                moonshot_score=moonshot_score,
                turnover=turnover,
                age_hours=age_hours,
                wallet_count=wallet_count,
                btc_m15=btc_m15,
            )
            if last.ok:
                return last

    if giyotin_alpha_mimic_lane_enabled() and _alpha_mimic_age_window(age_hours, pair):
        from hibrit_trader.alpha_mimic import recent_whitelist_buy

        kol_buy = recent_whitelist_buy(pair.token_address or "")
        max_am = giyotin_alpha_mimic_max_open()
        if alpha_mimic_open_count >= max_am:
            last = GiyotinEvalResult(
                False,
                f"giyotin alpha_mimic slot dolu ({alpha_mimic_open_count}/{max_am}) · {last.reason}",
                "alpha_mimic_slot",
                last.billy,
                0,
                False,
                last.m5,
                last.h1,
                last.h24,
                last.turn,
                last.btc_m15,
            )
        elif not kol_buy:
            last = GiyotinEvalResult(
                False,
                f"giyotin alpha_mimic whitelist KOL alım yok · {last.reason}",
                "alpha_mimic_kol",
                last.billy,
                0,
                False,
                last.m5,
                last.h1,
                last.h24,
                last.turn,
                last.btc_m15,
            )
        else:
            am_pos = giyotin_alpha_mimic_position_usd()
            last = evaluate_giyotin_alpha_mimic_hunter(
                pair,
                am_pos,
                moonshot_score=moonshot_score,
                turnover=turnover,
                age_hours=age_hours,
                wallet_count=wallet_count,
                btc_m15=btc_m15,
                kol_buy=kol_buy,
            )
            if last.ok:
                return last

    if giyotin_genesis_lane_enabled() and _genesis_age_window(age_hours, pair):
        max_gen = giyotin_genesis_max_open()
        if genesis_open_count >= max_gen:
            last = GiyotinEvalResult(
                False,
                f"giyotin genesis slot dolu ({genesis_open_count}/{max_gen}) · {mature.reason}",
                "genesis_slot",
                mature.billy,
                0,
                False,
                mature.m5,
                mature.h1,
                mature.h24,
                mature.turn,
                mature.btc_m15,
            )
        else:
            last = evaluate_giyotin_genesis_hunter(
                pair,
                position_usd,
                moonshot_score=moonshot_score,
                turnover=turnover,
                age_hours=age_hours,
                wallet_count=wallet_count,
                btc_m15=btc_m15,
            )
            if last.ok:
                return last

    if giyotin_bridge_lane_enabled() and _bridge_age_window(age_hours, pair):
        bridge_pos = giyotin_bridge_position_usd()
        max_br = giyotin_bridge_max_open()
        if bridge_open_count >= max_br:
            last = GiyotinEvalResult(
                False,
                f"giyotin bridge slot dolu ({bridge_open_count}/{max_br}) · {last.reason}",
                "bridge_slot",
                last.billy,
                0,
                False,
                last.m5,
                last.h1,
                last.h24,
                last.turn,
                last.btc_m15,
            )
        else:
            last = evaluate_giyotin_bridge_hunter(
                pair,
                bridge_pos,
                moonshot_score=moonshot_score,
                turnover=turnover,
                age_hours=age_hours,
                wallet_count=wallet_count,
                btc_m15=btc_m15,
            )
            if last.ok:
                return last

    if giyotin_gap_lane_enabled() and _gap_age_window(age_hours, pair):
        max_gap = giyotin_gap_max_open()
        if gap_open_count >= max_gap:
            last = GiyotinEvalResult(
                False,
                f"giyotin gap slot dolu ({gap_open_count}/{max_gap}) · {last.reason}",
                "gap_slot",
                last.billy,
                0,
                False,
                last.m5,
                last.h1,
                last.h24,
                last.turn,
                last.btc_m15,
            )
        else:
            gap_pos = giyotin_gap_position_usd()
            last = evaluate_giyotin_gap_hunter(
                pair,
                gap_pos,
                moonshot_score=moonshot_score,
                turnover=turnover,
                age_hours=age_hours,
                wallet_count=wallet_count,
                btc_m15=btc_m15,
            )
            if last.ok:
                return last

    if giyotin_phase2_lane_enabled() and _phase2_age_window(age_hours, pair):
        max_p2 = giyotin_phase2_max_open()
        if phase2_open_count >= max_p2:
            last = GiyotinEvalResult(
                False,
                f"giyotin faz2 slot dolu ({phase2_open_count}/{max_p2}) · {last.reason}",
                "faz2_slot",
                last.billy,
                0,
                False,
                last.m5,
                last.h1,
                last.h24,
                last.turn,
                last.btc_m15,
            )
        else:
            last = evaluate_giyotin_phase2_hunter(
                pair,
                position_usd,
                moonshot_score=moonshot_score,
                turnover=turnover,
                age_hours=age_hours,
                wallet_count=wallet_count,
                btc_m15=btc_m15,
            )
            if last.ok:
                return last

    if giyotin_dead_cat_lane_enabled() and _dead_cat_age_window(age_hours, pair):
        max_dc = giyotin_dead_cat_max_open()
        if dead_cat_open_count >= max_dc:
            last = GiyotinEvalResult(
                False,
                f"giyotin dead_cat slot dolu ({dead_cat_open_count}/{max_dc}) · {last.reason}",
                "dead_cat_slot",
                last.billy,
                0,
                False,
                last.m5,
                last.h1,
                last.h24,
                last.turn,
                last.btc_m15,
            )
        else:
            dc_pos = giyotin_dead_cat_position_usd()
            last = evaluate_giyotin_dead_cat_hunter(
                pair,
                dc_pos,
                moonshot_score=moonshot_score,
                turnover=turnover,
                age_hours=age_hours,
                wallet_count=wallet_count,
                btc_m15=btc_m15,
            )
            if last.ok:
                return last

    if giyotin_wallet2_lane_enabled() and _wallet2_age_window(age_hours, pair):
        max_w2 = giyotin_wallet2_max_open()
        if wallet2_open_count >= max_w2:
            last = GiyotinEvalResult(
                False,
                f"giyotin wallet2 slot dolu ({wallet2_open_count}/{max_w2}) · {last.reason}",
                "wallet2_slot",
                last.billy,
                0,
                False,
                last.m5,
                last.h1,
                last.h24,
                last.turn,
                last.btc_m15,
            )
        else:
            w2_pos = giyotin_wallet2_position_usd()
            last = evaluate_giyotin_wallet2_hunter(
                pair,
                w2_pos,
                moonshot_score=moonshot_score,
                turnover=turnover,
                age_hours=age_hours,
                wallet_count=wallet_count,
                btc_m15=btc_m15,
            )
            if last.ok:
                return last

    return last


@dataclass
class GiyotinEvalDetail:
    ok: bool
    reason: str
    tier_failed: str | None
    tier2_score: float
    altin_ok: bool
    altin_checks: dict[str, bool]
    billy: float
    toxic: bool = False
    profile_a_ok: bool = False
    profile_b_ok: bool = False
    profile_a_checks: dict[str, bool] = field(default_factory=dict)
    profile_b_checks: dict[str, bool] = field(default_factory=dict)
    deadly_chase: bool = False
    bonus_billy: float = 0.0
    entry_profile: str | None = None


def giyotin_eval_detail(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    btc_m15: float | None = None,
) -> GiyotinEvalDetail:
    r = evaluate_giyotin_entry(
        pair,
        position_usd,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
        btc_m15=btc_m15,
    )
    turn = float(turnover if turnover is not None else pair_turnover_estimate(pair))
    a_ok, a_checks, _ = evaluate_profile_a(pair, r.billy, turnover=turn)
    b_ok, b_checks, _ = evaluate_profile_b(pair, r.billy, turnover=turn)
    deadly, _ = evaluate_deadly_chase(pair)
    altin_checks = {
        "profil_a": a_ok,
        "profil_b": b_ok,
        "bonus_billy": r.bonus_billy >= giyotin_bonus_entry_min(),
    }
    return GiyotinEvalDetail(
        ok=r.ok,
        reason=r.reason,
        tier_failed=r.tier_failed,
        tier2_score=r.tier2_score,
        altin_ok=r.golden_ok,
        altin_checks=altin_checks,
        billy=r.billy,
        toxic=deadly,
        profile_a_ok=a_ok,
        profile_b_ok=b_ok,
        profile_a_checks=a_checks,
        profile_b_checks=b_checks,
        deadly_chase=deadly,
        bonus_billy=r.bonus_billy,
        entry_profile=r.entry_profile,
    )


def giyotin_entry_ok(
    pair: Pair,
    position_usd: float,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
    wallet_count: int = 0,
    daily_loss_blocked: bool = False,
    genesis_open_count: int = 0,
    bridge_open_count: int = 0,
    phase2_open_count: int = 0,
    dead_cat_open_count: int = 0,
    gap_open_count: int = 0,
    wallet2_open_count: int = 0,
    revival_open_count: int = 0,
    runner_scout_open_count: int = 0,
    alpha_mimic_open_count: int = 0,
) -> tuple[bool, str]:
    if not giyotin_mode_enabled():
        return True, "giyotin off"
    if daily_loss_blocked:
        return False, "giyotin tier1 günlük zarar limiti aşıldı — giriş durdu"
    r = evaluate_giyotin_entry(
        pair,
        position_usd,
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=wallet_count,
        genesis_open_count=genesis_open_count,
        bridge_open_count=bridge_open_count,
        phase2_open_count=phase2_open_count,
        dead_cat_open_count=dead_cat_open_count,
        gap_open_count=gap_open_count,
        wallet2_open_count=wallet2_open_count,
        revival_open_count=revival_open_count,
        runner_scout_open_count=runner_scout_open_count,
        alpha_mimic_open_count=alpha_mimic_open_count,
    )
    return r.ok, r.reason


def giyotin_max_open() -> int:
    return _i("GIYOTIN_MAX_OPEN", "5")


def get_open_billy_scores() -> dict[str, float]:
    """pool_address → giriş Billy skoru (state positions)."""
    out: dict[str, float] = {}
    for _tok, row in (_load_session().get("positions") or {}).items():
        pool = str(row.get("pool") or "")
        billy = float(row.get("billy") or 0)
        if pool and billy > 0:
            out[pool] = billy
    return out


def pick_giyotin_rotation_victim(
    positions: list[Position],
    last_prices: dict[str, float],
    *,
    new_billy: float,
    open_billy_scores: dict[str, float] | None = None,
) -> tuple[Position | None, str]:
    scores = open_billy_scores or {}
    if not positions:
        return None, "açık pozisyon yok"
    open_billies = [
        float(scores.get(p.pool_address) or getattr(p, "giyotin_billy", 0) or p.entry_score or 0)
        for p in positions
    ]
    min_billy = min(open_billies) if open_billies else 0.0
    if new_billy <= min_billy:
        return None, f"yeni billy {new_billy:.0f} ≤ min açık {min_billy:.0f}"

    victim = giyotin_rotation_victim(positions, new_billy, last_prices)
    if victim is None:
        return None, "rotation kurban seçilemedi"
    return victim, f"billy {new_billy:.0f}>{min_billy:.0f} · en düşük PnL kurban"


def reconcile_giyotin_state(open_token_addresses: set[str]) -> dict:
    """Saatlik reconcile — broker token set ↔ state positions."""
    open_positions = [
        Position(
            pair_name="",
            chain="solana",
            token_address=t,
            pool_address="",
            entry_price=1.0,
            amount_token=1.0,
            cost_usd=1.0,
            opened_at="",
            entry_score=0.0,
        )
        for t in open_token_addresses
        if t
    ]
    result = maybe_reconcile_giyotin_state(open_positions, force=True)
    return {
        **result,
        "stale_alarm": bool(result.get("alarm")),
    }


def review_adaptive_thresholds(trades_path: str | Path = "data/trades.jsonl") -> None:
    maybe_adaptive_review(trades_path)


def giyotin_watchlist_viable_only() -> bool:
    return giyotin_mode_enabled() and os.getenv("GIYOTIN_WATCHLIST_VIABLE", "1") != "0"


# Legacy aliases (tests / scripts)
def giyotin_runner_filters_enabled() -> bool:
    return giyotin_mode_enabled()


def giyotin_dual_green_mode() -> bool:
    return False


def evaluate_giyotin_runner(
    pair: Pair,
    *,
    moonshot_score: float = 0,
    turnover: float | None = None,
    age_hours: float | None = None,
) -> GiyotinEvalResult:
    r = evaluate_giyotin_hunter(
        pair,
        giyotin_max_position_usd(),
        moonshot_score=moonshot_score,
        turnover=turnover,
        age_hours=age_hours,
        wallet_count=giyotin_holders_min(),
    )
    return r


def giyotin_summary() -> dict | None:
    if not giyotin_mode_enabled():
        return None
    from hibrit_trader.giyotin_drought_guard import drought_guard_config

    data = _load_session()
    adaptive = data.get("adaptive") or {}
    return {
        "mode": "giyotin",
        "engine": _ENGINE,
        "display_version": giyotin_engine_label(),
        "display_subtitle": giyotin_engine_subtitle(),
        "ship_at": ship_timestamp(),
        "watchlist_viable_only": giyotin_watchlist_viable_only(),
        "tier2_min": giyotin_bonus_entry_min(),
        "billy_min": giyotin_a_billy_min(),
        "m5_max": giyotin_a_m5_max(),
        "h1_min": giyotin_a_h1_min(),
        "turn_max": giyotin_a_turn_max(),
        "b_m5_min": giyotin_b_m5_min(),
        "b_h1_min": giyotin_b_h1_min(),
        "b_h24_min": giyotin_b_h24_min(),
        "bonus_entry_min": giyotin_bonus_entry_min(),
        "sym_cooldown_sec": giyotin_sym_cooldown_sec(),
        "adaptive": adaptive,
        "entry_halted_until": data.get("entry_halted_until"),
        "btc_m15_block": float(os.getenv("GIYOTIN_BTC_M15_BLOCK", "-1.5")),
        "btc_m15_last": data.get("btc_m15_last"),
        "liq_min": giyotin_liq_min(),
        "entry_score_min": giyotin_entry_score_min(),
        "turn_tier1_max": giyotin_turn_tier1_max(),
        "turn_tier1_reduce": giyotin_turn_tier1_reduce_at(),
        "max_position_usd": giyotin_max_position_usd(),
        "max_position_pct": giyotin_max_position_pct(),
        "high_conviction_pct": giyotin_high_conviction_pct(),
        "high_conviction_liq_min": giyotin_high_conviction_liq_min(),
        "high_conviction_billy_min": giyotin_high_conviction_billy_min(),
        "hc_guillotine_mfe_pct": giyotin_high_conviction_guillotine_mfe_pct(),
        "hc_guillotine_liq_min": giyotin_hc_guillotine_liq_min(),
        "hc_guillotine_score_min": giyotin_hc_guillotine_score_min(),
        "size_by_reliability": giyotin_size_by_reliability(),
        "hard_stop_pct": giyotin_hard_stop_pct(),
        "volatile_guard": giyotin_volatile_guard_enabled(),
        "volatile_h1_pct": giyotin_volatile_h1_pct(),
        "volatile_m5_pct": giyotin_volatile_m5_pct(),
        "volatile_stop_pct": giyotin_volatile_tight_stop_pct(),
        "volatile_size_scale": giyotin_volatile_size_scale(),
        "drought_guard": drought_guard_config(),
        "guillotine_sec": giyotin_guillotine_sec(),
        "missing_ticks_exit": giyotin_missing_ticks_exit(),
        "guillotine_mfe_pct": giyotin_guillotine_mfe_pct(),
        "be_trigger_pct": giyotin_be_trigger_pct(),
        "trail_arm_mfe_pct": giyotin_trail_arm_mfe_pct(),
        "trail_pct": giyotin_trail_pct(),
        "partial_exit": giyotin_partial_exit_enabled(),
        "trail_partial_frac": giyotin_trail_partial_frac(),
        "runner_trail_pct": giyotin_runner_trail_pct(),
        "runner_min_mfe_pct": giyotin_runner_min_mfe_pct(),
        "runner_floor_pct": giyotin_runner_floor_pct(),
        "runner_profit_giveback_pct": giyotin_runner_profit_giveback_pct(),
        "runner_be_disabled": giyotin_runner_be_disabled(),
        "runner_tp_pct": giyotin_runner_tp_pct(),
        "runner_tp_usd": giyotin_runner_tp_usd(),
        "runner_tp_arm_pnl_pct": giyotin_runner_tp_arm_pnl_pct(),
        "runner_pnl_giveback_pct": giyotin_runner_pnl_giveback_pct(),
        "min_friction_pct": MIN_ROUND_TRIP_FRICTION_PCT,
        "pump_blacklist_n": len(data.get("pump_blacklist") or []),
        "guillotine_blacklist_n": len(data.get("guillotine_blacklist") or []),
        "open_positions_n": len(data.get("positions") or {}),
        "genesis_lane": giyotin_genesis_lane_enabled(),
        "genesis_age_min_sec": giyotin_genesis_age_min_sec(),
        "genesis_age_max_sec": giyotin_genesis_age_max_sec(),
        "genesis_liq_min": giyotin_genesis_liq_min(),
        "genesis_score_min": giyotin_genesis_score_min(),
        "genesis_turn_max": giyotin_genesis_turn_max(),
        "genesis_max_open": giyotin_genesis_max_open(),
        "bridge_lane": giyotin_bridge_lane_enabled(),
        "bridge_age_min_sec": giyotin_bridge_age_min_sec(),
        "bridge_age_max_sec": giyotin_bridge_age_max_sec(),
        "bridge_liq_min": giyotin_bridge_liq_min(),
        "bridge_score_min": giyotin_bridge_score_min(),
        "bridge_h1_min": giyotin_bridge_h1_min(),
        "bridge_h1_max": giyotin_bridge_h1_max(),
        "bridge_m5_min": giyotin_bridge_m5_min(),
        "bridge_m5_max": giyotin_bridge_m5_max(),
        "bridge_turn_max": giyotin_bridge_turn_max(),
        "bridge_max_open": giyotin_bridge_max_open(),
        "bridge_position_usd": giyotin_bridge_position_usd(),
        "phase2_lane": giyotin_phase2_lane_enabled(),
        "phase2_age_min_sec": giyotin_phase2_age_min_sec(),
        "phase2_age_max_sec": giyotin_phase2_age_max_sec(),
        "phase2_liq_min": giyotin_phase2_liq_min(),
        "phase2_liq_max": giyotin_phase2_liq_max(),
        "phase2_h24_min": giyotin_phase2_h24_min(),
        "phase2_max_open": giyotin_phase2_max_open(),
        "mature_liq_min": giyotin_mature_liq_min(),
        "runner_size_boost": giyotin_runner_size_boost_enabled(),
        "runner_size_boost_mult": giyotin_runner_size_boost_mult(),
        "dead_cat_lane": giyotin_dead_cat_lane_enabled(),
        "dead_cat_age_min_sec": giyotin_dead_cat_age_min_sec(),
        "dead_cat_age_max_sec": giyotin_dead_cat_age_max_sec(),
        "dead_cat_h1_max": giyotin_dead_cat_h1_max(),
        "dead_cat_m5_min": giyotin_dead_cat_m5_min(),
        "dead_cat_m5_max": giyotin_dead_cat_m5_max(),
        "dead_cat_max_open": giyotin_dead_cat_max_open(),
        "dead_cat_position_usd": giyotin_dead_cat_position_usd(),
        "dead_cat_hard_stop_pct": giyotin_dead_cat_hard_stop_pct(),
        "gap_lane": giyotin_gap_lane_enabled(),
        "gap_age_min_sec": giyotin_gap_age_min_sec(),
        "gap_age_max_sec": giyotin_gap_age_max_sec(),
        "gap_h1_min": giyotin_gap_h1_min(),
        "gap_h1_max": giyotin_gap_h1_max(),
        "gap_max_open": giyotin_gap_max_open(),
        "gap_position_usd": giyotin_gap_position_usd(),
        "revival_lane": giyotin_revival_lane_enabled(),
        "revival_age_min_sec": giyotin_revival_age_min_sec(),
        "revival_liq_min": giyotin_revival_liq_min(),
        "revival_h1_min": giyotin_revival_h1_min(),
        "revival_m5_min": giyotin_revival_m5_min(),
        "revival_wallet_min": giyotin_revival_wallet_min(),
        "revival_score_min": giyotin_revival_score_min(),
        "revival_wallet_cluster_min": giyotin_revival_wallet_cluster_min(),
        "revival_score_min_cluster": giyotin_revival_score_min_cluster(),
        "revival_max_open": giyotin_revival_max_open(),
        "revival_position_usd": giyotin_revival_position_usd(),
        "revival_guillotine_mfe_pct": giyotin_revival_guillotine_mfe_pct(),
        "wallet2_lane": giyotin_wallet2_lane_enabled(),
        "wallet2_age_min_sec": giyotin_wallet2_age_min_sec(),
        "wallet2_liq_min": giyotin_wallet2_liq_min(),
        "wallet2_h1_min": giyotin_wallet2_h1_min(),
        "wallet2_h1_max": giyotin_wallet2_h1_max(),
        "wallet2_m5_min": giyotin_wallet2_m5_min(),
        "wallet2_m5_max": giyotin_wallet2_m5_max(),
        "wallet2_score_min": giyotin_wallet2_score_min(),
        "wallet2_max_open": giyotin_wallet2_max_open(),
        "wallet2_position_usd": giyotin_wallet2_position_usd(),
        "wallet2_guillotine_mfe_pct": giyotin_wallet2_guillotine_mfe_pct(),
        "runner_scout_lane": giyotin_runner_scout_lane_enabled(),
        "runner_scout_age_min_sec": giyotin_runner_scout_age_min_sec(),
        "runner_scout_age_max_sec": giyotin_runner_scout_age_max_sec(),
        "runner_scout_liq_min": giyotin_runner_scout_liq_min(),
        "runner_scout_liq_max": giyotin_runner_scout_liq_max(),
        "runner_scout_h1_min": giyotin_runner_scout_h1_min(),
        "runner_scout_h1_max": giyotin_runner_scout_h1_max(),
        "runner_scout_score_min": giyotin_runner_scout_score_min(),
        "runner_scout_max_open": giyotin_runner_scout_max_open(),
        "runner_scout_position_usd": giyotin_runner_scout_position_usd(),
        "runner_scout_hard_stop_pct": giyotin_runner_scout_hard_stop_pct(),
        "alpha_mimic_lane": giyotin_alpha_mimic_lane_enabled(),
        "alpha_mimic_window_sec": giyotin_alpha_mimic_window_sec(),
        "alpha_mimic_age_min_sec": giyotin_alpha_mimic_age_min_sec(),
        "alpha_mimic_age_max_sec": giyotin_alpha_mimic_age_max_sec(),
        "alpha_mimic_liq_min": giyotin_alpha_mimic_liq_min(),
        "alpha_mimic_liq_max": giyotin_alpha_mimic_liq_max(),
        "alpha_mimic_score_min": giyotin_alpha_mimic_score_min(),
        "alpha_mimic_max_open": giyotin_alpha_mimic_max_open(),
        "alpha_mimic_position_usd": giyotin_alpha_mimic_position_usd(),
        "alpha_mimic_hard_stop_pct": giyotin_alpha_mimic_hard_stop_pct(),
        "states": [s.value for s in GiyotinState],
        "priority": ["failsafe", "guillotine", "hard_stop", "trailing", "break_even"],
    }


def _position_age_sec(pos: Position) -> float:
    if pos.opened_ts > 0:
        return time.time() - pos.opened_ts
    return 0.0


def _arm_break_even(pos: Position) -> None:
    if pos.breakeven_armed:
        return
    pos.breakeven_armed = True
    transition_giyotin_state(pos, GiyotinState.BREAK_EVEN)


def _arm_trail(pos: Position, price: float) -> None:
    if pos.trail_armed:
        return
    pos.trail_armed = True
    pos.giyotin_trail_peak_usd = max(pos.giyotin_trail_peak_usd, price)
    transition_giyotin_state(pos, GiyotinState.TRAILING)


def _update_trail_peak(pos: Position, price: float) -> None:
    if pos.trail_armed and price > 0:
        if pos.giyotin_trail_peak_usd <= 0:
            pos.giyotin_trail_peak_usd = price
        elif price > pos.giyotin_trail_peak_usd:
            pos.giyotin_trail_peak_usd = price


@dataclass(frozen=True)
class GiyotinExit:
    kind: str
    reason: str
    sell_fraction: float
    tag: ExitTag


def _position_exit_pending(pos: Position) -> bool:
    phase = (pos.giyotin_phase or "").upper()
    return phase in {GiyotinState.EXIT_PENDING.value, GiyotinState.CLOSED.value}


def _update_runner_peak_pnl(pos: Position, pnl: float) -> float:
    peak = float(getattr(pos, "giyotin_runner_peak_pnl", 0) or 0)
    if pnl > peak:
        pos.giyotin_runner_peak_pnl = pnl
        return pnl
    return peak


def _check_runner_green_exit(pos: Position, pnl: float) -> GiyotinExit | None:
    """Kalan bacak tepe kârdayken veya hafif geri çekilmede yeşil kapat."""
    if not getattr(pos, "giyotin_runner_armed", False):
        return None
    floor = giyotin_runner_floor_pct()
    if pnl <= floor:
        return None

    peak_pnl = _update_runner_peak_pnl(pos, pnl)

    tp_pct = giyotin_runner_tp_pct()
    if tp_pct > 0 and pnl >= tp_pct:
        return GiyotinExit(
            "exit_full",
            f"giyotin runner tp {pnl:.1f}%",
            1.0,
            "trailing",
        )

    tp_usd = giyotin_runner_tp_usd()
    if tp_usd > 0 and pos.cost_usd > 0:
        unreal = pos.cost_usd * pnl / 100.0
        if unreal >= tp_usd:
            return GiyotinExit(
                "exit_full",
                f"giyotin runner tp ${unreal:.2f}",
                1.0,
                "trailing",
            )

    arm = giyotin_runner_tp_arm_pnl_pct()
    give = giyotin_runner_pnl_giveback_pct()
    if arm > 0 and give > 0 and peak_pnl >= arm and pnl <= peak_pnl - give:
        return GiyotinExit(
            "exit_full",
            f"giyotin runner pnl take {pnl:.1f}% peak={peak_pnl:.1f}%",
            1.0,
            "trailing",
        )
    return None


def _guillotine_due(pos: Position) -> bool:
    if _position_exit_pending(pos):
        return False
    age = _position_age_sec(pos)
    mfe = float(pos.mfe_pct or 0)
    mfe_min = guillotine_mfe_pct_for(pos)
    if mfe_min < 0:
        return False
    return (
        age >= giyotin_guillotine_sec()
        and mfe < mfe_min
        and not pos.breakeven_armed
    )


def check_guillotine_heartbeat_exit(pos: Position, price: float) -> GiyotinExit | None:
    if pos.entry_price <= 0:
        return GiyotinExit("exit_full", "giyotin failsafe: entry invalid", 1.0, "failsafe")
    if _guillotine_due(pos):
        mfe = float(pos.mfe_pct or 0)
        return GiyotinExit(
            "exit_full",
            f"giyotin guillotine heartbeat mfe={mfe:.1f}%",
            1.0,
            "guillotine",
        )
    return None


def evaluate_giyotin_exit(
    pos: Position,
    price: float,
    pnl: float,
    pair: Pair | None = None,
    *,
    missing_ticks: int = 0,
    missing_ticks_limit: int = 3,
) -> GiyotinExit | None:
    if pos.entry_price <= 0 or price <= 0:
        return GiyotinExit("exit_full", "giyotin failsafe: entry/price invalid", 1.0, "failsafe")
    if missing_ticks >= missing_ticks_limit:
        return GiyotinExit("exit_full", "giyotin failsafe: veri kayboldu", 1.0, "failsafe")
    if pos.cost_usd <= 0 or pos.amount_token <= 0:
        return GiyotinExit("exit_full", "giyotin failsafe: pozisyon tutarsız", 1.0, "failsafe")

    mfe = float(pos.mfe_pct or 0)
    dead_cat = is_giyotin_dead_cat_position(pos)
    be_trigger = giyotin_be_trigger_pct()
    trail_arm_mfe = trail_arm_mfe_pct_for(pos)

    if not pos.breakeven_armed and (pnl >= be_trigger or mfe >= be_trigger):
        _arm_break_even(pos)

    if not pos.trail_armed and mfe >= trail_arm_mfe:
        _arm_trail(pos, price)

    if _guillotine_due(pos):
        mfe_min = guillotine_mfe_pct_for(pos)
        return GiyotinExit(
            "exit_full",
            f"giyotin guillotine {giyotin_guillotine_sec()/60:.0f}dk mfe<{mfe_min:.1f}%",
            1.0,
            "guillotine",
        )

    stop_pct = position_hard_stop_pct(pos)
    if not pos.breakeven_armed and pnl <= stop_pct:
        tag = "volatile " if getattr(pos, "giyotin_volatile_entry", False) else ""
        return GiyotinExit(
            "exit_full",
            f"giyotin {tag}hard stop {pnl:.1f}%",
            1.0,
            "hard_stop",
        )

    if not pos.breakeven_armed and os.getenv("BOT_MODE", "paper") == "paper":
        floor_mult = 1.0 + stop_pct / 100.0
        if pos.entry_price > 0 and price / pos.entry_price <= floor_mult:
            return GiyotinExit(
                "exit_full",
                f"giyotin hard stop paper ≤{floor_mult:.3f}",
                1.0,
                "hard_stop",
            )

    runner_armed = bool(getattr(pos, "giyotin_runner_armed", False))

    if runner_armed and pos.trail_armed:
        green = _check_runner_green_exit(pos, pnl)
        if green:
            return green

    if pos.trail_armed:
        _update_trail_peak(pos, price)
        peak = pos.giyotin_trail_peak_usd
        if peak > 0:
            drawdown = (peak - price) / peak * 100
            trail_pct = (
                giyotin_dead_cat_trail_pct()
                if dead_cat and not runner_armed
                else (giyotin_runner_trail_pct() if runner_armed else giyotin_trail_pct())
            )
            if runner_armed:
                floor = giyotin_runner_floor_pct()
                giveback = giyotin_runner_profit_giveback_pct()
                if pnl > floor and drawdown >= giveback and drawdown < trail_pct:
                    return GiyotinExit(
                        "exit_full",
                        f"giyotin runner profit take -{drawdown:.1f}%",
                        1.0,
                        "trailing",
                    )
            if drawdown >= trail_pct:
                if (
                    not runner_armed
                    and giyotin_partial_exit_enabled()
                    and mfe >= giyotin_runner_min_mfe_pct()
                ):
                    frac = giyotin_trail_partial_frac()
                    return GiyotinExit(
                        "exit_partial",
                        f"giyotin trail partial -{drawdown:.1f}%",
                        frac,
                        "trailing",
                    )
                tag = "runner trail" if runner_armed else "trail"
                return GiyotinExit(
                    "exit_full",
                    f"giyotin {tag} -{drawdown:.1f}%",
                    1.0,
                    "trailing",
                )

    if pos.trail_armed and runner_armed and giyotin_runner_be_disabled():
        if pnl <= giyotin_runner_floor_pct():
            return GiyotinExit(
                "exit_full",
                f"giyotin runner floor {pnl:.1f}%",
                1.0,
                "trailing",
            )
    elif pos.trail_armed and pnl <= 0:
        return GiyotinExit("exit_full", f"giyotin break-even {pnl:.1f}%", 1.0, "break_even")

    if not pos.giyotin_phase:
        pos.giyotin_phase = GiyotinState.POSITION_OPEN.value
    persist_position_state(pos)
    return None


def register_giyotin_partial_exit(pos: Position, price: float) -> None:
    """İlk trail partial sonrası runner bacağını kur."""
    pos.giyotin_runner_armed = True
    pos.giyotin_runner_peak_pnl = 0.0
    pos.runner_mode = True
    pos.trail_armed = True
    pos.giyotin_trail_peak_usd = max(pos.giyotin_trail_peak_usd, price)
    transition_giyotin_state(pos, GiyotinState.TRAILING)
    persist_position_state(pos)


def register_giyotin_exit(pos: Position, reason: str) -> None:
    tag = (reason or "").lower()
    mfe = float(pos.mfe_pct or 0)
    if is_guillotine_exit_reason(reason):
        blacklist_guillotine_token(pos.token_address)
    elif "hard stop" in tag and mfe < guillotine_mfe_pct_for(pos):
        blacklist_pump_token(pos.token_address)
    remove_position_from_state(pos.token_address)
    transition_giyotin_state(pos, GiyotinState.CLOSED)
    maybe_adaptive_review()
    if mfe > 0:
        _log_winner_profile(pos)


def is_guillotine_exit_reason(reason: str) -> bool:
    return "guillotine" in (reason or "").lower()


def _log_winner_profile(pos: Position) -> None:
    def _m(d: dict) -> None:
        profiles = list(d.get("winner_profiles") or [])
        profiles.append(
            {
                "pair": pos.pair_name,
                "token": pos.token_address,
                "profile": (pos.entry_regime or "").replace("giyotin_", "").upper(),
                "mfe_pct": round(float(pos.mfe_pct or 0), 2),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )
        d["winner_profiles"] = profiles[-20:]

    _mutate_session(_m)


def _post_ship_trades(trades_path: Path, ship: str) -> list[dict]:
    if not trades_path.is_file():
        return []
    out: list[dict] = []
    for line in trades_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ship and (t.get("opened_at") or "") >= ship:
            out.append(t)
    return _dedupe_position_trades(out)


def maybe_adaptive_review(trades_path: str | Path = "data/trades.jsonl") -> None:
    """Her kapanış sonrası — son N trade ölçüm penceresinde adaptasyon."""
    if not giyotin_mode_enabled():
        return
    ship = ship_timestamp()
    post = _post_ship_trades(Path(trades_path), ship)
    n = giyotin_adaptive_review_n()
    if len(post) < n:
        return
    window = post[-n:]
    wins = [t for t in window if float(t.get("pnl_usd") or 0) > 0]
    wr = len(wins) / len(window)
    avg_mfe = sum(float(t.get("mfe_pct") or 0) for t in window) / len(window)

    def _m(d: dict) -> None:
        adaptive = dict(d.get("adaptive") or {})
        last_n = int(adaptive.get("last_review_n") or 0)
        if len(post) // n <= last_n:
            return
        adaptive["last_review_n"] = len(post) // n
        adaptive["last_review_at"] = datetime.now(timezone.utc).isoformat()
        adaptive["window_wr"] = round(wr * 100, 1)
        adaptive["window_avg_mfe"] = round(avg_mfe, 2)

        if len(wins) == 0:
            halt_until = datetime.now(timezone.utc).timestamp() + 86400
            d["entry_halted_until"] = datetime.fromtimestamp(halt_until, tz=timezone.utc).isoformat()
        elif wr < 0.35 or avg_mfe < 2.0:
            adaptive["a_billy_min"] = max(float(adaptive.get("a_billy_min") or 75), 80.0)
            adaptive["a_m5_max"] = min(float(adaptive.get("a_m5_max") or 5), 3.0)
            adaptive["a_turn_max"] = min(float(adaptive.get("a_turn_max") or 20), 15.0)
            adaptive["b_h1_min"] = max(float(adaptive.get("b_h1_min") or 6), 8.0)
            adaptive["b_billy_min"] = max(float(adaptive.get("b_billy_min") or 64), 70.0)
            adaptive["tightened"] = True
        d["adaptive"] = adaptive

    _mutate_session(_m)


def maybe_reconcile_giyotin_state(
    open_positions: list[Position],
    *,
    trades_path: str | Path = "data/trades.jsonl",
    force: bool = False,
) -> dict:
    """Saatlik reconcile — state positions ↔ broker; stale alarm."""
    if not giyotin_mode_enabled():
        return {"skipped": True}

    data = _load_session()
    last = data.get("last_reconcile_at")
    now = time.time()
    if not force and last:
        try:
            prev = datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp()
            if now - prev < 3600:
                return {"skipped": True, "reason": "cooldown"}
        except (ValueError, TypeError):
            pass

    open_keys = {_blacklist_key(p.token_address) for p in open_positions if p.token_address}
    state_keys = set((data.get("positions") or {}).keys())
    stale = state_keys - open_keys
    missing = open_keys - state_keys

    def _m(d: dict) -> None:
        positions = dict(d.get("positions") or {})
        for k in stale:
            positions.pop(k, None)
        d["positions"] = positions
        d["last_reconcile_at"] = datetime.now(timezone.utc).isoformat()
        d["stale_removed"] = sorted(stale)
        d["missing_in_state"] = sorted(missing)

    _mutate_session(_m)

    alarm = len(stale) >= giyotin_stale_alarm_limit()
    return {
        "stale_removed": len(stale),
        "missing_in_state": len(missing),
        "alarm": alarm,
    }


def giyotin_rotation_victim(
    positions: list[Position],
    new_billy: float,
    last_prices: dict[str, float],
) -> Position | None:
    """Slot dolu — yeni aday billy > en düşük açık billy ise en düşük PnL'li kurban."""
    if not positions:
        return None
    open_billies: list[tuple[Position, float]] = []
    for pos in positions:
        b = float(getattr(pos, "giyotin_billy", 0) or pos.entry_score or 0)
        open_billies.append((pos, b))
    min_billy = min(b for _, b in open_billies)
    if new_billy <= min_billy:
        return None

    def _pnl(p: Position) -> float:
        px = last_prices.get(p.pool_address, p.entry_price)
        if p.entry_price <= 0:
            return -999.0
        return (px - p.entry_price) / p.entry_price * 100

    return min(positions, key=_pnl)


def _stats_block(trades: list[dict], label: str) -> dict:
    trades = _dedupe_position_trades(trades)
    if not trades:
        return {
            "label": label,
            "count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "net_pnl": 0.0,
        }
    wins = [t for t in trades if float(t.get("pnl_usd", 0)) > 0]
    losses = [t for t in trades if float(t.get("pnl_usd", 0)) <= 0]
    gp = sum(float(t["pnl_usd"]) for t in wins)
    gl = sum(float(t["pnl_usd"]) for t in losses)
    return {
        "label": label,
        "count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(trades), 1),
        "gross_profit": round(gp, 2),
        "gross_loss": round(gl, 2),
        "net_pnl": round(gp + gl, 2),
    }


def _dedupe_position_trades(trades: list[dict]) -> list[dict]:
    by_open: dict[str, list[dict]] = {}
    for t in trades:
        key = str(t.get("opened_at") or t.get("trade_id") or "")
        if not key:
            key = f"{t.get('pair_name')}:{t.get('closed_at')}"
        by_open.setdefault(key, []).append(t)
    out: list[dict] = []
    for group in by_open.values():
        group.sort(key=lambda x: x.get("closed_at") or "")
        row = dict(group[0])
        row["pnl_usd"] = round(sum(float(x.get("pnl_usd") or 0) for x in group), 4)
        out.append(row)
    return out


def reset_giyotin_measurement() -> str:
    if not giyotin_mode_enabled():
        return ""
    ship_at = datetime.now(timezone.utc).isoformat()

    def _m(d: dict) -> None:
        d["ship_at"] = ship_at
        d["pump_blacklist"] = []
        d["guillotine_blacklist"] = []
        d["positions"] = {}
        d["sym_last_buy"] = {}
        d["adaptive"] = {
            "a_billy_min": float(os.getenv("GIYOTIN_A_BILLY_MIN", "75")),
            "a_m5_max": float(os.getenv("GIYOTIN_A_M5_MAX", "5.0")),
            "a_turn_max": float(os.getenv("GIYOTIN_A_TURN_MAX", "20")),
            "b_h1_min": float(os.getenv("GIYOTIN_B_H1_MIN", "6.0")),
            "b_billy_min": float(os.getenv("GIYOTIN_B_BILLY_MIN", "64")),
        }
        d["entry_halted_until"] = None
        d["winner_profiles"] = []

    _mutate_session(_m)
    return ship_at


def giyotin_trade_splits(trades_path: str | Path = "data/trades.jsonl") -> dict:
    path = Path(trades_path)
    ship = ship_timestamp()
    legacy: list[dict] = []
    after: list[dict] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            closed = t.get("closed_at") or ""
            if ship and closed >= ship:
                after.append(t)
            else:
                legacy.append(t)
    return {
        "ship_at": ship,
        "legacy": _stats_block(_dedupe_position_trades(legacy), "eski motor (öncesi)"),
        "giyotin": _stats_block(_dedupe_position_trades(after), f"Giyotin {giyotin_engine_label()}"),
    }


def learn_runner_filters_from_trades(
    trades_path: str | Path = "data/trades.jsonl",
    attr_path: str | Path = "data/attribution.jsonl",
    *,
    ship_at: str | None = None,
) -> dict:
    """v4.1 — altın kesişim counterfactual raporu."""
    ship = ship_at or ship_timestamp()
    trades_path = Path(trades_path)
    attr_path = Path(attr_path)
    attr: dict[str, dict] = {}
    if attr_path.is_file():
        for line in attr_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = row.get("trade_id")
            if tid:
                attr[str(tid)] = row

    post = _post_ship_trades(trades_path, ship)
    rows: list[dict] = []
    blocked_wins = blocked_losses = passed_wins = passed_losses = 0

    for t in post:
        a = attr.get(str(t.get("trade_id")), {})
        pump = a.get("pump") or {}
        pnl = float(t.get("pnl_usd") or 0)
        win = pnl > 0
        pair_like = {
            "chg_m5": float(a.get("chg_m5") or 0),
            "chg_h1": float(a.get("chg_h1") or 0),
            "chg_h24": float(a.get("chg_h24") or 0),
            "vol_h24": float(a.get("vol_h24") or 0),
            "liquidity_usd": float(a.get("liq_entry") or 1),
            "market_cap_usd": float(a.get("market_cap_usd") or 0),
            "txns_h1": int(a.get("traders_h1") or 0),
            "pool_created_at": None,
            "token_address": "",
        }
        from hibrit_trader.scanner import Pair

        p = Pair(
            chain="solana",
            dex="",
            pool_address="",
            token_address="",
            name=str(t.get("pair_name") or ""),
            price_usd=1.0,
            liquidity_usd=float(pair_like["liquidity_usd"]),
            vol_m5=0,
            vol_h1=0,
            vol_h24=float(pair_like["vol_h24"]),
            chg_m5=float(pair_like["chg_m5"]),
            chg_h1=float(pair_like["chg_h1"]),
            chg_h24=float(pair_like["chg_h24"]),
            txns_h1=int(pair_like["txns_h1"]),
            market_cap_usd=float(pair_like["market_cap_usd"]),
        )
        turn = float(pump.get("turnover") or 0)
        billy = billy_fit_score(
            chg_m5=p.chg_m5,
            moonshot_score=float(pump.get("moonshot_score") or 0),
            turnover=turn,
            age_hours=float(a.get("token_age_h") or 0) if a.get("token_age_h") is not None else None,
            txns_h1=p.txns_h1,
        )
        golden, _ = evaluate_golden_intersection(p, billy, turnover=turn)
        toxic, _ = evaluate_toxic_intersection(p, turnover=turn)
        blocked = not golden or toxic
        if blocked and win:
            blocked_wins += 1
        elif blocked and not win:
            blocked_losses += 1
        elif not blocked and win:
            passed_wins += 1
        else:
            passed_losses += 1
        rows.append(
            {
                "pair": t.get("pair_name"),
                "pnl_usd": round(pnl, 2),
                "mfe_pct": round(float(t.get("mfe_pct") or 0), 1),
                "win": win,
                "billy": billy,
                "h24": round(p.chg_h24, 1),
                "turn": round(turn, 1),
                "golden": golden,
                "toxic": toxic,
                "blocked": blocked,
            }
        )

    return {
        "ship_at": ship,
        "engine": _ENGINE,
        "n_trades": len(rows),
        "counterfactual": {
            "blocked_losses": blocked_losses,
            "blocked_wins": blocked_wins,
            "passed_wins": passed_wins,
            "passed_losses": passed_losses,
            "net_if_filtered": round(sum(r["pnl_usd"] for r in rows if not r["blocked"]), 2),
        },
        "rows": rows,
    }
