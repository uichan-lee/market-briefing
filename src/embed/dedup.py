"""Re-report clustering. SPEC §6.1 Stage 1 (first of two cuts), SPEC §12 step 6.

Korean outlets republish the same wire story across multiple outlets with
minor rewording (see ``config/news_feeds.yaml:29-33`` on why: outlet body
length alone ranges 0–~1,155 chars, so a headline-only 한국경제 rewrite and a
full-body 뉴시스 rewrite of the same event look nothing alike as raw text).
This module collapses those into one representative per cluster before
SPEC §6.2's LLM scoring ever sees them, so the same event is not paid for
and counted twice.

**Embeds title only, not title+description — measured, not the plan's first
guess.** ``notes/step6-plan.md`` flagged the choice as its highest-leverage
open question: title+description is richer but the description-length
asymmetry above was hypothesized to break cosine similarity between short and
long representations of an identical story. Calibrated 2026-08-15 against
``data/golden/triage.jsonl``'s 148 unique articles — 7 known same-event pairs
found via ``scripts.golden._same_event`` (title-token Jaccard ≥ 0.5),
contrasted against 527 same-ticker non-duplicate pairs:

======================  ==================================  ======================================
strategy                known-dup pairs (cross-outlet only)  same-ticker non-dup pairs
======================  ==================================  ======================================
title-only              min 0.875, mean 0.951                max 0.901, mean 0.439 — 2 pairs above
title+description       min 0.696, mean 0.865                max 0.968, mean 0.520 — 59 pairs above
======================  ==================================  ======================================

Title-only gives near-clean separation against the crude ground truth (2
non-dup pairs cross the lowest known-dup score out of 527); title+description
does not (59). And the "violations" are not false positives — every one of
the 8 highest-scoring "non-dup" pairs inspected by hand (down to 0.799) reads
as a genuine cross-outlet re-report (신한 슈퍼SOL's MAU milestone reported
three separate times, 삼성물산 홈닉's launch, 기아's July sales — same figures
worded differently, 미래에셋's robo-wrap launch twice, 삼성증권's Q2 earnings)
that ``_same_event``'s cheap title-token-Jaccard heuristic missed entirely.
So bge-m3 title-only similarity is, on this evidence, a *better* duplicate
detector than the ground truth it was checked against — which also means
**this calibration has not actually located a confirmed genuine
non-duplicate near the threshold**, only confirmed that everything checked
near it turned out to be an uncaught duplicate. The honest statement is
narrower than "clean separation": every known duplicate clears 0.85, and
every "non-duplicate" pair inspected above 0.85 (down to 0.799) turned out to
be mislabeled ground truth rather than a real counterexample. Below ~0.80 the
pool was not inspected pair-by-pair; the full 527-pair mean (0.439) is the
only evidence there.

**Threshold set to 0.85, not SPEC §6.1's placeholder 0.92 — calibration says
0.92 is too high.** The lowest real cross-outlet duplicate scored 0.875;
0.92 would have missed it and one other (2 of 5 non-trivial known dups
false-negative). **Caveat stated against the number, not hidden**: this rests
on 7 known-duplicate pairs from one day's triage sample — a small n — and on
hand-inspection rather than an exhaustive negative set, so 0.85 should be
re-checked once more re-reported stories accumulate, the same spirit
MANUAL-TASKS §4 applies to a short-gap noise-floor measurement. What it does
not rest on is an unverified guess: every number above was measured, not
assumed, 2026-08-15.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.embed.encode import embed as _default_embed

DEDUP_THRESHOLD = 0.85


@dataclass
class DedupReport:
    """What clustering did, per ticker — so a silent over-collapse is visible."""

    input_pairs: int = 0
    output_pairs: int = 0
    clusters: dict[str, int] = field(default_factory=dict)  # ticker -> cluster count

    @property
    def dropped(self) -> int:
        return self.input_pairs - self.output_pairs

    def summary(self) -> str:
        return (
            f"{self.input_pairs} pairs -> {self.output_pairs} "
            f"({self.dropped} dropped as re-reports)"
        )


def _cluster_indices(similarity: np.ndarray, threshold: float) -> list[int]:
    """Union-find over a pairwise similarity matrix; returns each row's cluster id.

    Connected components rather than greedy pairwise linking: A~B and B~C
    both clearing the threshold should merge A, B and C into one cluster even
    if A~C alone would not clear it on its own — the same transitive-closure
    behavior ``scripts/golden.py``'s ``_same_event``-driven grouping assumes
    implicitly. A dedicated union-find is used rather than a graph library,
    since the input here is at most a few hundred articles for one ticker on
    one day and the operation is a few lines either way.
    """
    n = similarity.shape[0]
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if similarity[i, j] >= threshold:
                union(i, j)

    return [find(i) for i in range(n)]


def cluster_duplicates(
    matches: pd.DataFrame,
    articles: Mapping[str, Mapping[str, object]],
    *,
    threshold: float = DEDUP_THRESHOLD,
    embed: Callable[[Sequence[str]], np.ndarray] = _default_embed,
) -> tuple[pd.DataFrame, DedupReport]:
    """Collapse re-reported stories to one representative per cluster, per ticker.

    ``matches`` is ``src.entity.resolve.resolve``'s output — one row per
    ``(article_id, ticker)`` attachment. ``articles`` maps ``article_id`` to
    the raw collected row (``title``/``description`` read; other keys
    ignored). Clustering is **within a ticker's own matched articles**, not
    across the whole corpus — already the correct scope per
    ``config/news_feeds.yaml``'s clustering note, since two different
    companies genuinely covered by the same wire story are two separate
    judgments (SPEC §6.2 scores per ticker), not a duplicate.

    The representative kept per cluster is the member with the **longest
    ``description``** — ``config/news_feeds.yaml``'s own stated rule, so a
    한국경제 headline-only stub does not survive over a 뉴시스 item carrying
    the full body. Ties broken on ``article_id`` for determinism.

    Returns the filtered frame (one row per surviving ``(article_id,
    ticker)``, same columns as ``matches``) and a :class:`DedupReport`.
    """
    report = DedupReport(input_pairs=len(matches))
    if matches.empty:
        return matches, report

    kept_rows = []
    for ticker, group in matches.groupby("ticker", sort=False):
        ids = group["article_id"].tolist()
        titles = [str(articles.get(aid, {}).get("title", "")) for aid in ids]
        vectors = embed(titles)

        if len(ids) == 1:
            report.clusters[ticker] = 1
            kept_rows.append(group)
            continue

        similarity = vectors @ vectors.T
        cluster_ids = _cluster_indices(similarity, threshold)
        report.clusters[ticker] = len(set(cluster_ids))

        by_cluster: dict[int, list[int]] = {}
        for row_pos, cluster_id in enumerate(cluster_ids):
            by_cluster.setdefault(cluster_id, []).append(row_pos)

        desc_lengths = [len(str(articles.get(aid, {}).get("description", ""))) for aid in ids]

        keep_positions = []
        for members in by_cluster.values():
            best = max(members, key=lambda pos: (desc_lengths[pos], ids[pos]))
            keep_positions.append(best)

        kept_rows.append(group.iloc[sorted(keep_positions)])

    out = pd.concat(kept_rows, ignore_index=True) if kept_rows else matches.iloc[0:0]
    report.output_pairs = len(out)
    return out, report
