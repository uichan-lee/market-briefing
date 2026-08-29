"""Guard on the AI commentary. SPEC §2.2⑧.

The commentary is the one place in the briefing where an LLM writes prose about
direction. CLAUDE.md permits it only because it is *downstream* of the computed
rating: it receives the rendered deterministic sections as input, and it may not
originate, alter, or contradict the rating in SPEC §2.2⑥.

This module enforces that mechanically. It is deliberately string matching and
not a second LLM call — CLAUDE.md's determinism rule, and a checker that needed
judgment to verify a judgment would be checking nothing.

**The check and the prompt are a matched pair.** ``src/llm/prompts/v1_synthesis.md``
reserves the seven rating labels as rating vocabulary only, and requires ordinary
market movement be written as 순매수 / 수급 유입 / 수급 이탈 instead. Without that
constraint 매수 appears constantly in normal Korean market prose and this check
would be noise. If the prompt is rewritten, re-read this module.

Results are **reported, not raised**, following the same shape as
:class:`src.collectors.validate.ValidationReport`. On contradiction the renderer
drops the commentary and records the reason in the report header — CLAUDE.md
requires a partial report over no report, and publishing an opinion known to
disagree with the computed rating is worse than publishing neither.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from src.report.rating import Rating, RatingResult
from src.util.config import AliasEntry

_HANGUL = r"가-힣"

# Korean particles as a regex alternation, so 매수는 / 매수로 still read as rating
# claims. Particles are a closed class, which is why an allowlist works here. The
# compounds that must NOT match — 순매수, 매수세, 매수량, 매도호가 — are an open
# class of ordinary market vocabulary and cannot be enumerated, so they are
# excluded structurally instead: a label glued to an adjacent Hangul syllable is
# not a rating label.
_PARTICLES = r"는|은|가|이|를|을|로|으로|의|다|도|와|과|만|에|라|면"


def _label_pattern(label: str) -> re.Pattern[str]:
    """Match ``label`` as a rating claim rather than as part of another word.

    Whitespace inside the label is optional — 강한매수 and 강한 매수 are the same
    claim. Neither side may abut a Hangul syllable, except a trailing particle.
    """
    body = r"\s*".join(re.escape(part) for part in label.split())
    return re.compile(rf"(?<![{_HANGUL}]){body}(?:(?![{_HANGUL}])|(?=(?:{_PARTICLES})))")


# Longest label first, so 매수 is never matched inside 강한 매수 or 약한 매수.
_LABEL_PATTERNS: tuple[tuple[Rating, re.Pattern[str]], ...] = tuple(
    (rating, _label_pattern(rating.value))
    for rating in sorted(Rating, key=lambda r: len(r.value), reverse=True)
)

# These are explicit recommendation compounds, not an open-ended morphology
# rule.  The ordinary market vocabulary around 매수/매도 is deliberately broad;
# expanding this list by substring would turn routine flow commentary into a
# rating claim.  Each compound therefore needs a review and a test before it is
# added here.
_RECOMMENDATION_COMPOUNDS = ("저가매수", "분할매수", "추격매수")
_COMPOUND_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"(?<![{_HANGUL}]){compound}(?:(?![{_HANGUL}])|(?=(?:{_PARTICLES})))")
    for compound in _RECOMMENDATION_COMPOUNDS
)

# Ordinary flow terms that contain a rating word but are not a recommendation.
# Keep this a closed, reviewed list: it protects known false positives without
# turning the guard into an open-ended morphology rule.
_MARKET_VOCABULARY = re.compile(
    rf"(?<![{_HANGUL}])(?:순매수|순매도|매수세|매도세|매수량|매도량|매수호가|매도호가|"
    rf"매수\s+우위|매도\s+우위)(?![{_HANGUL}])"
)


@dataclass(frozen=True)
class Contradiction:
    """A rating label in the commentary that disagrees with the computed one."""

    ticker: str
    stated: Rating
    computed: Rating
    line: str

    def __str__(self) -> str:
        return (
            f"{self.ticker}: commentary says {self.stated}, "
            f"§2.2⑥ computed {self.computed} — {self.line.strip()!r}"
        )


@dataclass(frozen=True)
class UnverifiableLine:
    """A rating claim that cannot be uniquely checked against a KR rating."""

    tickers: tuple[str, ...]
    labels: tuple[Rating, ...]
    line: str
    reason: str


@dataclass
class ConsistencyReport:
    """Outcome of checking one commentary against one day's ratings."""

    contradictions: list[Contradiction] = field(default_factory=list)
    unverifiable: list[UnverifiableLine] = field(default_factory=list)
    checked_lines: int = 0

    @property
    def ok(self) -> bool:
        """Whether the commentary may be published."""
        return not self.contradictions and not self.unverifiable

    def summary(self) -> str:
        """One line, suitable for the briefing header (SPEC §2.1)."""
        if self.ok:
            detail = f"{self.checked_lines} attributed lines consistent"
            return f"commentary: {detail}"
        parts: list[str] = []
        if self.contradictions:
            names = ", ".join(sorted({c.ticker for c in self.contradictions}))
            parts.append(f"{len(self.contradictions)} contradiction(s) with §2.2⑥ ({names})")
        if self.unverifiable:
            parts.append(f"{len(self.unverifiable)} unverifiable rating claim(s)")
        return f"commentary: DROPPED — {'; '.join(parts)}"


