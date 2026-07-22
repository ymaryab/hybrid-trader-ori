"""Append-only olay yazicisi: saatlik JSONL segment + zstd + manifest.

Zarf: v, seq, ts_ms, slot, sig, kind, token, src, payload.
seq akis basina monotondur; bosluk = kayip kaniti. Crash sonrasi seq,
acik segmentin son satirindan devralinir (ayri sayac dosyasi yok,
tek gercek kaynak segmentin kendisi).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

_MANIFEST_KILIT = threading.Lock()

ZARF_V = 1
# aninda fsync gerektiren olay tipleri (kayip kabul edilemez)
FSYNC_KINDS = {"DecisionContext", "EngineEntryFilled", "EngineExitFilled",
               "CanliFill", "LiquidityRemoved", "GapDetected"}


class SegmentYazici:
    """Tek akisin saatlik segment dosyalarini yonetir."""

    def __init__(self, kok: Path, akis: str):
        self.kok = Path(kok)
        self.akis = akis
        self._fh = None
        self._saat_key = None
        self._yol: Path | None = None
        self.seq = 0
        self._seq_ilk = None
        self._satir = 0

    def _segment_yolu(self, ts: float) -> Path:
        g = time.strftime("%Y%m%d", time.gmtime(ts))
        h = time.strftime("%H", time.gmtime(ts))
        return self.kok / "events" / g / f"{h}.{self.akis}.jsonl"

    def _seq_devral(self, yol: Path) -> None:
        """Acik (sikismamis) segmentin son satirindan seq devral; dosyanin
        GERCEK ilk seq'ini de kaydet (manifest seq_ilk dogrulugu)."""
        if not yol.exists():
            return
        ilk = son = None
        satir = 0
        with open(yol, "rb") as f:
            for ln in f:
                if ln.strip():
                    satir += 1
                    if ilk is None:
                        ilk = ln
                    son = ln
        if son:
            try:
                self.seq = int(json.loads(son)["seq"])
                self._seq_ilk = int(json.loads(ilk)["seq"])
            except (ValueError, KeyError):
                pass
        self._satir = satir

    def _ac(self, ts: float) -> None:
        yol = self._segment_yolu(ts)
        yol.parent.mkdir(parents=True, exist_ok=True)
        self._seq_ilk = None
        if self.seq == 0 and yol.exists():   # sadece surec baslangicinda
            self._seq_devral(yol)            # _seq_ilk = dosyanin ilk seq'i
        self._fh = open(yol, "a", buffering=1)
        self._yol = yol
        self._saat_key = time.strftime("%Y%m%d%H", time.gmtime(ts))

    def _kapat_ve_sikistir(self) -> None:
        if self._fh is None:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None
        args = (self.kok, self.akis, self._yol,
                self._seq_ilk, self.seq, self._satir)
        # event loop'u bloke etmemek icin sikistirma ayri thread'de
        threading.Thread(target=self._sikistir_arka,
                         args=args, daemon=True).start()
        self._satir = 0

    @staticmethod
    def _sikistir_arka(kok, akis, yol, seq_ilk, seq_son, satir) -> None:
        try:
            subprocess.run(
                ["nice", "-n", "19", "zstd", "-9", "-q", "--rm", str(yol)],
                check=True, timeout=600)
            zyol = Path(str(yol) + ".zst")
            sha = hashlib.sha256(zyol.read_bytes()).hexdigest()
            man = {"ts_ms": int(time.time() * 1000), "akis": akis,
                   "dosya": str(zyol.relative_to(kok)),
                   "seq_ilk": seq_ilk, "seq_son": seq_son,
                   "satir": satir, "sha256": sha}
            with _MANIFEST_KILIT, open(kok / "manifest.jsonl", "a") as mf:
                mf.write(json.dumps(man) + "\n")
                mf.flush()
                os.fsync(mf.fileno())
        except (subprocess.SubprocessError, OSError):
            # sikistirma basarisiz: ham .jsonl yerinde kalir, veri kaybi yok
            pass

    def yaz(self, kind: str, payload: dict, *, token: str | None = None,
            slot: int | None = None, sig: str | None = None,
            src: str = "", ts_ms: int | None = None) -> dict:
        ts = time.time()
        key = time.strftime("%Y%m%d%H", time.gmtime(ts))
        if self._fh is None or key != self._saat_key:
            self._kapat_ve_sikistir()
            self._ac(ts)
        self.seq += 1
        if self._seq_ilk is None:
            self._seq_ilk = self.seq
        ev = {"v": ZARF_V, "seq": self.seq, "ts_ms": ts_ms or int(ts * 1000),
              "slot": slot, "sig": sig, "kind": kind, "token": token,
              "src": src, "payload": payload}
        self._fh.write(json.dumps(ev, separators=(",", ":"),
                                  default=str) + "\n")
        self._satir += 1
        if kind in FSYNC_KINDS:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        return ev

    def kapat(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None


class OlayYazici:
    """Cok akisli yazici. Akis = mantiksal kaynak grubu (dosya adi parcasi)."""

    def __init__(self, kok: Path):
        self.kok = Path(kok)
        self._akislar: dict[str, SegmentYazici] = {}

    def yaz(self, akis: str, kind: str, payload: dict, **kw) -> dict:
        sy = self._akislar.get(akis)
        if sy is None:
            sy = self._akislar[akis] = SegmentYazici(self.kok, akis)
        return sy.yaz(kind, payload, **kw)

    def kapat(self) -> None:
        for sy in self._akislar.values():
            sy.kapat()
