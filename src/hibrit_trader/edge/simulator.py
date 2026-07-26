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


def kademeli_politika(kilitler=((25.0, 0.25), (40.0, 0.25)),
                      trail_bantlari=((50.0, 20.0), (100.0, 15.0),
                                      (float("inf"), 10.0)),
                      stop_pct=-8.0, felaket_pct=-15.0,
                      timeout_dk=180.0) -> dict:
    """R2-tipi kademeli cikis ailesi (HIGH-6): kilitlerde kismi satis,
    kilit sonrasi ratchet trail, stop/felaket kalanin tamami."""
    return {"tur": "kademeli", "kilitler": tuple(kilitler),
            "trail_bantlari": tuple(trail_bantlari),
            "stop_pct": stop_pct, "felaket_pct": felaket_pct,
            "timeout_dk": timeout_dk}


def _kademeli(yol, pol: dict) -> dict:
    seri = yol.pct_seri()
    kalan, gerceklesen = 1.0, 0.0
    mfe = mae = tepe = 0.0
    kilit_i = 0
    son_cikis, son_dk = "seri_sonu", 0.0
    for dk, pct in seri[1:]:
        son_dk = dk
        mfe = max(mfe, pct)
        mae = min(mae, pct)
        tepe = max(tepe, pct)
        if pct <= pol["felaket_pct"]:
            gerceklesen += kalan * pct
            kalan, son_cikis = 0.0, "stop_felaket"
            break
        if kilit_i == 0 and pct <= pol["stop_pct"]:
            gerceklesen += kalan * pct
            kalan, son_cikis = 0.0, "stop_gec"
            break
        while (kilit_i < len(pol["kilitler"])
               and pct >= pol["kilitler"][kilit_i][0]):
            hedef, oran = pol["kilitler"][kilit_i]
            gerceklesen += oran * hedef
            kalan -= oran
            kilit_i += 1
            son_cikis = "tp_kilit"
        if kilit_i > 0:
            trail = next(t for esik, t in pol["trail_bantlari"]
                         if tepe < esik)
            if pct <= tepe - trail:
                gerceklesen += kalan * pct
                kalan, son_cikis = 0.0, "runner_trail"
                break
        if dk >= pol["timeout_dk"]:
            gerceklesen += kalan * pct
            kalan, son_cikis = 0.0, "timeout"
            break
    if kalan > 0:
        gerceklesen += kalan * seri[-1][1]
        son_dk = seri[-1][0]
    return _sonuc(gerceklesen, son_cikis, son_dk, mfe, mae)


def degerlendir(yol, politika: dict) -> dict:
    """Tek yolda politikayi kos; ham sonuc don. Yan etki yok."""
    if politika.get("tur") == "kademeli":
        return _kademeli(yol, politika)
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
