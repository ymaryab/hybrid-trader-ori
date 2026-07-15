"""Kill-switch — acil durdurma + opsiyonel Telegram bildirimi."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

_log = logging.getLogger(__name__)
KILL_FILE = Path("data/KILL")


def is_active() -> bool:
    return KILL_FILE.exists() or os.getenv("KILL_SWITCH", "").strip() == "1"


def activate(reason: str = "manuel") -> None:
    KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
    KILL_FILE.write_text(f"{datetime.now(timezone.utc).isoformat()} — {reason}\n")
    _event(f"kill-switch AKTIF — {reason}", reason=reason)
    notify(f"KILL AKTIF ({reason}): filo durduruldu, yeni islem acilmaz.")


def deactivate() -> None:
    was = KILL_FILE.exists()
    if was:
        KILL_FILE.unlink()
    if was:
        _event("kill-switch kapatildi")
        notify("Kill-switch kaldirildi: filo normal akisa dondu.")


def _event(message: str, **fields) -> None:
    try:
        from hibrit_trader import telemetry

        telemetry.log_event("SYSTEM", message, **fields)
    except Exception:
        pass


def notify(message: str, bot_token: str = "", chat_id: str = "") -> None:
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message},
            timeout=10,
        )
        if r.status_code != 200:
            _log.warning("telegram notify HTTP %d: %s", r.status_code, r.text[:200])
    except httpx.HTTPError as e:
        _log.warning("telegram notify hatasi: %s", e)
