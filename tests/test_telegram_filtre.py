"""TELEGRAM_SADECE_CANLI filtresi: motor onekli mesajlardan yalniz
[CANLI] gecer; sistem uyarilari her zaman gecer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hibrit_trader import killswitch


def _gonderilenler(monkeypatch, env_deger, mesajlar):
    monkeypatch.setenv("TELEGRAM_SADECE_CANLI", env_deger)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "C")
    giden = []

    class SahteYanit:
        status_code = 200

    monkeypatch.setattr(killswitch.httpx, "post",
                        lambda url, json, timeout: giden.append(json["text"])
                        or SahteYanit())
    for m in mesajlar:
        killswitch.notify(m)
    return giden


MESAJLAR = [
    "[CANLI] ALIM: X $10",
    "[CANLI] SALTER KAPALI: yeni giris durdu",
    "[YZ] SATIM: Y pnl $1",
    "[V7HT] ALIM: Z $5",
    "⚠️ SENKRON UYARI: CANLI Balloon cuzdan farki",
    "⚠️ SENKRON UYARI: baska motor farki",
    "KILL AKTIF (test): filo durduruldu",
    "⚠️ RPC HATA: endpoint dustu",
]


def test_filtre_acik(monkeypatch):
    """Yalniz [CANLI] onekli mesajlar gecer; senkron dahil geri kalan
    her sey kapali."""
    giden = _gonderilenler(monkeypatch, "1", MESAJLAR)
    assert giden == ["[CANLI] ALIM: X $10",
                     "[CANLI] SALTER KAPALI: yeni giris durdu"]


def test_filtre_kapali(monkeypatch):
    giden = _gonderilenler(monkeypatch, "0", MESAJLAR)
    assert giden == MESAJLAR
