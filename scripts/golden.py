"""Build the golden set. SPEC §7.3, MANUAL-TASKS §4.

    uv run python -m scripts.golden sample     # 1. pick candidates
    uv run python -m scripts.golden triage     # 2. sort into four buckets  (~17 min)
    uv run python -m scripts.golden review     # 3. revisit the rule-flagged  (~5 min)
    uv run python -m scripts.golden label      # 4. score the chosen 100    (~30 min)
    uv run python -m scripts.golden recheck    # 4. next day, re-label ten
    uv run python -m scripts.golden verify     # 5. check the result

**No LLM touches any label, and none ever may.** The golden set exists to measure
how well models score articles (SPEC §7.4). A model that pre-filled the labels
would be marking its own paper, and a human correcting a pre-filled number
anchors to it — the measurement would come out flattering and nothing downstream
could detect that it had. Everything automated here is clerical: which articles
to look at, in what order, and whether the finished file is well-formed.

**Two passes, because the buckets cannot be known before reading.** SPEC asks for
25 clearly positive / 25 clearly negative / 25 ambiguous / 25 irrelevant, which
is a property of the article, so the first pass is a four-way sort at a few
seconds each and the second scores only the survivors on all five dimensions.
Labelling 200 articles on five dimensions to keep 100 would cost twice the time
for the same result.

Every pass is append-only and resumable: quit whenever, re-run to continue.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import random
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GOLDEN = ROOT / "data" / "golden"

CANDIDATES = GOLDEN / "candidates.jsonl"
TRIAGE = GOLDEN / "triage.jsonl"
LABELS = GOLDEN / "v1.jsonl"
RECHECK = GOLDEN / "recheck.jsonl"
REVIEW = GOLDEN / "review.jsonl"

# SPEC §7.3 composition: key → (stored name, prompt label, progress label).
# The progress label is separate because "명백한 긍정" and "명백한 부정" share a
# first word, and a counter reading "명백한 3/25  명백한 1/25" tells Ricky
# nothing about which bucket still needs filling.
BUCKETS = {
    "1": ("positive", "명백한 긍정", "긍정"),
    "2": ("negative", "명백한 부정", "부정"),
    "3": ("ambiguous", "애매", "애매"),
    "4": ("irrelevant", "무관 (종목은 언급되나 실적·주가와 무관)", "무관"),
}
PER_BUCKET = 25

# Candidates to triage. Twice the final size plus slack: buckets fill at very
# different rates — 무관 is common, 명백한 부정 is not — so a pool of exactly
# 100 would run out of one bucket long before the others.
POOL_SIZE = 240

# The five dimensions, in the order the prompt asks for them. SPEC §6.2.
#
# Each carries anchors, because the one-line description alone left the middle
# of every scale undefined and "0.3 vs 0.7" was being decided fresh each time.
# The anchors are stated as *rules about the article*, never as worked examples
# from the corpus — an example drawn from the pool would put a suggested answer
# next to an article that is about to be scored.
DIMENSIONS = (
    # `relevance` used to read "실적·주가와 얼마나 관련되나" while every anchor
    # below it was written on 손익. 수급 기사 is exactly where the two part: a
    # 매매동향 column is all about 주가 and touches no line of the income
    # statement. Ricky labelled the first pass by the summary line and gave six
    # 매일경제 「주식 초고수는 지금」 articles relevance 0.6–0.9, which was
    # faithful to the text he was shown and inconsistent with the ladder.
    #
    # Resolved toward 손익 on 2026-08-07, for two reasons. It puts `relevance`
    # on the same axis as `intensity` — does this move a line of this company's
    # P&L — so the five dimensions measure five different things. And
    # `news_polarity` is a *relevance-weighted* average polarity (SPEC §2.2③),
    # so a high-relevance 매매동향 column would enter the rating with the weight
    # of an earnings surprise, on top of the flow it already contributes through
    # `foreign_flow_5d`.
    #
    # The six affected scores are re-asked by `label --redo relevance`; the rule
    # in `score_conflicts` is what finds them.
    (
        "relevance",
        0.0,
        1.0,
        "이 회사의 손익에 얼마나 닿나",
        (
            "0.0  이름만 스쳐감 — 리포트 작성 증권사, 업종 나열, 인사·채용·게시판",
            "0.3  회사 얘기지만 손익과 연결이 멀다 — 행사, MOU, 수상, 사회공헌",
            "0.7  본업에 닿는다 — 신제품, 수주, 증설, 점유율, 경쟁구도",
            "1.0  숫자가 직접 나온다 — 실적, 가이던스, 계약금액, 목표주가",
            "수급·매매동향은 주가 얘기지만 손익에는 닿지 않는다 — 0.0~0.3.",
        ),
    ),
    (
        "polarity",
        -1.0,
        1.0,
        "방향만. 크기는 intensity가 받는다",
        (
            "-1.0  명백히 악재",
            " 0.0  방향 없음 또는 호악재가 맞물림",
            "+1.0  명백히 호재",
            "기준: 이 기사만 읽은 투자자가 주식을 더 사고 싶어지는가, 팔고 싶어지는가.",
            "질문한 종목 기준으로 판단한다 — A가 B에 밀렸다는 기사는 A와 B의 부호가 다르다.",
        ),
    ),
    (
        "intensity",
        0.0,
        1.0,
        "재무적 충격의 크기",
        (
            "0.0  방향은 있으나 금액으로 환산되지 않는다",
            "0.3  단발성이거나 매출의 1% 미만 수준",
            "0.7  분기 실적을 눈에 띄게 움직인다",
            "1.0  연간 실적이나 사업 구조를 바꾼다",
            "polarity와 독립이다. 큰 악재도 intensity는 1.0이다.",
        ),
    ),
    (
        "uncertainty",
        0.0,
        1.0,
        "그 결과가 실제로 일어날지",
        (
            "0.0  이미 확정 — 발표된 실적, 체결된 계약, 집행된 처분",
            "0.3  공시·계약은 됐으나 이행이 남음",
            "0.7  전망·목표·계획 — 증권사 추정, 회사 가이던스",
            "1.0  추측 — '검토 중', '알려졌다', 익명 소식통",
        ),
    ),
    (
        "forwardness",
        0.0,
        1.0,
        "0=이미 반영된 과거, 1=미래 기대를 바꿈",
        (
            "0.0  이미 알려진 사실의 반복·정리 기사",
            "0.3  지난 분기에 일어난 일의 확인",
            "0.7  앞으로 몇 분기의 기대를 바꾼다",
            "1.0  처음 나온 정보이고 중기 전망을 다시 짜게 한다",
            "uncertainty와 다르다. 확정된 사실도 처음 알려졌다면 forwardness는 높다.",
        ),
    ),
)

# Items re-labelled a day later to measure Ricky against himself (SPEC §7.3).
RECHECK_SIZE = 10


# --- reading the corpus ----------------------------------------------------


def load_articles() -> dict[str, dict]:
    """Every collected article, de-duplicated by ``article_id``."""
    articles: dict[str, dict] = {}
    for path in sorted(glob.glob(str(RAW / "kr" / "news" / "*" / "*.jsonl.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                articles.setdefault(row["article_id"], row)
    return articles


def matched_pairs(articles: dict[str, dict]) -> list[tuple[str, str]]:
    """``(article_id, ticker)`` for every article the resolver attaches.

    The golden set scores *pairs*, not articles: SPEC §6.2's schema carries a
    ticker, and an article naming two companies is two judgments. Only matched
    articles are eligible — an article mentioning no watchlist name would be
    trivially irrelevant and would never reach the scoring stage in production
    either, so including it would measure nothing.
    """
    from src.entity.resolve import resolve
    from src.util.config import load_aliases

    matches, _ = resolve(articles.values(), load_aliases())
    if matches.empty:
        return []
    return [(str(row.article_id), str(row.ticker)) for row in matches.itertuples()]


# --- sampling --------------------------------------------------------------


def stratified_sample(
    pairs: Sequence[tuple[str, str]],
    articles: dict[str, dict],
    *,
    size: int = POOL_SIZE,
    seed: int = 20260806,
) -> list[tuple[str, str]]:
    """Spread the pool across tickers and days rather than sampling uniformly.

    Uniform sampling would hand back the corpus's own skew: measured
    2026-08-06, 삼성전자 held 138 of 479 pairs, so 29% of a random golden set
    would be one company and the bake-off would mostly measure how well a model
    reads Samsung coverage. Round-robin over tickers fixes that, and shuffling
    each ticker's own articles with a fixed seed keeps the choice reproducible
    while stopping a single busy day from filling a ticker's slots.
    """
    by_ticker: dict[str, list[tuple[str, str]]] = {}
    for article_id, ticker in pairs:
        by_ticker.setdefault(ticker, []).append((article_id, ticker))

    rng = random.Random(seed)
    for ticker in sorted(by_ticker):
        # Sort before shuffling: dict iteration order is stable but the file
        # order that produced it is not guaranteed, and the seed only makes the
        # shuffle reproducible if its input is.
        items = sorted(by_ticker[ticker], key=lambda pair: _published(articles, pair[0]))
        rng.shuffle(items)
        by_ticker[ticker] = items

    chosen: list[tuple[str, str]] = []
    tickers = sorted(by_ticker, key=lambda t: (-len(by_ticker[t]), t))
    while len(chosen) < size and any(by_ticker.values()):
        for ticker in tickers:
            if not by_ticker[ticker]:
                continue
            chosen.append(by_ticker[ticker].pop())
            if len(chosen) >= size:
                break
    return chosen


def _published(articles: dict[str, dict], article_id: str) -> str:
    return str(articles.get(article_id, {}).get("published_at", ""))


def write_candidates(pairs: Iterable[tuple[str, str]], articles: dict[str, dict]) -> int:
    from src.util.config import load_watchlist

    names = {entry.ticker: entry.name for entry in load_watchlist(market="KR")}
    GOLDEN.mkdir(parents=True, exist_ok=True)
    written = 0
    with CANDIDATES.open("w", encoding="utf-8") as handle:
        for article_id, ticker in pairs:
            article = articles[article_id]
            handle.write(
                json.dumps(
                    {
                        "article_id": article_id,
                        "ticker": ticker,
                        "name": names.get(ticker, ""),
                        "title": article.get("title", ""),
                        "description": article.get("description", ""),
                        "outlet": article.get("outlet", ""),
                        "link": article.get("link", ""),
                        "published_at": article.get("published_at", ""),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            written += 1
    return written


# --- append-only progress files -------------------------------------------


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    """Append one decision immediately.

    Written per item rather than at the end so that quitting — or a closed
    terminal — never costs work already done. Two hours of judgment is the most
    expensive thing in this repository.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def key_of(row: dict) -> tuple[str, str]:
    return (row["article_id"], row["ticker"])


