"""Korean news from outlet RSS. SPEC §3.1.

Replaces the Naver search API, which closed to new registrations in 2026. RSS is
a subscription rather than a query: each outlet publishes its most recent 50–120
articles and this collector takes all of them. Filtering to tickers happens
later, at SPEC §6.1 Stage 0, so a corrected ``aliases.yaml`` can be re-applied to
articles already stored.

Four properties of this source shape the code, and three of them have no
equivalent in the price collectors.

**Nothing here can be re-fetched.** A feed holds a rolling buffer with no
history. An hour not collected is gone permanently — unlike pykrx, which will
serve 2024 prices again in 2030. This single fact drives hourly collection, the
un-ignoring of ``data/raw/kr/news/`` in ``.gitignore``, and why
:func:`check_collection_gap` treats a long silence as a validation failure
rather than as an idle period.

**Consecutive polls overlap almost entirely.** A 50-item feed publishing ~60
articles a day, polled hourly, re-delivers roughly 48 items already stored.
Deduplication by :func:`article_id` is therefore load-bearing, not an
optimisation: without it, article counts would inflate ~25x and
``news_volume_z`` would measure polling frequency instead of news.

**Body text ranges from complete to absent.** 뉴시스 supplies ~1,155 characters,
한국경제 and 조선비즈 supply none. An empty ``description`` is valid data and the
missing-value threshold says so explicitly.

**A feed can answer 200 and mean nothing.** 매일경제 emits ``+09:00`` where RFC
822 wants ``+0900``, which ``email.utils`` rejects. An earlier draft dropped
unparseable items silently and thereby discarded all three 매일경제 feeds without
failing a single check. :func:`parse_feed` now raises when a feed yields items
but none survive parsing — the same lesson pykrx's empty DataFrame taught.

**There is no value to pin for check four.** The fourth check CLAUDE.md requires
— compare a hardcoded known value — presumes a source that can be asked about
the past. This one cannot. :func:`check_structural_invariants` substitutes for
it by catching the same failure class, well-formed but wrong: a timestamp at the
epoch or in the future, or a link pointing somewhere other than the outlet the
feed claims to be.
"""

from __future__ import annotations

import datetime as dt
import email.utils as email_utils
import gzip
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests

from src.collectors.validate import (
    CheckResult,
    ValidationReport,
    check_missing_ratio,
    check_schema,
    validate,
)
from src.util.config import NewsFeed, load_news_feeds
from src.util.session import next_tradeable_open, now_utc, to_utc

COLLECTOR = "kr_news"

SCHEMA = {
    "article_id": "object",  # dedup key; stable across re-parses of the same item
    "feed": "object",
    "outlet": "object",
    "title": "object",
    "link": "object",
    "description": "object",
    "published_at": "datetime64[ns, UTC]",
    "collected_at_utc": "datetime64[ns, UTC]",
    # CLAUDE.md: news published during a session is tradeable at the next
    # session's open. Features join on this, never on published_at.
    "known_at_utc": "datetime64[ns, UTC]",
}

# `description` is legitimately empty for headline-only outlets, so it carries no
# threshold at all. The other three are what makes an article usable.
MISSING_THRESHOLDS = {"title": 0.0, "link": 0.0, "published_at": 0.0}

# The fastest feed measured turned its buffer over in well under two hours.
# Collection runs hourly; two consecutive misses is the point at which articles
# have provably been lost rather than merely delayed.
MAX_COLLECTION_GAP = dt.timedelta(hours=2)

# A published_at outside this window means the date parse produced garbage.
# Feeds occasionally carry a corrected or scheduled item, so the past bound is
# generous; the future bound is not, since nothing can be published later than
# now plus clock skew.
_OLDEST_PLAUSIBLE = dt.timedelta(days=30)
_FUTURE_TOLERANCE = dt.timedelta(hours=6)

_TIMEOUT = 20

# A dropped feed costs articles that cannot be re-fetched, so a couple of
# seconds of retry is cheap insurance. Live evidence: 머니투데이 timed out from a
# GitHub Actions runner while answering fine locally.
_ATTEMPTS = 3


class FeedError(RuntimeError):
    """A feed answered, but not with parseable RSS."""


