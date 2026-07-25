"""Edge zinciri golge kiyasi (HAT 2, 25 Tem): saf fonksiyon testleri."""

from hibrit_trader.edge.golge import golge_degerlendir


def _skor(**pct):
    return {m: {"pct": p, "islem": 1} for m, p in pct.items()}


def test_uyum_gecis():
    g = golge_degerlendir(_skor(r1=1.0, v7=3.0), "r1", "gecis", "v7",
                          esik=1.5)
    assert g["golge_aday"] == "v7" and g["legacy_hedef"] == "v7"
    assert g["uyum"] is True and g["sapma_nedeni"] is None


def test_uyum_kal():
    g = golge_degerlendir(_skor(r1=3.0, v7=1.0), "r1", "kal", None,
                          esik=1.5)
    assert g["golge_aday"] == "r1" and g["legacy_hedef"] == "r1"
    assert g["uyum"] is True


def test_sapma_cooldown():
    g = golge_degerlendir(_skor(r1=1.0, v7=3.0), "r1", "cooldown", "v7",
                          esik=1.5)
    assert g["golge_aday"] == "v7" and g["legacy_hedef"] == "r1"
    assert g["uyum"] is False
    assert g["sapma_nedeni"] == "legacy_cooldown"


def test_sapma_golge_salter():
    g = golge_degerlendir(_skor(r1=-1.0, v7=0.5), "r1", "kal", None,
                          esik=1.5)
    assert g["golge_aday"] is None                 # pozitif edge yok
    assert g["uyum"] is False
    assert g["sapma_nedeni"] == "golge_salter"


def test_sapma_legacy_salter():
    g = golge_degerlendir(_skor(r1=2.0, v7=1.0), "r1", "sistem_kapali",
                          None, esik=1.5)
    assert g["legacy_hedef"] is None
    assert g["uyum"] is False
    assert g["sapma_nedeni"] == "legacy_salter"


def test_otonom_kapali_pasif_kiyas():
    g = golge_degerlendir(_skor(r1=1.0, v7=3.0), "r1", "otonom_kapali",
                          None, esik=1.5)
    assert g["legacy_hedef"] == "r1"               # pasif = mevcutta kal
    assert g["uyum"] is False
    assert g["sapma_nedeni"] == "legacy_pasif"
    g2 = golge_degerlendir(_skor(r1=3.0, v7=1.0), "r1", "otonom_kapali",
                           None, esik=1.5)
    assert g2["uyum"] is True                      # golge de mevcudu secer


def test_sapma_legacy_filtre():
    """Golge lider secti, legacy filtreleri (min islem/kasa/veto) eledi."""
    g = golge_degerlendir(_skor(r1=1.0, v7=3.0), "r1", "kal", None,
                          esik=1.5)
    assert g["golge_aday"] == "v7" and g["legacy_hedef"] == "r1"
    assert g["sapma_nedeni"] == "legacy_filtre"