def latest_triage(triaged: Sequence[dict]) -> list[dict]:
    """One row per example, the most recent decision winning.

    ``triage.jsonl`` is append-only, so a label changed during `review` lands as
    a second row for the same key rather than replacing the first. Nothing Ricky
    typed is ever rewritten; the readers resolve it. Original order is kept so
    that `select_for_labelling` still picks in triage order — a changed label
    keeps its original position rather than jumping to the end of its bucket.
    """
    order: list[tuple[str, str]] = []
    newest: dict[tuple[str, str], dict] = {}
    for row in triaged:
        key = key_of(row)
        if key not in newest:
            order.append(key)
        newest[key] = row
    return [newest[key] for key in order]


# --- selection after triage ------------------------------------------------


SAME_EVENT_SIMILARITY = 0.5
"""Title overlap above which two *different* articles are one event.

Jaccard over title tokens. Measured against the first real triage pass: at 0.5
every pair it caught was genuinely the same story carried by several outlets —
한화에어로's UAM contract cancellation appeared three times, HD현대일렉's
transformer milestone twice — and nothing distinct was merged.
"""


def _title_tokens(title: str) -> set[str]:
    return set(re.findall(r"[가-힣A-Za-z0-9]+", title or ""))


def _same_event(a: dict, b: dict) -> bool:
    """Whether two rows are the same story reported twice.

    Deliberately *not* true when two rows share an ``article_id`` and differ by
    ticker. One article naming both 현대차 and 기아 is two different examples —
    the model has to condition its answer on which ticker it was asked about,
    and that is exactly the ability worth measuring. Only distinct articles
    telling the same story are redundant.
    """
    if a["article_id"] == b["article_id"]:
        return False
    first, second = _title_tokens(a.get("title", "")), _title_tokens(b.get("title", ""))
    union = first | second
    if not union:
        return False
    return len(first & second) / len(union) >= SAME_EVENT_SIMILARITY


