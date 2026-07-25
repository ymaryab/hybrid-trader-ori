"""Kill bataryasi metrikleri: AUC (bagli siralar) ve recall@20."""

import importlib.util
import sys
from pathlib import Path

_yol = Path(__file__).resolve().parents[1] / "scripts" / "kill_bataryasi.py"
_spec = importlib.util.spec_from_file_location("kill_bataryasi", _yol)
kb = importlib.util.module_from_spec(_spec)
sys.modules["kill_bataryasi"] = kb
_spec.loader.exec_module(kb)


def test_auc_mukemmel_ayrism():
    assert kb.auc_mann_whitney([1, 2, 3, 10, 11, 12],
                               [False, False, False, True, True, True]) == 1.0


def test_auc_ters_ayrism_sifir():
    assert kb.auc_mann_whitney([10, 11, 1, 2],
                               [False, False, True, True]) == 0.0


def test_auc_bagli_degerler_yarim():
    assert kb.auc_mann_whitney([5, 5, 5, 5],
                               [True, False, True, False]) == 0.5


def test_recall20_temel():
    # 10 satir, top-2 dilim; en yuksek iki degerden biri runner
    degerler = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    etiketler = [True, False, False, False, True,
                 False, False, False, False, False]
    assert kb.recall_at(degerler, etiketler) == 0.5


def test_recall_ters_yon():
    # dusuk deger = runner (auc<0.5 yonu): ters=True ile yakalanir
    degerler = [1, 2, 9, 10]
    etiketler = [True, False, False, False]
    assert kb.recall_at(degerler, etiketler, ters=True) == 1.0


def test_recall_runner_yoksa_none():
    assert kb.recall_at([1, 2], [False, False]) is None
