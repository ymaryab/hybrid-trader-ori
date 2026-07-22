"""DecisionContext: her motor giris olayinda degismez karar-ani fotografi.

Kaynak olaylarin (giris, son snapshot, son MarketContext) TAM kopyasi
gomulur + (akis, seq) referanslari yazilir. Replay yukleyicisi
referanslardan ayni baglami yeniden kurup gomulu kopyayla dogrular.
ctx_id deterministiktir: sha256(engine|trade_id) ilk 16 hex.
"""

from __future__ import annotations

import hashlib


def ctx_id_uret(engine: str, trade_id: str) -> str:
    return hashlib.sha256(f"{engine}|{trade_id}".encode()).hexdigest()[:16]


class KararUretici:
    def __init__(self, bus, onbellek):
        self.bus = bus
        self.onbellek = onbellek

    async def olay_isle(self, ev: dict, akis: str) -> None:
        kind = ev.get("kind")
        pl = ev.get("payload") or {}
        if kind == "EngineEntryFilled":
            eng = pl.get("engine") or "?"
            tid = pl.get("trade_id") or ev.get("sig") or str(ev.get("seq"))
        elif kind == "CanliFill" and pl.get("yon") == "alis":
            eng = "CANLI"
            tid = pl.get("tx") or str(ev.get("seq"))
        else:
            return
        tok = ev.get("token")
        snap = self.onbellek.son_snapshot.get(tok)
        mctx = self.onbellek.son_mctx
        ctx = {
            "ctx_id": ctx_id_uret(eng, tid),
            "engine": eng,
            "trade_id": tid,
            "token": tok,
            "giris": {"akis": akis, "seq": ev.get("seq"),
                      "ts_ms": ev.get("ts_ms"), "payload": pl},
            "snapshot": (None if snap is None else
                         {"akis": "anlik", "seq": snap.get("seq"),
                          "ts_ms": snap.get("ts_ms"),
                          "yas_ms": ev.get("ts_ms", 0) - snap.get("ts_ms", 0),
                          "payload": snap.get("payload")}),
            "market_context": (None if mctx is None else
                               {"akis": "anlik", "seq": mctx.get("seq"),
                                "ts_ms": mctx.get("ts_ms"),
                                "payload": mctx.get("payload")}),
            "izlenen_kume": sorted(self.onbellek.izlenen),
        }
        # dogrudan yazici: dagitici icinden kuyruga geri koymak kilitlenir
        self.bus.yazici.yaz("karar", "DecisionContext", ctx,
                            token=tok, src="karar")
        self.bus.sayac.kind_sayi["DecisionContext"] += 1
