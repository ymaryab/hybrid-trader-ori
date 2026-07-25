"""Trade suskunluğunda filtre gevşetme / pump_bl temizleme / h1 chase kilidi."""

from __future__ import annotations

import os


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) == "1"


def _f(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def drought_guard_enabled() -> bool:
    return os.getenv("GIYOTIN_DROUGHT_GUARD", "1") != "0"


def drought_guard_config() -> dict:
    enabled = drought_guard_enabled()
    return {
        "enabled": enabled,
        "drought_loosen": _flag("GIYOTIN_DROUGHT_LOOSEN", "0"),
        "drought_loosen_effective": tier1_filters_may_loosen(),
        "pump_bl_auto_clear": _flag("GIYOTIN_PUMP_BL_AUTO_CLEAR", "0"),
        "pump_bl_auto_clear_effective": pump_blacklist_clear_allowed(),
        "h1_chase_entry": _flag("GIYOTIN_H1_CHASE_ENTRY", "0"),
        "h1_chase_entry_effective": h1_chase_entry_enabled(),
        "max_h1_pct": _f("GIYOTIN_DROUGHT_MAX_H1_PCT", "50"),
        "note": "drought guard aktif — pump chase / oturum bl temizleme / gevşetme kapalı"
        if enabled
        else "drought guard kapalı",
    }


def clamp_volatile_h1_pct(requested: float) -> float:
    if not drought_guard_enabled():
        return requested
    return min(requested, _f("GIYOTIN_DROUGHT_MAX_H1_PCT", "50"))


def pump_blacklist_clear_allowed(*, measurement_reset: bool = False) -> bool:
    if measurement_reset:
        return True
    if drought_guard_enabled():
        return False
    return _flag("GIYOTIN_PUMP_BL_AUTO_CLEAR", "0")


def tier1_filters_may_loosen() -> bool:
    if drought_guard_enabled():
        return False
    return _flag("GIYOTIN_DROUGHT_LOOSEN", "0")


def h1_chase_entry_enabled() -> bool:
    if drought_guard_enabled():
        return False
    return _flag("GIYOTIN_H1_CHASE_ENTRY", "0")


def drought_pump_chase_blocked(chg_h1: float, *, pump_blacklisted: bool) -> tuple[bool, str]:
    """Yüksek h1 + pump_bl = chase; guard açıkken ekstra red."""
    if not drought_guard_enabled() or not pump_blacklisted:
        return False, ""
    cap = _f("GIYOTIN_DROUGHT_MAX_H1_PCT", "50")
    h1 = float(chg_h1 or 0)
    if h1 > cap:
        return True, f"drought guard pump chase h1 %{h1:.0f}>{cap:.0f}"
    return False, ""
