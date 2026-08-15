"""Local sentence embeddings. SPEC §6.1 Stage 1, SPEC §12 step 6.

The only module in this package that imports ``sentence_transformers`` —
:mod:`dedup` and :mod:`topicality` take an injectable ``embed`` callable
(default: this module's :func:`embed`) so their clustering/filtering logic is
unit-testable without the optional ``embed`` extra installed, the same
dependency-injection shape ``src/eval/bakeoff.py::run`` uses for its
``scorer`` parameter. ``uv sync --extra embed`` pulls ``sentence-transformers``
and CPU-only ``torch`` (``pyproject.toml``); nothing outside this module
requires either to import.

**Why local embeddings at all, per CLAUDE.md's determinism-first principle:**
zero marginal cost per article (unlike SPEC §6.2's LLM scoring), and — the
sharper reason — genuinely reproducible where an LLM is not even at
``temperature=0`` (SPEC §6.1 has said since v0.1 that LLM output "isn't
perfectly identical run to run"). Re-report detection and topicality
filtering are exactly the kind of judgment string-matching and statistics can
make, which is the CLAUDE.md test for whether an LLM call is justified here —
it is not.

**Revision pinned, not referenced by model name alone.** HuggingFace repos are
mutable; an unpinned revision plus a hard similarity threshold in
:mod:`dedup`/:mod:`topicality` means a silent upstream model update could
shift cluster membership with no record of why, undermining the one property
(reproducibility) that justifies this stage's existence over an LLM call.
``MODEL_REVISION`` was read live via ``HfApi().model_info("BAAI/bge-m3").sha``
on 2026-08-15, not copied from memory or a README.

**Cross-platform float determinism is a known limit, not assumed away.**
The same threshold runs on two architectures — Ricky's Mac (arm64, local
calibration and testing) and ``report.yml``'s ubuntu-latest runner (x86_64,
production). Floating-point embedding output can differ by ~1e-6 between
them; near a hard threshold that is enough to flip cluster membership for a
genuinely borderline pair. Documented here per ``notes/step6-plan.md`` rather
than treated as a hypothetical — this stage is "100% reproducible" relative
to an LLM call, not bit-identical across every machine that runs it.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

MODEL_NAME = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"

# Lazy singleton — loading is a multi-second operation and, on first use on a
# machine without the weights cached, a ~2.27GB download (report.yml caches
# ~/.cache/huggingface keyed on "huggingface-bge-m3" for exactly this reason).
# A module-level load would tax every import of this module, including from
# code that only wants the constants above.
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(MODEL_NAME, revision=MODEL_REVISION)
    return _model


def embed(texts: Sequence[str]) -> np.ndarray:
    """L2-normalized embeddings, one row per text, in the input order.

    Normalized so a caller computes cosine similarity as a plain dot product
    (``dedup.py``/``topicality.py`` both do this) rather than re-deriving the
    normalization at every call site.

    An empty ``texts`` returns a ``(0, dim)`` array rather than raising or
    returning a Python list — callers that concatenate embeddings across
    tickers should not need to special-case a ticker with zero articles.
    """
    if not texts:
        model = _get_model()
        dim = model.get_embedding_dimension()
        return np.empty((0, dim), dtype=np.float32)
    return _get_model().encode(list(texts), normalize_embeddings=True)
