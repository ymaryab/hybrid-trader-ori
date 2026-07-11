from pathlib import Path

from hibrit_trader.killswitch import KILL_FILE, activate, deactivate, is_active


def test_kill_switch_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr("hibrit_trader.killswitch.KILL_FILE", tmp_path / "KILL")
    assert not is_active()
    activate("test")
    assert is_active()
    deactivate()
    assert not is_active()


def test_kill_switch_telegram_bildirimi(tmp_path, monkeypatch):
    from hibrit_trader import killswitch

    monkeypatch.setattr(killswitch, "KILL_FILE", tmp_path / "KILL")
    mesajlar = []
    monkeypatch.setattr(killswitch, "notify", lambda m: mesajlar.append(m))
    activate("panel")
    deactivate()
    deactivate()  # dosya yokken tekrar: yeni bildirim gitmez
    assert mesajlar == [
        "KILL AKTIF (panel): filo durduruldu, yeni islem acilmaz.",
        "Kill-switch kaldirildi: filo normal akisa dondu.",
    ]
