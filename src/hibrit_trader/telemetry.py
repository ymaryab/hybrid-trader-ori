"""Gözlem/telemetri katmanı — işlem-üstü analiz için ham olay kaydı (JSONL).

7 sinyal:
  1. MFE/MAE      → trades.jsonl (Position üzerinden işlenir)
  2. Ham giriş     → attribution.jsonl (giriş anı snapshot, sayısal)
  3. Kaynak        → attribution + trades (discovery_source)
  4. Reddedilen    → decisions.jsonl (reject_type=filter)
  5. Rejim damgası → attribution + trades (regime/fear_greed)
  6. Doluyken kaçan → decisions.jsonl (reject_type=no_slot/no_capital)
  7. Karar-giriş gecikmesi → attribution + trades (px_decision/decision_to_entry_sec/entry_drift)

İlke: hiçbir telemetri hatası trade döngüsünü kırmaz (fail-safe). Devre dışı bırakmak
için TELEMETRY_ENABLED=0. Dosyalar data/ altında (gitignore), events/ logs/ altında.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_lock = threading.Lock()

# attribution/decisions → data/ (gerçek/paper işlem gerçeği); events → logs/
DATA_DIR = Path(os.getenv("TELEMETRY_DATA_DIR", "data"))
LOGS_DIR = Path(os.getenv("TELEMETRY_LOGS_DIR", "logs"))

ATTRIBUTION_FILE = "attribution.jsonl"
DECISIONS_FILE = "decisions.jsonl"
EXITS_FILE = "exits.jsonl"  # kapanış anı tepe/dip + erken-tick profili (trades.jsonl'den ayrı)
SHADOW_EXITS_FILE = "shadow_exits.jsonl"  # kapanış sonrası 20dk fiyat izi (counterfactual, pasif)


def telemetry_enabled() -> bool:
    return os.getenv("TELEMETRY_ENABLED", "1") != "0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(path: Path, rec: dict) -> None:
    """Tek satır JSONL ekle — kilit altında, hata yutulur (fail-safe)."""
    if not telemetry_enabled():
        return
    try:
        payload = {"ts": round(time.time(), 3), "ts_iso": _now_iso(), **rec}
        line = json.dumps(payload, default=str, ensure_ascii=False)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:  # telemetri asla trade'i kırmaz
        log.debug("telemetri yazılamadı: %s", path, exc_info=True)


def log_attribution(row: dict) -> None:
    """Giriş anı snapshot — neden girdik, hangi ham değerlerle."""
    _append(DATA_DIR / ATTRIBUTION_FILE, row)


def log_decision(row: dict) -> None:
    """Reddedilen/kaçırılan aday — neden girmedik (filter/no_slot/no_capital)."""
    _append(DATA_DIR / DECISIONS_FILE, row)


def log_exit(row: dict) -> None:
    """Kapanış anı gözlem kaydı — tepe/dip fiyat+zaman, erken-tick profili (runner ölçümü).

    trades.jsonl append-only kalır; bu dosya counterfactual/ölçüm alanlarını taşır.
    """
    _append(DATA_DIR / EXITS_FILE, row)


def log_shadow_exit(row: dict) -> None:
    """Kapanış sonrası 20dk fiyat izi — saf gözlem, trade'i etkilemez.

    exits.jsonl/trades.jsonl'a dokunmaz; ayrı counterfactual ölçüm dosyası.
    "Beklemek zararı küçültür müydü" sorusunu ileride forward ölçmek için.
    """
    _append(DATA_DIR / SHADOW_EXITS_FILE, row)


def log_event(kind: str, message: str, **fields) -> None:
    """Merkezi olay akışı — SYSTEM/SIGNAL/MONEY/ERROR + serbest alanlar.

    Günlük dosya: logs/events-YYYY-MM-DD.jsonl
    """
    if not telemetry_enabled():
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _append(LOGS_DIR / f"events-{day}.jsonl", {"kind": kind, "message": message, **fields})


def read_recent(name: str, limit: int = 100) -> list[dict]:
    """Son N kaydı oku (panel/analiz). name: 'attribution' | 'decisions'."""
    fname = {"attribution": ATTRIBUTION_FILE, "decisions": DECISIONS_FILE, "exits": EXITS_FILE}.get(name)
    if fname is None:
        return []
    path = DATA_DIR / fname
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[dict] = []
        for line in lines[-limit:]:
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return list(reversed(out))
    except Exception:
        log.debug("telemetri okunamadı: %s", path, exc_info=True)
        return []


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:
        return 0


def summarize() -> dict:
    """Hafif özet — panel/monitör için sayımlar ve reddetme dağılımı."""
    decisions = read_recent("decisions", limit=500)
    reject_breakdown: dict[str, int] = {}
    for row in decisions:
        rt = str(row.get("reject_type", "?"))
        reject_breakdown[rt] = reject_breakdown.get(rt, 0) + 1
    return {
        "enabled": telemetry_enabled(),
        "attribution_count": _count_lines(DATA_DIR / ATTRIBUTION_FILE),
        "decisions_count": _count_lines(DATA_DIR / DECISIONS_FILE),
        "exits_count": _count_lines(DATA_DIR / EXITS_FILE),
        "reject_breakdown": reject_breakdown,
    }