def article_id(guid: str | None, link: str) -> str:
    """Stable identity for one article.

    ``guid`` is preferred because outlets sometimes rewrite a URL after
    publication — adding tracking parameters, switching to https — and a link
    hash would then read as a new article on the next poll. Falls back to the
    link when a feed omits guid, which several do.
    """
    basis = (guid or link).strip()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


# --- validation ----------------------------------------------------------


def check_collection_gap(
    df: pd.DataFrame, previous_run: pd.Timestamp | None, *, now: pd.Timestamp | None = None
) -> CheckResult:
    """Check three, as collection continuity rather than trading-day continuity.

    News does not follow a market calendar — outlets publish through weekends
    and holidays — so the strict check in :mod:`src.collectors.validate` does
    not apply. What matters instead is whether *this collector* ran often enough
    to see everything the feeds held, because anything missed is unrecoverable.
    """
    now = now or now_utc()

    if previous_run is None:
        return CheckResult(
            "collection_gap", True, "first run; no previous collection to compare against"
        )

    gap = now - previous_run
    if gap > MAX_COLLECTION_GAP:
        return CheckResult(
            "collection_gap",
            False,
            f"{gap.total_seconds() / 3600:.1f}h since the previous run, limit is "
            f"{MAX_COLLECTION_GAP.total_seconds() / 3600:.0f}h; feeds roll over faster than "
            f"this and the articles in between cannot be re-fetched",
        )
    return CheckResult(
        "collection_gap", True, f"{gap.total_seconds() / 3600:.1f}h since the previous run"
    )


def check_structural_invariants(
    df: pd.DataFrame, feeds: Sequence[NewsFeed], *, now: pd.Timestamp | None = None
) -> CheckResult:
    """Stands in for check four, which this source cannot support.

    CLAUDE.md asks for a hardcoded known value. RSS has no history to pin one
    against, so instead these assert the properties a wrong parse would break
    while a schema check still passed.
    """
    if df.empty:
        return CheckResult("structural_invariants", True, "no rows to check")

    now = now or now_utc()
    domains = {f.name: f.domain for f in feeds}
    problems: list[str] = []

    stale = df[df["published_at"] < now - _OLDEST_PLAUSIBLE]
    if len(stale):
        oldest = stale["published_at"].min()
        problems.append(f"{len(stale)} rows older than {_OLDEST_PLAUSIBLE.days}d (oldest {oldest})")

    future = df[df["published_at"] > now + _FUTURE_TOLERANCE]
    if len(future):
        problems.append(
            f"{len(future)} rows published in the future (latest {future['published_at'].max()})"
        )

    mismatched = 0
    for row in df.itertuples():
        expected = domains.get(row.feed)
        if expected and expected not in (urlparse(row.link).hostname or ""):
            mismatched += 1
    if mismatched:
        problems.append(f"{mismatched} links point outside their feed's declared domain")

    if df["article_id"].duplicated().any():
        problems.append(f"{int(df['article_id'].duplicated().sum())} duplicated article_id")

    if problems:
        return CheckResult("structural_invariants", False, "; ".join(problems))
    return CheckResult("structural_invariants", True, f"{len(df)} rows well-formed")


def validate_frame(
    df: pd.DataFrame,
    feeds: Sequence[NewsFeed],
    *,
    previous_run: pd.Timestamp | None = None,
    now: pd.Timestamp | None = None,
) -> ValidationReport:
    """Run the four checks, one of them substituted. See the module docstring."""
    return validate(
        COLLECTOR,
        [
            check_schema(df, SCHEMA),
            check_missing_ratio(df, MISSING_THRESHOLDS)
            if len(df)
            else CheckResult("missing_ratio", True, "no rows"),
            check_collection_gap(df, previous_run, now=now),
            check_structural_invariants(df, feeds, now=now),
        ],
    )


# --- parsing -------------------------------------------------------------


def _text(item: ET.Element, tag: str) -> str:
    value = item.findtext(tag)
    return value.strip() if value else ""


# RFC 822 wants "+0900"; 매일경제 emits "+09:00". email.utils rejects the latter,
# which silently discarded all three 매일경제 feeds until a fixture caught it.
_ISO_OFFSET = re.compile(r"([+-]\d{2}):(\d{2})\s*$")


