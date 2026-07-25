"""Giyotin v4.1 — bağımsız asyncio heartbeat.

Guillotine zamanlayıcısı yalnızca fiyat tick'ine bağlı kalmaz; WS donsa bile
3dk kontrolü saniyede bir çalışır.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

from hibrit_trader.giyotin_strategy import giyotin_mode_enabled
from hibrit_trader.liq_momentum_exclusive import exclusive_mode_enabled

if TYPE_CHECKING:
    from hibrit_trader.session import Engine

log = logging.getLogger(__name__)


class GiyotinHeartbeat:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not giyotin_mode_enabled() and not exclusive_mode_enabled():
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="giyotin-heartbeat", daemon=True)
        self._thread.start()
        log.info("Giyotin heartbeat başladı (1s asyncio)")

    def stop(self) -> None:
        self._stop.set()

    def _thread_main(self) -> None:
        asyncio.run(self._async_loop())

    async def _async_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._engine.giyotin_heartbeat_pulse()
            except Exception:
                log.exception("giyotin heartbeat pulse")
            await asyncio.sleep(1.0)