def select_for_labelling(
    triaged: Sequence[dict], *, per_bucket: int = PER_BUCKET
) -> tuple[list[dict], dict[str, int]]:
    """The 100 that go on to full scoring, and how full each bucket is.

    Takes the first ``per_bucket`` of each bucket in triage order, so the
    selection is a function of what Ricky did rather than of a second random
    draw he cannot see.

    One story reported by several outlets is taken once. The first triage pass
    put 한화에어로's UAM cancellation into the 부정 bucket three times, from
    매일경제, 전자신문 and 연합뉴스 — 12% of that bucket for a single event. A
    model that happens to read that one story well would score as though it read
    Korean market news well, which is the same skew `stratified_sample` exists
    to prevent one level up. Dropping the repeats also refills the slot from the
    next candidate, so the bucket still reaches `per_bucket`.

    Only *distinct articles* are collapsed. See :func:`_same_event` for why a
    single article attached to two tickers is kept twice.
    """
    picked: list[dict] = []
    counts: dict[str, int] = {name: 0 for name, _, _ in BUCKETS.values()}
    for row in latest_triage(triaged):
        bucket = row["bucket"]
        if counts.get(bucket, 0) >= per_bucket:
            continue
        if any(chosen["bucket"] == bucket and _same_event(chosen, row) for chosen in picked):
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
        picked.append(row)
    return picked, counts


# --- flags for review ------------------------------------------------------
#
# Every flag below is a **rule**, never an opinion about a particular article.
# That distinction is the whole design. Ricky asked for suspicious labels to be
# surfaced rather than corrected, and the obvious way to do that would be for
# Claude to read the set and say which ones look wrong — which is the golden
# set's one forbidden move wearing a different hat. Flags chosen per article
# would still steer the labels toward the model that chose them, and would do it
# invisibly, because only the disagreements get surfaced.
#
# A rule is safe where a judgement is not, for three reasons: it applies to
# every article alike rather than to the ones a model happened to dislike, it is
# written down and auditable before anyone sees an answer, and it points at a
# *conflict between two of Ricky's own decisions* rather than at a preferred
# label. `run_review` never shows a suggested bucket for that last reason.
#
# CLAUDE.md's determinism-first rule reaches the same place from the other
# direction: this is string matching over labels Ricky already made, so an LLM
# call here would be both unjustified and unsafe.

# Watchlist names whose articles are usually *about somebody else*. A brokerage
# is named as the author of a view far more often than as the subject of news,
# and the resolver cannot tell the two apart.
_AUTHOR_LIKE = ("증권", "금융지주", "자산운용")

# A brokerage is the author, not the subject, when its name appears next to one
# of these. Deliberately narrow: matching "밝혔다" alone would catch the
# company's own announcements too.
_AUTHOR_MARKERS = (
    "목표가",
    "목표주가",
    "투자의견",
    "제시했다",
    "평가했다",
    "진단했다",
    "분석이 나왔다",
    "전망했다",
    "대해",
)


def _is_author_mention(row: dict) -> bool:
    """Whether a brokerage row reads as the author of a view about someone else."""
    if not any(marker in row.get("name", "") for marker in _AUTHOR_LIKE):
        return False
    text = f"{row.get('title', '')} {row.get('description', '')}"
    short = row.get("name", "")[:4]
    # Its own results are the counter-case and must never be flagged: the name
    # sits next to its own numbers rather than next to a view about a third party.
    own_news = ("영업이익", "순이익", "실적", "출시", "협약", "MOU", "승진", "채용")
    if any(word in row.get("title", "") for word in own_news) and short in row.get("title", ""):
        return False
    return any(marker in text for marker in _AUTHOR_MARKERS)


def find_flags(triaged: Sequence[dict]) -> list[dict]:
    """Label pairs that conflict with each other, or with a stated rule.

    Returns one entry per row worth a second look, each carrying the reason and
    the evidence — never a proposed answer.
    """
    flags: list[dict] = []

    # 1. The same story, the same ticker, two different buckets. Whatever the
    #    right answer is, both cannot be it.
    for index, row in enumerate(triaged):
        for other in triaged[index + 1 :]:
            if row["ticker"] != other["ticker"] or row["bucket"] == other["bucket"]:
                continue
            if not _same_event(row, other):
                continue
            for side, against in ((row, other), (other, row)):
                flags.append(
                    {
                        "article_id": side["article_id"],
                        "ticker": side["ticker"],
                        "flag": "contradiction",
                        "reason": (
                            f"같은 사건인데 버킷이 다릅니다: 이 건은 '{side['bucket']}', "
                            f"'{against['title'][:40]}'({against['outlet']})는 "
                            f"'{against['bucket']}'"
                        ),
                    }
                )

    # 2. One article, several tickers, all in one bucket. Correct whenever the
    #    story is good or bad for everyone in it, and wrong whenever it is a
    #    comparison — which the rule cannot tell apart, so it asks.
    by_article: dict[str, list[dict]] = {}
    for row in triaged:
        by_article.setdefault(row["article_id"], []).append(row)
    for group in by_article.values():
        if len(group) < 2 or len({row["bucket"] for row in group}) != 1:
            continue
        if group[0]["bucket"] not in {"positive", "negative"}:
            continue
        names = ", ".join(row["name"] for row in group)
        for row in group:
            flags.append(
                {
                    "article_id": row["article_id"],
                    "ticker": row["ticker"],
                    "flag": "shared_sign",
                    "reason": (
                        f"기사 하나에 {names} 가 모두 '{group[0]['bucket']}'입니다. "
                        f"한쪽이 다른 쪽에 밀렸다는 내용이면 부호가 갈려야 합니다."
                    ),
                }
            )

    # 3. A brokerage matched as the author of a view about a third party.
    #    Silent once the row already sits in 무관: Ricky's standing rule
    #    (MANUAL-TASKS §4) is that an author mention belongs there, so a row
    #    that agrees with the rule has nothing left to decide. Flagging it
    #    anyway would bury the rows that do disagree.
    for row in triaged:
        if row["bucket"] != "irrelevant" and _is_author_mention(row):
            flags.append(
                {
                    "article_id": row["article_id"],
                    "ticker": row["ticker"],
                    "flag": "author_mention",
                    "reason": (
                        f"{row['name']}이 다른 회사에 대한 견해의 '작성자'로 잡혔습니다. "
                        f"기사의 주어가 이 회사인지 확인이 필요합니다."
                    ),
                }
            )

    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for flag in flags:
        key = (flag["article_id"], flag["ticker"], flag["flag"])
        if key not in seen:
            seen.add(key)
            unique.append(flag)
    return unique


