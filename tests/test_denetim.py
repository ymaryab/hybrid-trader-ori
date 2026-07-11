import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hibrit_trader import denetim


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    return tmp_path


def _yaz(data_dir: Path, dosya: str, rows: list[dict]) -> None:
    (data_dir / dosya).write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )


def _oku(yol: Path) -> list[dict]:
    with yol.open() as f:
        return list(csv.DictReader(f))


def test_defter_yaz_kolonlar_ve_ay_filtresi(data_dir):
    _yaz(data_dir, "v7_trades.jsonl", [
        {
            "closed_at": "2026-07-10T20:28:31+00:00", "pair": "febu / SOL",
            "entry_price": 0.002, "exit_price": 0.0021,
            "cost_usd": 200.0, "pnl_usd": 8.78,
        },
        {
            "closed_at": "2026-06-30T10:00:00+00:00", "pair": "eski / SOL",
            "entry_price": 1.0, "exit_price": 1.1,
            "cost_usd": 100.0, "pnl_usd": 10.0,
        },
    ])
    yol = denetim.defter_yaz("2026-07")
    assert yol == data_dir / "denetim" / "2026-07_defter.csv"
    rows = _oku(yol)
    assert len(rows) == 1
    r = rows[0]
    assert list(r.keys()) == denetim.KOLONLAR
    assert r["motor"] == "v7"
    assert r["cift"] == "febu / SOL"
    assert float(r["miktar"]) == pytest.approx(200.0 / 0.002)
    assert r["tx_imzasi"] == ""


def test_defter_yaz_coklu_motor_ve_siralama(data_dir):
    _yaz(data_dir, "v6_trades.jsonl", [{
        "closed_at": "2026-07-02T12:00:00+00:00", "pair": "B / SOL",
        "entry_price": 2.0, "exit_price": 2.1, "cost_usd": 50.0, "pnl_usd": 2.5,
    }])
    _yaz(data_dir, "x1_trades.jsonl", [{
        "closed_at": "2026-07-01T12:00:00+00:00", "pair": "A / SOL",
        "entry_price": 4.0, "exit_price": 3.9, "cost_usd": 40.0, "pnl_usd": -1.0,
    }])
    rows = _oku(denetim.defter_yaz("2026-07"))
    assert [r["motor"] for r in rows] == ["x1", "v6"]  # tarihe gore sirali


def test_defter_yaz_legacy_trades_jsonl(data_dir):
    # eski ana kayit: pair_name alani, signature'li canli satir
    _yaz(data_dir, "trades.jsonl", [{
        "closed_at": "2026-07-09T08:31:05+00:00", "pair_name": "BULL / SOL",
        "entry_price": 4.2e-05, "exit_price": 4.0e-05,
        "cost_usd": 6.93, "pnl_usd": -0.38, "signature": "5abcDEF123",
    }])
    rows = _oku(denetim.defter_yaz("2026-07"))
    assert len(rows) == 1
    assert rows[0]["motor"] == "ana"
    assert rows[0]["cift"] == "BULL / SOL"
    assert rows[0]["tx_imzasi"] == "5abcDEF123"


def test_defter_yaz_bos_ay_sadece_baslik(data_dir):
    _yaz(data_dir, "v7_trades.jsonl", [{
        "closed_at": "2026-06-01T00:00:00+00:00", "pair": "X / SOL",
        "entry_price": 1.0, "exit_price": 1.0, "cost_usd": 10.0, "pnl_usd": 0.0,
    }])
    rows = _oku(denetim.defter_yaz("2026-05"))
    assert rows == []


def test_defter_yaz_bozuk_satir_ve_format_hatasi(data_dir):
    (data_dir / "v7_trades.jsonl").write_text(
        "bozuk-json\n" + json.dumps({
            "closed_at": "2026-07-01T00:00:00+00:00", "pair": "Y / SOL",
            "entry_price": 0, "exit_price": 1.0, "cost_usd": 10.0, "pnl_usd": 0.5,
        }) + "\n"
    )
    rows = _oku(denetim.defter_yaz("2026-07"))
    assert len(rows) == 1
    assert rows[0]["miktar"] == ""  # entry_price 0: miktar turetilemez
    with pytest.raises(ValueError):
        denetim.defter_yaz("temmuz")


def test_ay_devri_kontrol_idempotent(data_dir):
    _yaz(data_dir, "v7_trades.jsonl", [{
        "closed_at": "2026-06-15T00:00:00+00:00", "pair": "Z / SOL",
        "entry_price": 1.0, "exit_price": 1.2, "cost_usd": 20.0, "pnl_usd": 4.0,
    }])
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    yol = denetim.ay_devri_kontrol(now)
    assert yol is not None and yol.name == "2026-06_defter.csv"
    assert len(_oku(yol)) == 1
    assert denetim.ay_devri_kontrol(now) is None  # ikinci cagri yazmaz
