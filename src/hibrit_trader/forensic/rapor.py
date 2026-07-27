"""Forensic Factory: RAPOR katmani.

Rapor iki blogu ASLA karistirmaz: giris aninda bilinebilen ozellikler
(karar kurmaya aday) ve giristen sonra olusanlar (yalniz teshis).
Ayrica veri saydamligi (dusen satirlar, kismi alanlar, kirli pencere)
her raporun basinda yer alir; bulgu bu baglam olmadan okunmaz.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def metin(evren_ozet: dict, kohort_ad: str, maliyet: dict,
          imza: dict, ornekler: list[dict] | None = None) -> str:
    s = []
    s.append("=" * 72)
    s.append(f"FORENSIC FABRIKA · kohort: {kohort_ad} · "
             f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC")
    s.append("=" * 72)
    s.append("")
    s.append("VERI SAYDAMLIGI")
    s.append(f"  evren: {evren_ozet['n']} islem, {evren_ozet['baslangic']}'ten itibaren")
    s.append(f"  motorlar: {', '.join(evren_ozet['motorlar'])}")
    s.append(f"  birlestirilen cok-parcali pozisyon: {evren_ozet['birlestirilen_pozisyon']}")
    if evren_ozet["dusen"]:
        s.append(f"  dusen satirlar: {evren_ozet['dusen']}")
    if evren_ozet["kirli_pencere"]:
        s.append("  !! KIRLI PENCERE: guvenilir baslangictan onceki veri dahil")
    s.append("")
    s.append("KOHORTUN MALIYETI")
    s.append(f"  kohort: {imza['hedef_n']} islem (evrenin %{maliyet['kohort_islem_payi_pct']}'i)"
             f" · PnL ${maliyet['kohort_pnl_usd']:+.2f}")
    s.append(f"  evren PnL ${maliyet['evren_pnl_usd']:+.2f} · "
             f"bu kohort olmasaydi ${maliyet['kohort_haric_pnl_usd']:+.2f}")
    s.append("")

    for zaman, baslik, aciklama in (
            ("giris", "GIRIS ANINDA BILINEN OZELLIKLER",
             "karar kurmaya aday; ayrim buradaysa is yapilabilir"),
            ("sonra", "GIRISTEN SONRA OLUSANLAR",
             "yalniz teshis; bunlarla giris kurali KURULAMAZ")):
        s.append(f"{baslik}  ({aciklama})")
        s.append("  %-22s %9s %9s %8s %8s %8s" %
                 ("ozellik", "kohort", "kontrol", "delta", "buyukluk", "gun"))
        var = False
        for r in imza["satirlar"]:
            if r.get("zaman") != zaman:
                continue
            if r["durum"] != "olculdu":
                s.append("  %-22s  yetersiz ornek (hedef %s / kontrol %s)" %
                         (r["ozellik"], r["hedef_n"], r["kontrol_n"]))
                var = True
                continue
            var = True
            uyari = " *kismi" if r["kismi_alanlar"] else ""
            s.append("  %-22s %9.3f %9.3f %8.3f %8s %8s%s" %
                     (r["ozellik"], r["hedef_medyan"], r["kontrol_medyan"],
                      r["cliff_delta"], r["buyukluk"], r["gun_tutarliligi"],
                      uyari))
        if not var:
            s.append("  (kayitli ozellik yok)")
        s.append("")

    s.append("OKUMA NOTU")
    s.append("  cliff delta: |d|<0.15 ihmal · 0.15-0.33 kucuk · 0.33-0.47 orta"
             " · >=0.47 buyuk")
    s.append("  gun sutunu: etkinin ayni isarette kaldigi gun / olculebilen gun")
    s.append("  *kismi: ozellik, alani dolu olan ALT-EVRENDE olculdu")
    s.append("  Fabrika esik onermez; ayrim guclu gorunuyorsa siradaki adim"
             " ON-KAYITLI ve kor pencereli dogrulamadir.")
    if ornekler:
        s.append("")
        s.append("KOHORT ORNEKLERI (en buyuk 5 kayip)")
        for t in ornekler[:5]:
            s.append("  %s %-8s %-16s %+7.2f%% $%+8.2f %-14s h1=%+6.1f mfe=%+5.1f" % (
                time.strftime("%m-%d %H:%M", time.gmtime(t["_giris_ts"])),
                t.get("_motor", "?"), str(t.get("pair"))[:16], t["pnl_pct"],
                t.get("pnl_usd") or 0, str(t.get("exit_reason"))[:14],
                t.get("chg_h1") or 0, t.get("mfe_pct") or 0))
    return "\n".join(s)


def json_yaz(yol: Path, evren_ozet: dict, kohort_ad: str,
             maliyet: dict, imza: dict) -> None:
    yol.parent.mkdir(parents=True, exist_ok=True)
    tmp = yol.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "sv": 1, "ts": time.time(), "kohort": kohort_ad,
        "evren": evren_ozet, "maliyet": maliyet, "imza": imza},
        ensure_ascii=False, indent=1))
    tmp.replace(yol)
