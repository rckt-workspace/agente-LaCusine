"""Atribución de anuncios: solo campos documentados por Meta, con tipo y longitud acotados."""

import pytest

from app.models.referral import sanitize_referral


def test_documented_fields_are_kept():
    raw = {"source_type": "ad", "source_id": "120200000000001", "source_url": "https://fb.me/x",
           "ctwa_clid": "ARAkLk-clid", "headline": "no documentado", "body": "texto del anuncio"}
    assert sanitize_referral(raw) == {"source_type": "ad", "source_id": "120200000000001",
                                      "source_url": "https://fb.me/x", "ctwa_clid": "ARAkLk-clid"}


@pytest.mark.parametrize("raw", [None, "ad", 42, [], {}, {"source_type": ""}, {"unrelated": "x"}])
def test_no_usable_attribution_returns_none(raw):
    assert sanitize_referral(raw) is None


def test_oversized_or_wrong_typed_fields_are_dropped_and_others_kept():
    raw = {"source_type": "ad", "source_id": 12345, "source_url": "x" * 501, "ctwa_clid": "abc"}
    assert sanitize_referral(raw) == {"source_type": "ad", "ctwa_clid": "abc"}


def test_values_are_stripped():
    assert sanitize_referral({"source_id": "  987  "}) == {"source_id": "987"}