# --- rules over the scores -------------------------------------------------
#
# The same device as `find_flags`, one stage later. `find_flags` compares two of
# Ricky's bucket decisions against each other; these compare a number he gave
# against a bucket he gave, or against a rule he stated himself. Neither ever
# proposes a value, for the reason at the top of this file: `polarity` is what
# the bake-off measures most directly, so a number originating with a model
# would turn the bake-off into a similarity test against that model.
#
# Everything here is a warning, never a verify failure. A conflict means two of
# Ricky's decisions disagree; which one gives way is his call, and a check that
# forced a change would be supplying the answer through the back door.

# Titles about who is buying, not about what the company earns. Narrow on
# purpose: "실적" or "매출" would catch the articles this rule exists to spare.
_MARKET_STRUCTURE = (
    "초고수",
    "차익실현",
    "차익 실현",
    "수급",
    "매수세",
    "매도세",
    "공매도",
    "패시브",
    "지수 편입",
    "거래대금",
    "순매수",
    "순매도",
)

# A price move is not a P&L event, but a business event is often *reported*
# through the price it caused — "테슬라 공급 싹쓸이에…삼성전기 급등" is a supply
# contract wearing a price headline. Matching 급등/급락 alone would flag those
# too and bury the rows worth seeing, so a market-side cause is also required.
_PRICE_MOVE = ("폭락", "급등", "급락", "반등", "신고가", "신저가", "상한가", "하한가", "특징주")
_MARKET_CAUSE = ("외인", "외국인", "기관", "투자심리", "투자 심리", "동시호가", "투자주의", "수급")

# Above this, a number is disagreeing with its bucket rather than shading it.
_SIGN_CONFLICT = 0.4
_STRUCTURE_RELEVANCE = 0.5

# `relevance` and `intensity` ask the same question — does a line of *this
# company's* P&L move — at two magnitudes. Saying no to the first and yes to
# the second is a contradiction whichever number turns out to be wrong.
_DETACHED_RELEVANCE = 0.3
_DETACHED_INTENSITY = 0.7

# Two write-ups of one event, scored far apart. Looser than
# `SAME_EVENT_SIMILARITY` on purpose: that constant decides which articles are
# *selected*, so loosening it now would drop scored rows and pull in unscored
# replacements. This one only flags, so it can afford to ask about pairs that
# are merely probably the same story — SK실트론's 신용등급 하향검토 was written
# up by 나이스신평 and by 신평 3사 with a 0.38 overlap, under the selection
# threshold and plainly one event.
_DIVERGENCE_SIMILARITY = 0.35
_DIVERGENCE_GAP = 0.4


def score_conflicts(rows: Sequence[dict]) -> list[dict]:
    """Scores that contradict the bucket beside them, or a rule Ricky stated.

    Takes collated rows — one per (article, ticker), carrying ``bucket`` and
    whichever dimensions have been scored so far — so it works mid-run on
    partial progress as well as on the finished set.
    """
    conflicts: list[dict] = []

    def add(row: dict, dimension: str, reason: str) -> None:
        conflicts.append(
            {
                "article_id": row["article_id"],
                "ticker": row["ticker"],
                "dimension": dimension,
                "reason": reason,
            }
        )

    for row in rows:
        bucket = row.get("bucket", "")
        polarity = row.get("polarity")
        relevance = row.get("relevance")
        title = row.get("title", "")

        if polarity is not None:
            polarity = float(polarity)
            if bucket == "positive" and polarity <= 0:
                add(row, "polarity", f"긍정 버킷인데 polarity={polarity:+.1f}")
            elif bucket == "negative" and polarity >= 0:
                add(row, "polarity", f"부정 버킷인데 polarity={polarity:+.1f}")
            elif bucket == "irrelevant" and abs(polarity) >= _SIGN_CONFLICT:
                add(row, "polarity", f"무관 버킷인데 polarity={polarity:+.1f}")

            # Ricky's own standing rule (MANUAL-TASKS §4): a brokerage named as
            # the author of a view about someone else scores 0 on that row.
            if _is_author_mention(row) and abs(polarity) >= 0.2:
                add(
                    row,
                    "polarity",
                    f"증권사가 남의 회사 견해의 작성자인데 polarity={polarity:+.1f} "
                    "— 이 행의 종목은 증권사입니다",
                )

        if relevance is not None:
            relevance = float(relevance)
            price_only = any(word in title for word in _PRICE_MOVE) and any(
                word in title for word in _MARKET_CAUSE
            )
            if bucket == "irrelevant" and relevance >= _STRUCTURE_RELEVANCE:
                add(row, "relevance", f"무관 버킷인데 relevance={relevance:.1f}")
            elif relevance >= _STRUCTURE_RELEVANCE and any(
                word in title for word in _MARKET_STRUCTURE
            ):
                add(
                    row,
                    "relevance",
                    f"수급·매매동향 기사인데 relevance={relevance:.1f} — 손익 기준이면 0.0~0.3",
                )
            elif relevance >= _STRUCTURE_RELEVANCE and price_only:
                add(
                    row,
                    "relevance",
                    f"주가 움직임 자체가 기사인데 relevance={relevance:.1f} "
                    "— 원인이 수급이면 손익 줄은 움직이지 않습니다",
                )

        intensity = row.get("intensity")
        if (
            intensity is not None
            and relevance is not None
            and float(relevance) <= _DETACHED_RELEVANCE
            and float(intensity) >= _DETACHED_INTENSITY
        ):
            add(
                row,
                "intensity",
                f"relevance={float(relevance):.1f}인데 intensity={float(intensity):.1f} "
                "— 손익에 닿지 않는다면서 손익 충격은 크다는 뜻이 됩니다",
            )

    names = [name for name, _, _, _, _ in DIMENSIONS]
    for index, row in enumerate(rows):
        for other in rows[index + 1 :]:
            if row["ticker"] != other["ticker"] or row["article_id"] == other["article_id"]:
                continue
            mine, theirs = (
                _title_tokens(row.get("title", "")),
                _title_tokens(other.get("title", "")),
            )
            if not mine or not theirs:
                continue
            if len(mine & theirs) / min(len(mine), len(theirs)) < _DIVERGENCE_SIMILARITY:
                continue
            for name in names:
                if name not in row or name not in other:
                    continue
                gap = abs(float(row[name]) - float(other[name]))
                if gap < _DIVERGENCE_GAP:
                    continue
                for side, against in ((row, other), (other, row)):
                    add(
                        side,
                        name,
                        f"같은 사건으로 보이는데 {name}가 {float(side[name]):.1f} vs "
                        f"{float(against[name]):.1f} 입니다: '{against.get('title', '')[:34]}'",
                    )

    return conflicts