def parse_pubdate(raw: str, assume_tz: str | None = None) -> pd.Timestamp | None:
    """Parse an RSS ``pubDate``, tolerating the formats seen in the wild.

    Returns ``None`` when the value cannot be read as an instant. Callers drop
    those items rather than defaulting them: an article carrying a guessed
    timestamp would violate the look-ahead rule silently, which is worse than an
    article that is simply absent.

    ``assume_tz`` applies only to a timestamp carrying no zone at all — 인포스탁
    emits ``2026-08-03 17:11:18``. The zone is never inferred here; it comes from
    ``timezone:`` on the feed's config entry, so the assumption is written down
    where someone can disagree with it.
    """
    if not raw:
        return None

    for candidate in (raw, _ISO_OFFSET.sub(r"\1\2", raw)):
        try:
            parsed = email_utils.parsedate_to_datetime(candidate)
        except (TypeError, ValueError):
            continue
        if parsed is not None and parsed.tzinfo is not None:
            return to_utc(pd.Timestamp(parsed))

    if assume_tz:
        try:
            naive = pd.Timestamp(raw)
        except ValueError:
            return None
        if naive.tzinfo is None:
            return to_utc(naive.tz_localize(assume_tz))
    return None


def parse_feed(xml: bytes, feed: NewsFeed, collected_at: pd.Timestamp) -> pd.DataFrame:
    """Turn one feed's RSS body into the committed schema.

    Items without a parseable ``pubDate`` are dropped rather than defaulted:
    an article with a guessed timestamp would silently violate the look-ahead
    rule, which is worse than an article that is simply absent.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise FeedError(f"{feed.name}: malformed XML ({exc})") from exc

    rows: list[dict] = []
    for item in root.findall(".//item"):
        link = _text(item, "link")
        if not link:
            continue

        published = parse_pubdate(_text(item, "pubDate"), feed.timezone)
        if published is None:
            continue

        rows.append(
            {
                "article_id": article_id(_text(item, "guid") or None, link),
                "feed": feed.name,
                "outlet": feed.outlet,
                "title": _text(item, "title"),
                "link": link,
                "description": _text(item, "description"),
                "published_at": published,
            }
        )

    if not rows:
        # A feed that answered 200 and yielded nothing is a defect, not a quiet
        # news day. This is how the 매일경제 offset bug hid: parse_feed returned
        # an empty frame, fetch skipped it, and three outlets vanished from the
        # pipeline without a single failed check.
        raise FeedError(
            f"{feed.name}: {len(root.findall('.//item'))} items in the feed, none parseable"
        )

    df = pd.DataFrame(rows)
    df["collected_at_utc"] = collected_at
    df["known_at_utc"] = [next_tradeable_open("KR", ts) for ts in df["published_at"]]

    # pandas infers second resolution from a scalar Timestamp assignment; the
    # schema declares nanoseconds, and a mismatch would fail check_schema.
    for column, dtype in SCHEMA.items():
        df[column] = df[column].astype(dtype)
    return df[list(SCHEMA)]


# --- storage -------------------------------------------------------------


def run_path(root: Path, at: pd.Timestamp) -> Path:
    """Where one run's articles land.

    One file per run inside a per-day directory. Nothing is ever appended to or
    rewritten, which satisfies CLAUDE.md's immutability rule without the
    collector needing to know about it.
    """
    at = to_utc(at)
    return root / "kr" / "news" / at.strftime("%Y-%m-%d") / f"{at.strftime('%H%M')}.jsonl.gz"


def seen_ids(root: Path, day: dt.date) -> set[str]:
    """Article ids already stored for ``day``."""
    directory = root / "kr" / "news" / day.isoformat()
    if not directory.exists():
        return set()

    ids: set[str] = set()
    for path in sorted(directory.glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    ids.add(json.loads(line)["article_id"])
    return ids


def last_run_at(root: Path, day: dt.date) -> pd.Timestamp | None:
    """Timestamp of the most recent run stored for ``day``, if any."""
    directory = root / "kr" / "news" / day.isoformat()
    if not directory.exists():
        return None
    stamps = sorted(p.stem.removesuffix(".jsonl") for p in directory.glob("*.jsonl.gz"))
    if not stamps:
        return None
    return to_utc(pd.Timestamp(f"{day.isoformat()} {stamps[-1][:2]}:{stamps[-1][2:]}", tz="UTC"))


def write_run(df: pd.DataFrame, root: Path, at: pd.Timestamp) -> Path:
    """Write one run's new articles, never overwriting an existing file."""
    path = run_path(root, at)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        # CLAUDE.md rule 1: re-runs write to a new suffixed path.
        suffix = 2
        while path.with_name(f"{path.name.removesuffix('.jsonl.gz')}-v{suffix}.jsonl.gz").exists():
            suffix += 1
        path = path.with_name(f"{path.name.removesuffix('.jsonl.gz')}-v{suffix}.jsonl.gz")

    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in df.to_dict("records"):
            for key in ("published_at", "collected_at_utc", "known_at_utc"):
                row[key] = row[key].isoformat()
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# --- fetching ------------------------------------------------------------


