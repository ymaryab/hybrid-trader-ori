"""DecisionContext: her motor giris olayinda degismez karar-ani fotografi.

Kaynak olaylarin (giris, son snapshot, son MarketContext) TAM kopyasi
gomulur + (akis, seq) referanslari yazilir. Replay yukleyicisi
referanslardan ayni baglami yeniden kurup gomulu kopyayla dogrular.
ctx_id deterministiktir: sha256(engine|trade_id) ilk 16 hex.

Denetim duzeltmeleri (22 Tem):
- CanliFill yonu WAL'daki gercek degerle eslesir ("al"; eski "alis"
  varsayimi olu daldi). CANLI ctx'leri YALNIZ WAL'dan uretilir; CANLI
  EngineEntryFilled ctx uretmez (cift-ctx korumasi: ayni girisin iki
  kaynaktan iki ctx olmasi yapisal olarak imkansiz). Ek koruma:
  uretilen ctx_id kumesi, ayni id'yi ikinci kez uretmez.
- Terfi yarisi: karar aninda snapshot yoksa veya 15 sn'den bayatsa
  DexScreener'dan ACIL tek cekim yapilir (snap_getir), olay olarak da
  yazilir; ctx'te acil_cekim=true ile isaretlenir. Cekim basarisizsa
  snapshot durustce None kalir.
"""

from __future__ import annotations

import hashlib

SNAP_BAYAT_MS = 15_000


def ctx_id_uret(engine: str, trade_id: str) -> str:
    return hashlib.sha256(f"{engine}|{trade_id}".encode()).hexdigest()[:16]


class KararUretici:
    ACIL_DEVRE_SN = 30.0   # 24 Tem: 429 baskisinda kota israfini onler

    def __init__(self, bus, onbellek, snap_getir=None):
        """snap_getir: async (token) -> ham pair payload | None"""
        self.bus = bus
        self.onbellek = onbellek
        self.snap_getir = snap_getir
        self._uretilen: set[str] = set()
        self._uretilen_sira: list[str] = []   # FIFO tavan (RSS budamasi)
        self._acil_son_hata = 0.0

    URETILEN_TAVAN = 20000

    def _uretilen_ekle(self, cid: str) -> None:
        self._uretilen.add(cid)
        self._uretilen_sira.append(cid)
        if len(self._uretilen_sira) > self.URETILEN_TAVAN:
            atilan = self._uretilen_sira[:-self.URETILEN_TAVAN]
            self._uretilen_sira = self._uretilen_sira[-self.URETILEN_TAVAN:]
            self._uretilen.difference_update(atilan)

    async def olay_isle(self, ev: dict, akis: str) -> None:
        kind = ev.get("kind")
        pl = ev.get("payload") or {}
        if kind == "EngineEntryFilled":
            eng = pl.get("engine") or "?"
            if eng == "CANLI":
                return   # CANLI ctx yalniz WAL'dan: cift-ctx korumasi
            tid = pl.get("trade_id") or ev.get("sig") or str(ev.get("seq"))
        elif kind == "CanliFill" and pl.get("yon") in ("al", "alis"):
            eng = "CANLI"
            tid = pl.get("tx") or str(ev.get("seq"))
        else:
            return
        cid = ctx_id_uret(eng, tid)
        if cid in self._uretilen:
            return
        self._uretilen_ekle(cid)
        tok = ev.get("token")
        snap = self.onbellek.son_snapshot.get(tok)
        acil = False
        bayat = (snap is None
                 or ev.get("ts_ms", 0) - snap.get("ts_ms", 0) > SNAP_BAYAT_MS)
        import time as _t
        devre_acik = _t.time() - self._acil_son_hata < self.ACIL_DEVRE_SN
        if bayat and self.snap_getir is not None and tok and not devre_acik:
            try:
                taze = await self.snap_getir(tok)
            except Exception as e:  # noqa: BLE001
                taze = None
                self._acil_son_hata = _t.time()   # devre 30sn acilir
                self.bus.yazici.yaz(
                    "sistem", "GapDetected",
                    {"src": "acil_cekim", "neden": str(e)[:200]},
                    token=tok, src="karar")
            if taze is not None:
                snap = self.bus.yazici.yaz("anlik", "Snapshot", taze,
                                           token=tok, src="dexs-acil")
                self.bus.sayac.kind_sayi["Snapshot"] += 1
                self.onbellek.guncelle(snap)
                acil = True
        mctx = self.onbellek.son_mctx
        ctx = {
            "ctx_id": cid,
            "engine": eng,
            "trade_id": tid,
            "token": tok,
            "kesinti_telafisi": bool(pl.get("kesinti_telafisi")),
            "giris": {"akis": akis, "seq": ev.get("seq"),
                      "ts_ms": ev.get("ts_ms"), "payload": pl},
            "snapshot": (None if snap is None else
                         {"akis": "anlik", "seq": snap.get("seq"),
                          "ts_ms": snap.get("ts_ms"),
                          "yas_ms": ev.get("ts_ms", 0) - snap.get("ts_ms", 0),
                          "acil_cekim": acil,
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