def _tickers_in(line: str, aliases: Mapping[str, AliasEntry]) -> tuple[str, ...]:
    """Which watchlist tickers this line mentions, by code or alias.

    Each entry's ``exclude`` terms are masked before its own aliases are matched,
    so 삼성전자우 does not read as a mention of 삼성전자. Masking is per-entry
    rather than global because one ticker's excluded term may legitimately be
    another ticker's alias.

    Masked with ``\\x00`` rather than spaces, matching
    ``src.entity.resolve._mask_excluded`` and for the reason recorded there: the
    replacement must not be able to *create* a match that was not in the text.
    45 of the committed aliases contain a space (``삼성 전자``, ``SK Hynix``), so
    a one-character exclude term blanked to one space could bridge two fragments
    into a false mention. No exclude term is that short today — the shortest is
    three characters — which makes this the cheap kind of fix: it closes the
    hole before someone adds one, and the two modules stop disagreeing about a
    question one of them had already answered.
    """
    found: list[str] = []
    for ticker, entry in aliases.items():
        masked = line
        for term in entry.exclude:
            masked = masked.replace(term, "\x00" * len(term))
        surfaces = (ticker, entry.canonical, *entry.aliases)
        if any(surface and surface in masked for surface in surfaces):
            found.append(ticker)
    return tuple(found)


def _labels_in(line: str) -> tuple[Rating, ...]:
    """Which rating labels this line states, longest match first."""
    remaining = _MARKET_VOCABULARY.sub(lambda m: " " * len(m.group()), line)
    found: list[Rating] = []
    for rating, pattern in _LABEL_PATTERNS:
        if pattern.search(remaining):
            found.append(rating)
            remaining = pattern.sub(lambda m: " " * len(m.group()), remaining)
    if any(pattern.search(remaining) for pattern in _COMPOUND_PATTERNS):
        found.append(Rating.BUY)
    return tuple(found)


def check_commentary(
    text: str,
    ratings: Mapping[str, RatingResult],
    aliases: Mapping[str, AliasEntry],
) -> ConsistencyReport:
    """Check LLM commentary against the computed ratings.

    A rating claim passes only when it can be attributed to exactly one ticker
    with a computed KR rating and every stated label matches that rating.

    **A line stating a label but naming no ticker inherits the subject of its
    paragraph.** Attribution used to be strictly per line, which left the guard
    open to the most ordinary shape Korean prose takes:

        005930 삼성전자의 수급이 개선됐다.
        따라서 강한 매수 의견이다.

    The second line carries the label and no ticker, so it was skipped — the
    commentary could state the exact opposite of §2.2⑥ and the report came back
    ``ok`` with ``checked_lines`` at zero. Since this is the mechanism CLAUDE.md
    rule 3 names as the reason §2.2⑧ is permitted at all, a hole that a
    paragraph break walks through is not an acceptable state to wire ⑧ into.

    The subject carries only while it is uniquely rated, and a blank line ends
    the paragraph and clears it.  A multiple-ticker comparison, an ungraded
    recognized ticker (for example a US ticker), and an unattributed label are
    unsafe to publish rather than merely informational: dropping the section is
    the only fail-closed outcome.  The same applies when this execution has no
    computed ratings at all.
    """
    report = ConsistencyReport()

    if not ratings:
        return ConsistencyReport(
            unverifiable=[
                UnverifiableLine(
                    tickers=(),
                    labels=(),
                    line="",
                    reason="no computed KR ratings",
                )
            ]
        )

    # The paragraph's subject carries only from a line naming exactly one
    # computed KR rating.  ``context_tickers`` remembers why it was cleared so
    # a later bare claim is recorded as unverifiable rather than silently read
    # as a market-level sentence.
    subject: str | None = None
    context_tickers: tuple[str, ...] = ()

    for line in text.splitlines():
        if not line.strip():
            subject, context_tickers = None, ()
            continue

        mentioned = _tickers_in(line, aliases)
        rated = tuple(ticker for ticker in mentioned if ticker in ratings)
        if len(mentioned) == 1 and len(rated) == 1:
            subject, context_tickers = rated[0], ()
        elif mentioned:
            # Crucially, this also clears a previously rated subject when an
            # ungraded ticker appears, preventing subject leakage across US
            # ticker commentary.
            subject, context_tickers = None, mentioned

        labels = _labels_in(line)
        if not labels:
            continue

        if mentioned and not (len(mentioned) == 1 and len(rated) == 1):
            reason = (
                "multiple recognized tickers"
                if len(mentioned) > 1
                else "ticker has no computed KR rating"
            )
            report.unverifiable.append(UnverifiableLine(mentioned, labels, line, reason))
            continue

        ticker = rated[0] if rated else subject
        if ticker is None:
            reason = "unattributed rating claim"
            if context_tickers:
                reason = "unattributable after recognized ticker context"
            report.unverifiable.append(UnverifiableLine(context_tickers, labels, line, reason))
            continue

        computed = ratings[ticker].rating
        report.checked_lines += 1
        for label in labels:
            if label is not computed:
                report.contradictions.append(
                    Contradiction(ticker=ticker, stated=label, computed=computed, line=line)
                )

    return report