def scored_rows(progress: Path, picked: Sequence[dict]) -> list[dict]:
    """Merge whatever has been scored so far back onto the articles it came from.

    ``collate_scores`` drops examples missing a dimension, which is right for
    the finished set and wrong here — a conflict between two scores is worth
    seeing before the other three arrive.
    """
    known = {key_of(row): row for row in picked}
    merged: dict[tuple[str, str], dict] = {}
    for record in read_jsonl(progress):
        key = (record["article_id"], record["ticker"])
        entry = merged.setdefault(key, dict(known.get(key, {})))
        entry.setdefault("article_id", record["article_id"])
        entry.setdefault("ticker", record["ticker"])
        entry["bucket"] = record.get("bucket", entry.get("bucket", ""))
        entry[record["dimension"]] = record["value"]
    return list(merged.values())


# --- display ---------------------------------------------------------------


def format_article(row: dict, *, index: int, total: int) -> str:
    published = str(row.get("published_at", ""))[:16].replace("T", " ")
    description = (row.get("description") or "").strip()
    if len(description) > 400:
        description = description[:400] + " …"
    lines = [
        "",
        "─" * 72,
        f"[{index}/{total}]  {row['ticker']} {row.get('name', '')}"
        f"   ·  {row.get('outlet', '')}  {published}",
        "",
        f"  {row.get('title', '')}",
    ]
    if description:
        lines += ["", *(f"  {chunk}" for chunk in _wrap(description, 68))]
    else:
        lines += ["", "  (본문 없음 — 헤드라인만 제공하는 매체)"]
    lines += ["", f"  {row.get('link', '')}", "─" * 72]
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


# --- parsing input ---------------------------------------------------------


class InputError(ValueError):
    """A malformed score line, reported back to the prompt rather than raised."""


def parse_score(text: str, name: str, low: float, high: float) -> float:
    """Parse one number for one dimension.

    Scoring runs a dimension at a time across the whole set rather than five
    dimensions per article. The earlier version took all five on one line, on
    the reasoning that the judgement is a single act — but the five are not one
    judgement, they are five scales, and interleaving them means the scale for
    ``intensity`` is rebuilt from memory on every article. Holding one scale
    over a hundred articles is what makes the numbers comparable, which is the
    only property the bake-off actually consumes.

    The cost is real and worth naming: five passes over the set instead of one,
    so every article gets read five times.
    """
    raw = text.replace(",", " ").split()
    if len(raw) != 1:
        raise InputError(f"숫자 1개가 필요합니다 (입력 {len(raw)}개)")
    try:
        value = float(raw[0])
    except ValueError as exc:
        raise InputError(f"{name}: {raw[0]!r}은 숫자가 아닙니다") from exc
    if not low <= value <= high:
        raise InputError(f"{name}은 {low}~{high} 범위여야 합니다 (입력 {value})")
    return value


def collate_scores(scored: Sequence[dict]) -> list[dict]:
    """Pivot per-dimension progress records into one row per (article, ticker).

    ``scores.jsonl`` is append-only and holds one record per dimension, which is
    what makes a five-pass run resumable at any point. The finished golden set
    keeps the original one-row-per-example shape, so `verify` and the bake-off
    are unaffected by how the numbers were gathered.

    Rows missing any dimension are omitted rather than written with holes: a
    partially scored example would otherwise reach the bake-off and be measured
    on dimensions nobody supplied.
    """
    by_example: dict[tuple[str, str], dict] = {}
    for record in scored:
        key = (record["article_id"], record["ticker"])
        entry = by_example.setdefault(
            key,
            {
                "article_id": record["article_id"],
                "ticker": record["ticker"],
                "bucket": record.get("bucket", ""),
            },
        )
        entry[record["dimension"]] = record["value"]
        entry["labeled_at"] = max(entry.get("labeled_at", ""), record.get("labeled_at", ""))

    names = [name for name, _, _, _, _ in DIMENSIONS]
    return [row for row in by_example.values() if all(name in row for name in names)]


# --- the interactive passes ------------------------------------------------


def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        return "q"