def _fetch_one(
    feed: NewsFeed,
    collected_at: pd.Timestamp,
    *,
    timeout: int,
    attempts: int,
) -> tuple[pd.DataFrame | None, str | None]:
    """Poll one feed, retrying transient network failures.

    Retrying is worth the seconds here in a way it would not be for prices. A
    dropped connection costs articles that cannot be re-fetched at the next run,
    or ever. Observed live: 머니투데이 — one of the better sources at ~516
    characters of body text — timed out from a GitHub Actions runner while
    answering fine from a local connection.

    A parse failure is not retried. Malformed XML will be malformed again.
    """
    last: str | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(2**attempt)
        try:
            response = requests.get(
                feed.url, headers={"User-Agent": "market-briefing"}, timeout=timeout
            )
        except requests.RequestException as exc:
            last = f"{feed.name}: {type(exc).__name__}"
            continue

        if response.status_code != 200:
            last = f"{feed.name}: HTTP {response.status_code}"
            continue

        try:
            return parse_feed(response.content, feed, collected_at), None
        except FeedError as exc:
            return None, f"{feed.name}: {exc}"

    return None, f"{last} after {attempts} attempts"


def fetch(
    feeds: Iterable[NewsFeed],
    *,
    root: Path | None = None,
    now: pd.Timestamp | None = None,
    timeout: int = _TIMEOUT,
    attempts: int = _ATTEMPTS,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Poll every feed once and return the articles not already stored.

    A feed that fails is recorded and the others still collect, per CLAUDE.md's
    failure handling — one outlet's outage must not cost an hour of everything
    else, since that hour cannot be recovered.
    """
    feeds = list(feeds)
    collected_at = to_utc(now or now_utc())
    root = root or Path("data/raw")

    frames: list[pd.DataFrame] = []
    failures: list[str] = []

    for feed in feeds:
        parsed, failure = _fetch_one(feed, collected_at, timeout=timeout, attempts=attempts)
        if failure:
            failures.append(failure)
        elif parsed is not None and not parsed.empty:
            frames.append(parsed)

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(SCHEMA))

    fetched = len(df)
    if not df.empty:
        df = df.drop_duplicates(subset="article_id", keep="first")
        already = seen_ids(root, collected_at.date())
        df = df[~df["article_id"].isin(already)].reset_index(drop=True)

    report = validate_frame(
        df, feeds, previous_run=last_run_at(root, collected_at.date()), now=collected_at
    )
    report.add(
        CheckResult(
            "fetch",
            not failures,
            "; ".join(failures) or f"{len(feeds)} feeds, {fetched} items seen, {len(df)} new",
        )
    )
    return df, report


def main() -> int:
    """Entry point for the hourly collection workflow.

    Prints the validation report and always exits 0 on a partial success: a
    failing feed must not abort the run, because the other fourteen outlets'
    articles cannot be re-fetched later either. A non-zero exit is reserved for
    the case where nothing at all was collected.
    """

    feeds = load_news_feeds()
    now = now_utc()
    df, report = fetch(feeds, now=now)

    print(report.summary())
    for result in report.results:
        print(f"  {result}")

    if df.empty:
        print("no new articles; nothing written")
        return 0 if report.ok else 1

    path = write_run(df, Path("data/raw"), now)
    print(f"wrote {len(df)} articles to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
