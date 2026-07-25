"""Path Archive: ampirik yol olcusu uzerinde SALT-OKUR arayuz.

Kaynak: data/kosucu_ekg.jsonl (EKG tick kayitlari; motor koduna dokunmaz,
dosyayi yalniz okur). Piyasa modellenmez, uretilmez, tahmin edilmez:
arsivdeki gercek yollar dagilimin kendisidir (ham-veri ilkesi).

Yol = tek tokenin zaman sirali (ts, fiyat) serisi + turev ozetler.
Turevler diske YAZILMAZ; her sey okuma aninda hesaplanir.
"""

from __future__ import annotations

import json
from pathlib import Path


class Yol:
    """Tek tokenin gozlenen fiyat yolu (tetik-kosullu evren)."""

    __slots__ = ("token", "ticks")

    def __init__(self, token: str, ticks: list[tuple[float, float]]):
        self.token = token
        self.ticks = sorted(ticks)          # (ts, fiyat_usd)

    @property
    def ilk_fiyat(self) -> float:
        return self.ticks[0][1]

    @property
    def ath_pct(self) -> float:
        p0 = self.ilk_fiyat
        return 100 * (max(p for _, p in self.ticks) / p0 - 1)

    @property
    def yasam_dk(self) -> float:
        return (self.ticks[-1][0] - self.ticks[0][0]) / 60

    def pct_seri(self) -> list[tuple[float, float]]:
        """(dakika, ilk fiyata gore % degisim) serisi."""
        t0, p0 = self.ticks[0]
        return [((ts - t0) / 60, 100 * (p / p0 - 1)) for ts, p in self.ticks]


class YolArsivi:
    """kosucu_ekg.jsonl -> Yol nesneleri. Salt okur, tek gecis."""

    def __init__(self, veri: Path = Path("data"), min_tick: int = 3):
        self.dosya = Path(veri) / "kosucu_ekg.jsonl"
        self.min_tick = min_tick

    def _ham(self) -> dict[str, list[tuple[float, float]]]:
        seriler: dict[str, list[tuple[float, float]]] = {}
        try:
            fh = open(self.dosya)
        except OSError:
            return seriler
        with fh:
            for ln in fh:
                if not ln.strip():
                    continue
                try:
                    t = json.loads(ln)
                except ValueError:
                    continue
                m = t.get("token_address")
                p = float(t.get("price_usd") or 0)
                ts = float(t.get("ts") or 0)
                if m and p > 0 and ts > 0:
                    seriler.setdefault(m, []).append((ts, p))
        return seriler

    def yollar(self):
        """Butun yollari uret (min_tick alti seriler elenir, sayilir)."""
        for token, ticks in self._ham().items():
            if len(ticks) >= self.min_tick:
                yield Yol(token, ticks)

    def yol(self, token: str) -> Yol | None:
        ticks = self._ham().get(token) or []
        return Yol(token, ticks) if len(ticks) >= self.min_tick else None

    def sayim(self) -> dict:
        ham = self._ham()
        yeterli = sum(1 for t in ham.values() if len(t) >= self.min_tick)
        return {"token_n": len(ham), "yeterli_n": yeterli,
                "elenen_n": len(ham) - yeterli, "min_tick": self.min_tick}