def run_triage() -> int:
    candidates = read_jsonl(CANDIDATES)
    if not candidates:
        print("후보가 없습니다. 먼저 `sample`을 실행하세요.")
        return 1

    triaged = read_jsonl(TRIAGE)
    done = {key_of(row) for row in triaged}
    remaining = [row for row in candidates if key_of(row) not in done]

    # Counted through select_for_labelling rather than by tallying buckets, so
    # the progress line and the stop condition both mean "how many usable
    # examples exist" rather than "how many keystrokes were made". They differ:
    # the first pass ended with 25 부정 of which 2 were the same 한화에어로 story,
    # so the raw tally said full while the bucket held 23. Counting raw would
    # have shown "다 찼습니다" and sent Ricky to `label` with a short bucket.
    _, counts = select_for_labelling(triaged)

    print(f"\n1단계 — 4지선다 분류. 남은 후보 {len(remaining)}건 (완료 {len(done)}건)")
    print("각 기사를 읽고 어느 쪽인지만 고르세요. 숫자는 2단계에서 매깁니다.\n")
    for key, (_, label, _) in BUCKETS.items():
        print(f"  {key} = {label}")
    print("  s = 건너뛰기    q = 저장하고 종료\n")

    for index, row in enumerate(remaining, start=1):
        if all(counts.get(name, 0) >= PER_BUCKET for name, _, _ in BUCKETS.values()):
            print("\n네 버킷이 모두 찼습니다. `label`로 넘어가세요.")
            break

        print(format_article(row, index=index, total=len(remaining)))
        # Counts can exceed PER_BUCKET now that a full bucket still accepts the
        # honest answer, and "26/25" reads like a bug. Show the target as met
        # instead, which is also the more useful thing to know at a glance.
        status = "  ".join(
            f"{short} {min(counts.get(name, 0), PER_BUCKET)}/{PER_BUCKET}"
            + ("✓" if counts.get(name, 0) >= PER_BUCKET else " ")
            for name, _, short in BUCKETS.values()
        )
        print(f"  진행: {status}")

        while True:
            answer = _prompt("  분류 [1/2/3/4/s/q] > ").lower()
            if answer == "q":
                print(f"\n저장 완료. {TRIAGE}")
                return 0
            if answer == "s":
                break
            if answer in BUCKETS:
                name = BUCKETS[answer][0]
                # A full bucket never changes the answer. The earlier version
                # rejected the input and asked for "a different classification",
                # which is the one thing this pass must never do: it would put a
                # label on an article because a counter was full rather than
                # because the article said so, and a golden set built that way
                # measures the quota instead of the judgement. The cap belongs at
                # selection time, and `select_for_labelling` already applies it.
                append_jsonl(TRIAGE, {**row, "bucket": name})
                triaged.append({**row, "bucket": name})
                # Re-derived rather than incremented: this answer may be the
                # same story as one already chosen, in which case it adds a
                # keystroke but not a usable example, and the counter has to say
                # so or the pass stops two examples short.
                before = counts.get(name, 0)
                _, counts = select_for_labelling(triaged)
                if counts.get(name, 0) == before:
                    reason = (
                        f"'{BUCKETS[answer][1]}' {PER_BUCKET}건이 이미 찼습니다"
                        if before >= PER_BUCKET
                        else "이미 뽑힌 기사와 같은 사건입니다"
                    )
                    print(f"  기록했습니다. 다만 {reason} — 채점 대상에는 안 들어갑니다.")
                break
            print("  1, 2, 3, 4, s, q 중 하나를 입력하세요.")

    print(f"\n분류 완료 {sum(counts.values())}건 → {TRIAGE}")
    return 0


