"""Tests for src/embed/encode.py — the real bge-m3 wrapper.

Marked network throughout: the pinned revision's weights (~2.27GB) are not
committed, and the first call on any machine downloads them. Excluded from
the default `pytest -m "not network"` run for the same reason every other
live-API test is — src/embed/dedup.py's own tests cover the clustering logic
against a fake embed and need none of this.
"""

from __future__ import annotations

import numpy as np
import pytest

pytestmark = pytest.mark.network


def test_the_pinned_revision_loads_and_produces_bge_m3s_known_dimension():
    from src.embed.encode import embed

    vectors = embed(["삼성전자 실적 발표"])
    assert vectors.shape == (1, 1024)  # bge-m3's documented output size


def test_embeddings_are_l2_normalized():
    from src.embed.encode import embed

    vectors = embed(["삼성전자 실적 발표", "SK하이닉스 신제품 출시"])
    norms = np.linalg.norm(vectors, axis=1)
    assert norms == pytest.approx([1.0, 1.0], abs=1e-5)


def test_identical_text_embeds_identically():
    from src.embed.encode import embed

    a = embed(["같은 문장"])
    b = embed(["같은 문장"])
    assert np.allclose(a, b)


def test_an_empty_input_returns_a_correctly_shaped_empty_array():
    from src.embed.encode import embed

    vectors = embed([])
    assert vectors.shape == (0, 1024)
