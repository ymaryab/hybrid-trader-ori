"""Equity jsonl rotasyonu (momentum/golge/v3): 10MB tavaninda arsive tasi, veri kaybi sifir."""

from __future__ import annotations

import json

import pytest

from hibrit_trader import panel


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    monkeypatch.setattr(panel, "_eq_last_write", {})


def test_rotation_archives_without_data_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "_MOM_EQ_ROTATE_BYTES", 200)  # test için küçük tavan
    p = tmp_path / "momentum_equity.jsonl"
    old_lines = [json.dumps({"ts": 1000.0 + i, "eq": 900.0 + i}) for i in range(20)]
    p.write_text("\n".join(old_lines) + "\n")
    assert p.stat().st_size > 200

    panel._equity_append(tmp_path, "momentum", 999.99)

    archives = list(tmp_path.glob("momentum_equity_arsiv_*.jsonl"))
    assert len(archives) == 1
    # Eski veri arşivde birebir duruyor, aktif dosya taze devam ediyor
    assert archives[0].read_text().splitlines() == old_lines
    active = p.read_text().splitlines()
    assert len(active) == 1
    assert json.loads(active[0])["eq"] == 999.99


def test_same_day_second_rotation_does_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "_MOM_EQ_ROTATE_BYTES", 50)
    p = tmp_path / "momentum_equity.jsonl"
    p.write_text("x" * 100 + "\n")
    panel._equity_append(tmp_path, "momentum", 1.0)
    panel._eq_last_write.clear()
    p.write_text("y" * 100 + "\n")  # aktif dosya yine tavanı aştı (aynı gün)
    panel._equity_append(tmp_path, "momentum", 2.0)
    archives = sorted(tmp_path.glob("momentum_equity_arsiv_*"))
    assert len(archives) == 2  # ikincisi sayaçlı isim aldı, üzerine yazılmadı
    contents = "".join(a.read_text() for a in archives)
    assert "x" in contents and "y" in contents


def test_below_threshold_no_rotation(tmp_path):
    p = tmp_path / "momentum_equity.jsonl"
    p.write_text(json.dumps({"ts": 1.0, "eq": 1.0}) + "\n")
    panel._equity_append(tmp_path, "momentum", 2.0)
    assert list(tmp_path.glob("momentum_equity_arsiv_*")) == []
    assert len(p.read_text().splitlines()) == 2


def test_prefixes_write_isolated_files(tmp_path):
    panel._equity_append(tmp_path, "golge", 1000.0)
    panel._equity_append(tmp_path, "v3", 998.5)
    assert json.loads((tmp_path / "golge_equity.jsonl").read_text())["eq"] == 1000.0
    assert json.loads((tmp_path / "v3_equity.jsonl").read_text())["eq"] == 998.5
    assert not (tmp_path / "momentum_equity.jsonl").exists()


def test_rotation_uses_prefix_archive_name(tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "_MOM_EQ_ROTATE_BYTES", 50)
    p = tmp_path / "golge_equity.jsonl"
    p.write_text("z" * 100 + "\n")
    panel._equity_append(tmp_path, "golge", 3.0)
    archives = list(tmp_path.glob("golge_equity_arsiv_*.jsonl"))
    assert len(archives) == 1
    assert archives[0].read_text() == "z" * 100 + "\n"


def test_throttle_is_per_prefix(tmp_path):
    panel._equity_append(tmp_path, "momentum", 1.0)
    panel._equity_append(tmp_path, "momentum", 2.0)  # 4sn dolmadı: yazılmaz
    panel._equity_append(tmp_path, "golge", 3.0)     # farklı prefix: yazılır
    assert len((tmp_path / "momentum_equity.jsonl").read_text().splitlines()) == 1
    assert len((tmp_path / "golge_equity.jsonl").read_text().splitlines()) == 1


def test_live_equity_definition():
    state = {
        "balance": 800.0,
        "positions": [
            {"amount_token": 100.0, "last_price": 1.5},
            {"amount_token": 10.0, "last_price": 2.0},
        ],
    }
    assert panel._live_equity(state) == 970.0
    assert panel._live_equity({"balance": 500.0}) == 500.0
    assert panel._live_equity({}) == 0.0


def test_equity_series_live_tip_from_state(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    state = {
        "start_balance": 1000.0,
        "balance": 800.0,
        "positions": [{"amount_token": 100.0, "last_price": 1.5}],
    }
    (tmp_path / "v10_state.json").write_text(json.dumps(state))
    (tmp_path / "v10_equity.jsonl").write_text(
        json.dumps({"ts": 200.0, "eq": 990.0}) + "\n"
    )
    out = panel._equity_series("v10", 0)
    ts_ms, eq = out["points"][-1]
    # canli uc nokta: ust ozetle ayni formul, istek aninda
    assert eq == panel._live_equity(state) == 950.0
    assert abs(ts_ms / 1000 - _time.time()) < 5


def test_api_summary_equity_equals_series_tip(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    state = {
        "start_balance": 1000.0,
        "balance": 800.0,
        "realized_pnl": 5.0,
        "positions": [{"amount_token": 100.0, "last_price": 1.5}],
    }
    (tmp_path / "v10_state.json").write_text(json.dumps(state))
    out = panel.api_v10(limit=5)
    series = panel._equity_series("v10", 0)
    assert out["summary"]["equity"] == 950.0
    assert series["points"][-1][1] == 950.0


def test_equity_series_no_live_tip_without_balance(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    (tmp_path / "v9_state.json").write_text(json.dumps({"start_balance": 1000.0}))
    (tmp_path / "v9_equity.jsonl").write_text(
        json.dumps({"ts": 200.0, "eq": 990.0}) + "\n"
    )
    out = panel._equity_series("v9", 0)
    assert out["points"] == [[200000, 990.0]]


def test_equity_series_reads_prefix_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATA_DIR", str(tmp_path))
    (tmp_path / "golge_state.json").write_text(json.dumps({"start_balance": 1000.0}))
    (tmp_path / "golge_trades.jsonl").write_text(
        json.dumps({"ts": 100.0, "hold_sec": 60.0, "pnl_usd": 5.0}) + "\n"
    )
    (tmp_path / "golge_equity.jsonl").write_text(
        json.dumps({"ts": 200.0, "eq": 1002.5}) + "\n"
    )
    out = panel._equity_series("golge", 0)
    assert out["start_balance"] == 1000.0
    # çapa ($1000) + trade kümülatifi (1005) + panel örneklemi (1002.5)
    assert [pt[1] for pt in out["points"]] == [1000.0, 1005.0, 1002.5]
