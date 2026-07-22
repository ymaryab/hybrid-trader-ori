#!/usr/bin/env python3
"""Replay yukleyici: ctx_id ile DecisionContext'i bul, referanslardan
yeniden kur ve gomulu kopyayla dogrula.

Kullanim:
  python scripts/gozlem_replay.py <ctx_id> [--dizin data/gozlem]
  python scripts/gozlem_replay.py --liste [-n 10]
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path


def segmentler(kok: Path, akis: str | None = None):
    """Tum segment dosyalari (jsonl + jsonl.zst), kronolojik."""
    ev = kok / "events"
    if not ev.exists():
        return
    for yol in sorted(ev.rglob("*.jsonl*")):
        ad = yol.name
        if akis is not None and f".{akis}.jsonl" not in ad:
            continue
        yield yol


def satirlar(yol: Path):
    if yol.suffix == ".zst":
        p = subprocess.run(["zstd", "-dc", str(yol)], capture_output=True,
                           check=True)
        fh = io.BytesIO(p.stdout)
    else:
        fh = open(yol, "rb")
    with fh:
        for ln in fh:
            if ln.strip():
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def olay_bul(kok: Path, akis: str, seq: int):
    for yol in segmentler(kok, akis):
        for ev in satirlar(yol):
            if ev.get("seq") == seq:
                return ev
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ctx_id", nargs="?")
    ap.add_argument("--dizin", default="data/gozlem")
    ap.add_argument("--liste", action="store_true")
    ap.add_argument("-n", type=int, default=10)
    a = ap.parse_args()
    kok = Path(a.dizin)

    if a.liste:
        son = []
        for yol in segmentler(kok, "karar"):
            for ev in satirlar(yol):
                if ev.get("kind") == "DecisionContext":
                    p = ev["payload"]
                    son.append((ev["ts_ms"], p["ctx_id"], p["engine"],
                                p.get("token")))
        for ts, cid, eng, tok in son[-a.n:]:
            print(f"{cid}  {eng:<8} {ts}  {tok}")
        return 0

    if not a.ctx_id:
        ap.error("ctx_id gerekli (veya --liste)")
    ctx = None
    for yol in segmentler(kok, "karar"):
        for ev in satirlar(yol):
            if (ev.get("kind") == "DecisionContext"
                    and ev["payload"].get("ctx_id") == a.ctx_id):
                ctx = ev
    if ctx is None:
        print(f"HATA: ctx {a.ctx_id} bulunamadi", file=sys.stderr)
        return 1
    p = ctx["payload"]
    print(json.dumps(p, indent=1)[:4000])
    print()
    # dogrulama: referanslardan yeniden kur
    sonuc = []
    for ad in ("giris", "snapshot", "market_context"):
        ref = p.get(ad)
        if ref is None:
            sonuc.append((ad, "YOK (karar aninda mevcut degildi)"))
            continue
        akis = ref.get("akis") or ("motor" if ad == "giris" else "anlik")
        orij = olay_bul(kok, akis, ref["seq"])
        if orij is None:
            sonuc.append((ad, f"HATA: {akis}/seq={ref['seq']} bulunamadi"))
        elif orij.get("payload") == ref.get("payload"):
            sonuc.append((ad, f"DOGRULANDI ({akis}/seq={ref['seq']})"))
        else:
            sonuc.append((ad, f"UYUSMAZLIK ({akis}/seq={ref['seq']})"))
    print("YENIDEN KURULUM DOGRULAMASI:")
    kotu = False
    for ad, d in sonuc:
        print(f"  {ad:<16} {d}")
        kotu = kotu or d.startswith(("HATA", "UYUSMAZLIK"))
    return 2 if kotu else 0


if __name__ == "__main__":
    sys.exit(main())