def run_label(
    *,
    target: Path = LABELS,
    source: Sequence[dict] | None = None,
    progress: Path | None = None,
    redo: str | None = None,
) -> int:
    triaged = read_jsonl(TRIAGE)
    if not triaged:
        print("분류 결과가 없습니다. 먼저 `triage`를 실행하세요.")
        return 1

    picked = list(source) if source is not None else select_for_labelling(triaged)[0]
    progress = progress or target.with_name(target.stem + "-scores.jsonl")
    scored = read_jsonl(progress)
    done = {(r["article_id"], r["ticker"], r["dimension"]) for r in scored}

    # `--redo` re-asks only what a rule flags, never a whole dimension. Re-asking
    # all 100 after a definition change would mean re-deriving the scale from
    # memory, which is the thing scoring one dimension at a time exists to avoid.
    reasons: dict[tuple[str, str, str], str] = {}
    if redo:
        for conflict in score_conflicts(scored_rows(progress, picked)):
            if conflict["dimension"] != redo:
                continue
            key = (conflict["article_id"], conflict["ticker"], conflict["dimension"])
            done.discard(key)
            reasons[key] = conflict["reason"]
        if not reasons:
            print(f"\n{redo} 차원에서 규칙에 걸린 항목이 없습니다.")
            return 0
        print(f"\n재채점 — {redo} 차원에서 규칙에 걸린 {len(reasons)}건")

    print(f"\n2단계 — 채점. 대상 {len(picked)}건 × 차원 {len(DIMENSIONS)}개")
    print("  한 차원씩 전부 훑습니다. 한 척도를 끝까지 유지해야 숫자가 비교 가능해집니다.")
    print("  s = 건너뛰기    q = 저장하고 종료")
    print("  ⚠ 그 뒤 주가가 어떻게 됐는지 보지 말고 매길 것 (MANUAL-TASKS §4)")

    for name, low, high, hint, anchors in DIMENSIONS:
        if redo and name != redo:
            continue
        remaining = [row for row in picked if (row["article_id"], row["ticker"], name) not in done]
        if not remaining:
            continue

        print(f"\n{'━' * 68}")
        print(f"  차원 {name}  [{low:g} ~ {high:g}]   {hint}")
        print(f"  남은 {len(remaining)}건 / {len(picked)}건")
        for line in anchors:
            print(f"    {line}")
        print("━" * 68)

        for index, row in enumerate(remaining, start=1):
            print(format_article(row, index=index, total=len(remaining)))
            print(f"  분류: {row.get('bucket', '?')}")
            reason = reasons.get((row["article_id"], row["ticker"], name))
            if reason:
                print(f"  ⚑ {reason}")

            while True:
                answer = _prompt(f"  {name} [{low:g}~{high:g}] > ")
                if answer.lower() == "q":
                    _finalise_labels(progress, target)
                    print(f"\n저장 완료. 진행 {progress}")
                    return 0
                if answer.lower() == "s":
                    break
                try:
                    value = parse_score(answer, name, low, high)
                except InputError as exc:
                    print(f"  {exc}")
                    continue
                append_jsonl(
                    progress,
                    {
                        "article_id": row["article_id"],
                        "ticker": row["ticker"],
                        "bucket": row.get("bucket", ""),
                        "dimension": name,
                        "value": value,
                        "labeled_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    },
                )
                break

    complete = _finalise_labels(progress, target)
    print(f"\n채점 완료 {complete}건 → {target}")
    return 0


def _finalise_labels(progress: Path, target: Path) -> int:
    """Rewrite ``target`` from whatever is complete in ``progress``.

    Rewritten rather than appended because a partially scored example becomes
    complete later, and appending would leave the earlier incomplete copy in
    place. ``progress`` stays append-only, so nothing Ricky typed is ever
    rewritten — only the derived file is.
    """
    complete = collate_scores(read_jsonl(progress))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for row in complete:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(complete)


def run_review() -> int:
    """Walk the rule-flagged labels. Ricky keeps or changes each; nothing else does.

    Every outcome is written to ``review.jsonl``, kept even when nothing
    changes. That record is the point as much as the corrections are: it makes
    the flagging's influence on the finished set a number rather than a
    reassurance. `verify` reports it, and if the bake-off ever ranks models
    differently on the flagged subset than on the rest, the record is what makes
    that checkable after the fact.
    """
    triaged = read_jsonl(TRIAGE)
    if not triaged:
        print("분류 결과가 없습니다. 먼저 `triage`를 실행하세요.")
        return 1

    current = {key_of(row): row for row in latest_triage(triaged)}
    flags = find_flags(list(current.values()))
    decided = {(r["article_id"], r["ticker"], r["flag"]) for r in read_jsonl(REVIEW)}
    remaining = [f for f in flags if (f["article_id"], f["ticker"], f["flag"]) not in decided]

    if not remaining:
        print(f"\n검토할 flag가 없습니다. (전체 {len(flags)}건 모두 판정 완료)")
        return 0

    print(f"\n검토 — 규칙이 걸러낸 {len(remaining)}건 (전체 flag {len(flags)}건)")
    print("  전부 '두 판단이 서로 안 맞는다'는 지적이지, 정답 제시가 아닙니다.")
    print("  그대로가 맞다고 판단되면 Enter를 누르면 됩니다. 그것도 기록됩니다.\n")
    for key, (_, label, _) in BUCKETS.items():
        print(f"  {key} = {label}")
    print("  Enter = 그대로 유지    s = 나중에    q = 저장하고 종료\n")

    for index, flag in enumerate(remaining, start=1):
        row = current.get((flag["article_id"], flag["ticker"]))
        if row is None:
            continue

        print(format_article(row, index=index, total=len(remaining)))
        print(f"  현재 분류: {row['bucket']}")
        print(f"  ⚑ {flag['flag']} — {flag['reason']}")

        while True:
            answer = _prompt("  유지=Enter / 변경=[1/2/3/4] / s / q > ").lower()
            if answer == "q":
                print(f"\n저장 완료. {REVIEW}")
                return 0
            if answer == "s":
                break
            if answer == "" or answer in BUCKETS:
                after = BUCKETS[answer][0] if answer else row["bucket"]
                if after != row["bucket"]:
                    append_jsonl(TRIAGE, {**row, "bucket": after})
                    current[key_of(row)] = {**row, "bucket": after}
                append_jsonl(
                    REVIEW,
                    {
                        "article_id": row["article_id"],
                        "ticker": row["ticker"],
                        "flag": flag["flag"],
                        "before": row["bucket"],
                        "after": after,
                        "changed": after != row["bucket"],
                        "decided_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    },
                )
                moved = f" → {after}" if after != row["bucket"] else " 유지"
                print(f"  → {row['bucket']}{moved}")
                break
            print("  Enter, 1, 2, 3, 4, s, q 중 하나를 입력하세요.")

    changed = sum(1 for r in read_jsonl(REVIEW) if r.get("changed"))
    print(f"\n검토 완료. 판정 {len(read_jsonl(REVIEW))}건 중 변경 {changed}건 → {REVIEW}")
    return 0


def run_recheck(*, size: int = RECHECK_SIZE, seed: int = 20260807) -> int:
    """Re-label a subset a day later, without showing the first answers.

    SPEC §7.3: if Ricky's two passes disagree more than the models disagree with
    each other, the schema is underdefined and the bake-off would be measuring
    noise. The first answers are deliberately not displayed — seeing them turns
    the check into a memory test.
    """
    labelled = read_jsonl(LABELS)
    if len(labelled) < size:
        print(f"채점이 {len(labelled)}건뿐입니다. {size}건 이상 끝난 뒤 실행하세요.")
        return 1

    first_day = min(str(row.get("labeled_at", ""))[:10] for row in labelled)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    if first_day == today:
        print("⚠ 오늘 매긴 라벨입니다. 하루 지난 뒤에 실행하세요 (기억이 아니라 기준을 재는 검사).")
        return 1

    triaged = {key_of(row): row for row in read_jsonl(TRIAGE)}
    rng = random.Random(seed)
    chosen = rng.sample(sorted(labelled, key=key_of), size)
    source = [triaged[key_of(row)] for row in chosen if key_of(row) in triaged]

    print(f"\n재라벨링 검사 — {len(source)}건. 첫 회차 답은 보여주지 않습니다.")
    return run_label(target=RECHECK, source=source)


# --- verification ----------------------------------------------------------


# Share of the finished set that may be reworked after a flag before the
# flagging stops being a nudge and starts being an author. No measurement backs
# this number — it is a declared limit, set before any flag was reviewed so that
# it cannot be widened to fit whatever the review turned out to do.
MAX_REVIEW_INFLUENCE = 0.15


def review_influence(labelled: Sequence[dict], reviewed: Sequence[dict]) -> list[str]:
    """How much of the finished set changed because a rule flagged it.

    The flags in :func:`find_flags` are rules rather than opinions, which is what
    keeps them out of the labels. It is still worth counting: rules were written
    by reading this corpus, and a rule that fires on a fifth of the set and
    changes every label it touches has authored a fifth of the answers however
    mechanical each step looked.

    Reported as a number rather than trusted as an argument. If the bake-off
    ever ranks models differently on the flagged subset than on the rest,
    ``review.jsonl`` is what makes that checkable after the fact.
    """
    if not labelled:
        return []
    changed = {(row["article_id"], row["ticker"]) for row in reviewed if row.get("changed")} & {
        key_of(row) for row in labelled
    }
    share = len(changed) / len(labelled)
    if share > MAX_REVIEW_INFLUENCE:
        return [
            f"검토 후 바뀐 항목이 {len(changed)}/{len(labelled)}건 ({share:.0%}) — "
            f"한도 {MAX_REVIEW_INFLUENCE:.0%}를 넘었습니다. 규칙이 라벨을 대신 쓰고 있는지 "
            f"확인이 필요하고, 베이크오프는 flag된 항목을 뺀 부분집합에서도 같은 순위가 "
            f"나오는지 함께 봐야 합니다"
        ]
    return []


def verify() -> tuple[bool, list[str]]:
    """Check the finished set, and report Ricky's agreement with himself."""
    problems: list[str] = []
    labelled = read_jsonl(LABELS)

    if not labelled:
        return False, [f"{LABELS} 가 비어 있습니다"]

    counts: dict[str, int] = {}
    for row in labelled:
        counts[row.get("bucket", "?")] = counts.get(row.get("bucket", "?"), 0) + 1
    for name, label, _ in BUCKETS.values():
        have = counts.get(name, 0)
        if have != PER_BUCKET:
            problems.append(f"{label}: {have}건 (목표 {PER_BUCKET})")

    seen = set()
    for row in labelled:
        key = key_of(row)
        if key in seen:
            problems.append(f"중복: {key}")
        seen.add(key)

    for row in labelled:
        for name, low, high, _, _ in DIMENSIONS:
            if name not in row:
                problems.append(f"{key_of(row)}: {name} 없음")
            elif not low <= float(row[name]) <= high:
                problems.append(f"{key_of(row)}: {name}={row[name]} 범위 밖")

    # A golden set whose polarity never leaves the middle cannot separate models.
    strong = sum(1 for row in labelled if abs(float(row.get("polarity", 0))) >= 0.5)
    if labelled and strong < len(labelled) * 0.25:
        problems.append(f"|polarity| ≥ 0.5 인 항목이 {strong}건뿐 — 쉬운 사례만 모였을 수 있습니다")

    problems.extend(review_influence(labelled, read_jsonl(REVIEW)))

    # Reported, never failed. See the note above `score_conflicts`: a conflict
    # says two of Ricky's decisions disagree, and choosing which one gives way
    # is exactly the judgement no automated check may make here.
    context = {key_of(row): row for row in select_for_labelling(read_jsonl(TRIAGE))[0]}
    conflicts = score_conflicts([{**context.get(key_of(row), {}), **row} for row in labelled])
    if conflicts:
        print(f"\n⚑ 규칙에 걸린 점수 {len(conflicts)}건 — 실패가 아니라 확인 요청입니다.")
        for conflict in conflicts:
            article = context.get((conflict["article_id"], conflict["ticker"]), {})
            print(f"  [{conflict['dimension']}] {article.get('name', '?')} — {conflict['reason']}")
            print(f"      {article.get('title', '')[:60]}")
        first = conflicts[0]["dimension"]
        print(f"  고치려면: uv run python -m scripts.golden label --redo {first}\n")

    recheck = read_jsonl(RECHECK)
    if recheck:
        first = {key_of(row): row for row in labelled}
        gaps = []
        for row in recheck:
            other = first.get(key_of(row))
            if not other:
                continue
            gaps.append(
                max(abs(float(row[name]) - float(other[name])) for name, _, _, _, _ in DIMENSIONS)
            )
        if gaps:
            worst = max(gaps)
            mean = sum(gaps) / len(gaps)
            print(f"재라벨링 일치도: 평균 차이 {mean:.2f}, 최대 {worst:.2f} ({len(gaps)}건)")
            if mean > 0.25:
                problems.append(
                    f"두 회차 평균 차이 {mean:.2f} — 스키마가 덜 정의됐을 수 있습니다 (SPEC §7.3)"
                )

    return not problems, problems


# --- entry point -----------------------------------------------------------


def run_sample(size: int) -> int:
    print("기사를 읽는 중…")
    articles = load_articles()
    pairs = matched_pairs(articles)
    print(f"  고유 기사 {len(articles):,}건 → 매칭된 (기사,종목) 쌍 {len(pairs):,}개")
    if not pairs:
        print("매칭된 기사가 없습니다. 수집기를 더 돌리세요.")
        return 1

    chosen = stratified_sample(pairs, articles, size=size)
    written = write_candidates(chosen, articles)

    spread: dict[str, int] = {}
    for _, ticker in chosen:
        spread[ticker] = spread.get(ticker, 0) + 1
    top = sorted(spread.items(), key=lambda kv: -kv[1])[:5]
    print(f"  후보 {written}건 → {CANDIDATES}")
    print(f"  종목 {len(spread)}개에 분산. 최다: {', '.join(f'{t} {n}건' for t, n in top)}")
    print("\n다음: uv run python -m scripts.golden triage")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sampler = sub.add_parser("sample", help="후보를 추출한다")
    sampler.add_argument("--size", type=int, default=POOL_SIZE)
    sub.add_parser("triage", help="1단계 — 4지선다 분류")
    sub.add_parser("review", help="규칙이 걸러낸 분류를 다시 본다")
    labeller = sub.add_parser("label", help="2단계 — 차원별 채점")
    labeller.add_argument(
        "--redo",
        choices=[name for name, _, _, _, _ in DIMENSIONS],
        help="그 차원에서 규칙에 걸린 항목만 다시 매긴다",
    )
    sub.add_parser("recheck", help="하루 뒤 재라벨링 검사")
    sub.add_parser("verify", help="완성된 골든셋을 검사한다")

    args = parser.parse_args(argv)

    if args.command == "sample":
        return run_sample(args.size)
    if args.command == "triage":
        return run_triage()
    if args.command == "review":
        return run_review()
    if args.command == "label":
        return run_label(redo=args.redo)
    if args.command == "recheck":
        return run_recheck()

    ok, problems = verify()
    if ok:
        print(f"✅ 골든셋 정상 — {len(read_jsonl(LABELS))}건")
        return 0
    print("❌ 확인이 필요합니다:")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
