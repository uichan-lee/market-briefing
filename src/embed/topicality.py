"""Topicality filter. SPEC §6.1 Stage 1 (second of two cuts), SPEC §12 step 6.

Cuts a resolved ``(article, ticker)`` match when the article is not really
*about* that ticker — a namesake, a passing mention, a wrong-company analyst
citation — by comparing the article's embedding against a per-ticker profile
sentence and dropping the bottom tail. ``src/entity/resolve.py``'s own
docstring names this exact gap: it "attaches tickers to text" but does not
judge whether the article is about the company in a way that should move a
feature, and calls that SPEC §6.1's job.

**Not to be confused with SPEC §6.2's ``relevance``.** That field measures
P&L *materiality* — does this touch the company's earnings — and the two
constructs are sometimes opposite: a 수급 article ("삼성전자 주가 급등, 외국인
순매수") is maximally *topical* to 삼성전자 but scores low ``relevance`` by
design, because trading flow is not a P&L event. ``notes/step6-plan.md``'s
design section works through this in detail. This module only answers "is
this article about the company at all" — materiality stays Stage 2's job,
already scored and already used as a weight in SPEC §2.2③.

**Profile sentence is derived, not hand-maintained.** ``config/watchlist.yaml``
already carries ``name``/``sector`` per ticker, trusted and validated by
``src.util.config.load_watchlist``. :func:`build_profile_sentence` builds the
comparison text from those two fields alone — no new config file, and nothing
here touches ``config/aliases.yaml``, which CLAUDE.md says must never be
auto-extended.

**What text gets embedded on the article side is a parameter, not a
decision made here.** ``dedup.py``'s calibration found title-only right for
*that* task (paraphrase detection between near-identical headlines); that
finding does not transfer automatically to topicality, where the company's
only mention might sit in a byline the title never carries. Rather than
assume either way, :func:`filter_topicality` takes an injectable
``article_text`` callable (default: title + description joined) so a future
calibration pass — the same shape ``dedup.py``'s did — can compare title-only
against title+description against real labels before either is fixed as the
answer.

**No threshold ships with this module — and, as of calibration, none is
coming.** Unlike ``dedup.py``'s ``DEDUP_THRESHOLD = 0.85``, there is no
``TOPICALITY_THRESHOLD`` constant here, and :func:`filter_topicality`
requires ``threshold`` as an explicit keyword argument. Checked against 149
real labels on 2026-08-22 (``data/golden/topicality_v1.jsonl``,
``notes/step6-plan.md``'s 2026-08-22 status block has the full numbers):
AUC ≈ 0.74 — real signal, not nothing — but the ``topical``/not-``topical``
similarity distributions overlap almost completely (lowest true positive
0.210 sits *below* the lowest false positive 0.214), so no cut threshold
beats the trivial "everything is topical" baseline (0.752 accuracy vs. 0.638
at the best threshold found). **Decision: this module is not wired into the
daily pipeline.** It stays built and tested as a base for a future attempt
with a richer per-ticker profile than ``name + sector`` — shipping a guessed
number now, or a measured-but-useless one, would both be the failure mode
CLAUDE.md's uncertainty section warns about.

**A ticker with a match but no profile sentence is a config bug, not a
soft-fail case.** ``filter_topicality`` raises rather than silently passing
those rows through — CLAUDE.md's "silent failure is the worst outcome"
applies here as much as it does to a collector.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.embed.encode import embed as _default_embed
from src.util.config import WatchlistEntry


def build_profile_sentence(entry: WatchlistEntry) -> str:
    """``"{name} ({ticker}), {sector}"`` — sector omitted when the entry has none.

    Deliberately this thin: ``name``/``sector`` are the only per-ticker fields
    ``config/watchlist.yaml`` carries that describe *what the company is*
    rather than *how to match it in text* (that second kind lives in
    ``config/aliases.yaml`` and is a different, hand-maintained artifact this
    function does not read).
    """
    if entry.sector:
        return f"{entry.name} ({entry.ticker}), {entry.sector}"
    return f"{entry.name} ({entry.ticker})"


def build_profiles(entries: Iterable[WatchlistEntry]) -> dict[str, str]:
    """One profile sentence per ticker, keyed the way :func:`filter_topicality` expects."""
    return {entry.ticker: build_profile_sentence(entry) for entry in entries}


def _default_article_text(article: Mapping[str, object]) -> str:
    title = str(article.get("title", "")).strip()
    description = str(article.get("description", "")).strip()
    return f"{title} {description}".strip()


@dataclass
class TopicalityReport:
    """What the filter did, per ticker — so a silent over-cut is visible."""

    input_pairs: int = 0
    output_pairs: int = 0
    kept: dict[str, int] = field(default_factory=dict)  # ticker -> pairs kept

    @property
    def dropped(self) -> int:
        return self.input_pairs - self.output_pairs

    def summary(self) -> str:
        return (
            f"{self.input_pairs} pairs -> {self.output_pairs} ({self.dropped} dropped as off-topic)"
        )


def filter_topicality(
    matches: pd.DataFrame,
    articles: Mapping[str, Mapping[str, object]],
    profiles: Mapping[str, str],
    *,
    threshold: float,
    embed: Callable[[Sequence[str]], np.ndarray] = _default_embed,
    article_text: Callable[[Mapping[str, object]], str] = _default_article_text,
) -> tuple[pd.DataFrame, TopicalityReport]:
    """Cut ``matches`` to rows whose article-to-profile similarity clears ``threshold``.

    ``matches`` is ``src.entity.resolve.resolve``'s output — one row per
    ``(article_id, ticker)`` attachment; run this after
    ``src.embed.dedup.cluster_duplicates`` so profile similarity is not spent
    on articles dedup would already drop (SPEC §6.1's own Stage 1 ordering:
    dedup, then the relevance/topicality cut). ``articles`` maps
    ``article_id`` to the raw collected row. ``profiles`` maps ``ticker`` to
    its profile sentence, typically :func:`build_profiles`'s output.

    Every ticker present in ``matches`` must have an entry in ``profiles`` —
    a resolved match with no profile sentence means ``config/watchlist.yaml``
    is missing an entry ``resolve()`` produced a match for, and this raises
    rather than passing those rows through untouched.

    The threshold comparison is inclusive (``similarity >= threshold``),
    matching ``dedup.py``'s convention.

    Returns the filtered frame (one row per surviving ``(article_id,
    ticker)``, same columns as ``matches``) and a :class:`TopicalityReport`.
    """
    report = TopicalityReport(input_pairs=len(matches))
    if matches.empty:
        return matches, report

    tickers = matches["ticker"].unique().tolist()
    missing = sorted(t for t in tickers if t not in profiles)
    if missing:
        raise KeyError(
            f"no profile sentence for ticker(s) {missing} — config/watchlist.yaml is "
            "missing an entry that resolve() produced a match for"
        )

    profile_vectors = dict(zip(tickers, embed([profiles[t] for t in tickers]), strict=True))

    kept_rows = []
    for ticker, group in matches.groupby("ticker", sort=False):
        ids = group["article_id"].tolist()
        texts = [article_text(articles.get(aid, {})) for aid in ids]
        vectors = embed(texts)
        similarity = vectors @ profile_vectors[ticker]
        keep_positions = np.where(similarity >= threshold)[0]
        report.kept[ticker] = len(keep_positions)
        kept_rows.append(group.iloc[keep_positions])

    out = pd.concat(kept_rows, ignore_index=True) if kept_rows else matches.iloc[0:0]
    report.output_pairs = len(out)
    return out, report
