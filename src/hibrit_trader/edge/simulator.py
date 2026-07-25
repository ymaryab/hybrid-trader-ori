"""Policy Simulator: payoff_pi(yol) fonksiyonelleri. SAF fonksiyon, IO yok.

Faz 0 replay semantigi: giris = yolun ilk tick'i (tetik ani), cikislar
tick cozunurlugunde degerlendirilir. SADAKAT SINIRI (durustluk): EKG
dakika-cozunurluklu ve tetik-kosullu; tick araligindaki asiri hareketler
gorulmez, kayma/ucret modellenmez. Edge tahmini bu tavanla sinirlidir
ve raporlarda ayri satir olarak belirtilmelidir.

Politika = parametre sozlugu; degerlendir() tek yol uzerinde kosar.
Cikti ham sonuc sozlugudur (pnl_pct, cikis, sure_dk, mfe, mae).
"""

from __future__ import annotations


def tp_politikasi(tp_pct: float, timeout_dk: float,
                  stop_pct: float | None = None) -> dict:
    """Sabit hedefli politika tanimi (TP2/TP5 ailesi)."""
    return {"tur": "tp", "tp_pct": tp_pct, "timeout_dk": timeout_dk,
            "stop_pct": stop_pct}


def runner_politikasi(kilit_pct: float, trail_pct: float,
                      timeout_dk: float) -> dict:
    """Kar kilidi + ratchet trail ailesi (RUN)."""
    return {"tur": "runner", "kilit_pct": kilit_pct,
            "trail_pct": trail_pct, "timeout_dk": timeout_dk}


def degerlendir(yol, politika: dict) -> dict:
    """Tek yolda politikayi kos; ham sonuc don. Yan etki yok."""
    seri = yol.pct_seri()
    mfe = mae = 0.0
    tepe = 0.0
    kilitli = False
    for dk, pct in seri[1:]:
        mfe = max(mfe, pct)
        mae = min(mae, pct)
        tepe = max(tepe, pct)
        if politika["tur"] == "tp":
            stop = politika.get("stop_pct")
            if stop is not None and pct <= stop:
                return _sonuc(pct, "stop", dk, mfe, mae)
            if pct >= politika["tp_pct"]:
                return _sonuc(politika["tp_pct"], "tp", dk, mfe, mae)
        else:                                   # runner
            if not kilitli and pct >= politika["kilit_pct"]:
                kilitli = True
            if kilitli and pct <= tepe - politika["trail_pct"]:
                return _sonuc(pct, "trail", dk, mfe, mae)
        if dk >= politika["timeout_dk"]:
            return _sonuc(pct, "timeout", dk, mfe, mae)
    dk, pct = seri[-1] if len(seri) > 1 else (0.0, 0.0)
    return _sonuc(pct, "seri_sonu", dk, mfe, mae)


def _sonuc(pnl, cikis, dk, mfe, mae):
    return {"pnl_pct": round(pnl, 3), "cikis": cikis,
            "sure_dk": round(dk, 1), "mfe": round(mfe, 3),
            "mae": round(mae, 3)}
