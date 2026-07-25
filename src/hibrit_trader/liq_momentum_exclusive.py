"""Stub: yalnizca exclusive_mode_enabled (giyotin_drought_guard bagimliligini karsilar)."""
import os

def exclusive_mode_enabled() -> bool:
    return os.getenv("HIBRIT_EXCLUSIVE_LIQ_MOMENTUM", "0") != "0"
