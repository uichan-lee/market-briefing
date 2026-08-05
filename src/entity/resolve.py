"""Attach tickers to Korean news articles. SPEC §4.

> If this step fails, everything downstream is meaningless.

SPEC §4 is explicit that this must be deterministic and must not be handed to an
LLM. Every rule here is a string operation over ``config/aliases.yaml``, so the
same corpus resolves the same way on every run, which is what makes the
PREREGISTRATION evaluation reproducible.

The three rules, in the order SPEC §4.2 gives them
--------------------------------------------------
**1. Excluded forms are masked before anything is searched.** Longest first.
This is the part that is easy to get subtly wrong: a plain "does the text
contain an excluded form" test would throw away a genuine 삼성전자 article that
happens to also mention 삼성전자우. Masking removes only the excluded span and
leaves a standalone mention intact.

**2. An alias or a bare ticker code matches.** The code is matched with digit
boundaries so ``005930`` inside a longer run of digits is not a hit.

**3. Anything else that carries only a group name goes to the ambiguous
bucket** — it is not guessed at. SPEC §4.2: better to drop a match than get it
wrong. The daily ambiguous ratio is reported, and SPEC §4.2 item 4 says to
extend the alias file when it passes 30%.

What this does not do
---------------------
It attaches tickers to text. It does not judge whether the article is *about*
the company in a way that should move a feature — a sports report naming 두산,
or a note whose only mention of 키움증권 is the analyst attribution at the end
of the headline, both resolve here and are both noise. That judgment is SPEC
§6.1's relevance scoring, and the two known classes are recorded in
``config/aliases.yaml`` where the aliases that produce them live.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import pandas as pd

from src.util.config import AliasEntry

# SPEC §4.2 item 4.
AMBIGUOUS_THRESHOLD = 0.30

# Masked spans are replaced with a character that cannot occur in the source
# text, so masking can never create a match that was not already there.
_MASK = "\x00"

_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


@dataclass(frozen=True)
class Match:
    """One article-to-ticker attachment, with the evidence that produced it."""

    article_id: str
    ticker: str
    matched_form: str
    match_type: str  # "alias" or "code"


@dataclass
class ResolutionReport:
    """Counts SPEC §4.2 item 4 requires in the briefing header."""

    articles: int = 0
    matched_articles: int = 0
    ambiguous_articles: int = 0
    unmatched_articles: int = 0
    multi_ticker_articles: int = 0
    per_ticker: dict[str, int] = field(default_factory=dict)
    per_form: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def ambiguous_ratio(self) -> float:
        """Ambiguous as a share of everything seen.

        Measured against all articles rather than against matched ones. Most of
        a Korean news feed is about companies nobody here tracks, and dividing
        by matches would make the ratio swing on watchlist size instead of on
        alias quality.
        """
        return self.ambiguous_articles / self.articles if self.articles else 0.0

    @property
    def needs_attention(self) -> bool:
        return self.ambiguous_ratio > AMBIGUOUS_THRESHOLD

    def summary(self) -> str:
        """One line for the briefing header."""
        if not self.articles:
            return "entity: no articles"
        flag = " — ABOVE THRESHOLD, extend config/aliases.yaml" if self.needs_attention else ""
        return (
            f"entity: {self.matched_articles}/{self.articles} articles matched, "
            f"ambiguous {self.ambiguous_ratio:.1%}{flag}"
        )


def _mask_excluded(text: str, entry: AliasEntry) -> str:
    for form in sorted(entry.exclude, key=len, reverse=True):
        text = text.replace(form, _MASK)
    return text


def resolve_article(
    text: str, entries: Mapping[str, AliasEntry], *, article_id: str = ""
) -> tuple[list[Match], set[str]]:
    """Resolve one article's text. Returns (matches, ambiguous tickers).

    A ticker appears in at most one of the two: a ticker whose alias matched is
    resolved, not ambiguous. ``ambiguous`` therefore means "this article talks
    about the group but never says which company", which is the case SPEC §4.2
    says to drop rather than guess.
    """
    matches: list[Match] = []
    ambiguous: set[str] = set()
    codes = set(_CODE.findall(text))

    for ticker, entry in entries.items():
        masked = _mask_excluded(text, entry)

        hit = next((alias for alias in entry.aliases if alias in masked), None)
        if hit is not None:
            matches.append(Match(article_id, ticker, hit, "alias"))
            continue

        # A bare ticker code is unambiguous by construction, so it is checked
        # even when no alias matched — and against the masked text, so a code
        # inside an excluded span still does not count.
        if ticker in codes and ticker in _CODE.findall(masked):
            matches.append(Match(article_id, ticker, ticker, "code"))
            continue

        if any(parent in masked for parent in entry.ambiguous_parents):
            ambiguous.add(ticker)

    return matches, ambiguous


def resolve(
    articles: Iterable[Mapping], entries: Mapping[str, AliasEntry]
) -> tuple[pd.DataFrame, ResolutionReport]:
    """Resolve a corpus.

    Returns one row per (article, ticker) attachment, plus the report. The frame
    is deliberately long rather than one row per article: an article naming both
    삼성전자 and SK하이닉스 is evidence for both, and collapsing it would force a
    choice the data does not support.
    """
    report = ResolutionReport()
    rows: list[Match] = []

    for article in articles:
        report.articles += 1
        text = f"{article.get('title', '')} {article.get('description', '')}"
        matches, ambiguous = resolve_article(
            text, entries, article_id=str(article.get("article_id", ""))
        )

        if matches:
            report.matched_articles += 1
            report.multi_ticker_articles += len({m.ticker for m in matches}) > 1
            for match in matches:
                rows.append(match)
                report.per_ticker[match.ticker] = report.per_ticker.get(match.ticker, 0) + 1
                key = (match.ticker, match.matched_form)
                report.per_form[key] = report.per_form.get(key, 0) + 1
        elif ambiguous:
            report.ambiguous_articles += 1
        else:
            report.unmatched_articles += 1

    frame = pd.DataFrame(
        [(m.article_id, m.ticker, m.matched_form, m.match_type) for m in rows],
        columns=["article_id", "ticker", "matched_form", "match_type"],
    )
    return frame, report


def dead_aliases(
    entries: Mapping[str, AliasEntry], report: ResolutionReport
) -> list[tuple[str, str]]:
    """Aliases that matched nothing in this corpus.

    Not automatically a fault — every English form is dead against Korean-only
    feeds — but a *Korean* alias that never fires is either misspelled or not
    how headlines actually write the name.
    """
    return [
        (ticker, alias)
        for ticker, entry in entries.items()
        for alias in entry.aliases
        if report.per_form.get((ticker, alias), 0) == 0
    ]


def coverage_gaps(
    entries: Mapping[str, AliasEntry], watchlist: Sequence[str], report: ResolutionReport
) -> list[str]:
    """Watchlist tickers with no alias entry, or an entry that never matched."""
    return [
        ticker
        for ticker in watchlist
        if ticker not in entries or report.per_ticker.get(ticker, 0) == 0
    ]
