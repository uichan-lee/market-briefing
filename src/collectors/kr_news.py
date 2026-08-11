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
serve 2024 prices again in 2030. This single fact drives the collection
schedule, the un-ignoring of ``data/raw/kr/news/`` in ``.gitignore``, and why
:func:`check_collection_gap` treats a long silence as a validation failure
rather than as an idle period.

Because that loss is silent, it gets a check of its own.
:func:`check_feed_continuity` compares where each feed's buffer now starts
against the newest article already stored from it; if the first has passed the
second, the window moved past everything known and the articles between them
were lost. Unlike the gap check it names which feeds and how much, which matters
because the measured buffers range from 4 hours of history to 101.

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
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
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

# An early warning, not the loss detector — :func:`check_feed_continuity` is
# that, and it reads evidence rather than a clock.
#
# Measured 2026-08-03 at 22:30 KST, hours of history held per feed: 한국경제 경제
# 4.0, 전자신문 4.5 and 7.0, 연합뉴스 10.3 and 12.2, 한국경제 증권 12.3, 뉴시스 17.9,
# 매일경제 26.6–58.2, and everything else past 70. So two hours costs nothing at
# that time of day. It was set at two anyway, because the measurement was taken
# during the Korean night: a buffer is a fixed item count, so its span shrinks in
# proportion to how fast the outlet is publishing.
#
# The market-hours measurement that comment was waiting for was taken 2026-08-07
# at 09:20 KST, mid-session, and it settles the question in the unwelcome
# direction. `etnews_economy` holds thirty items and spanned **1.2 hours** —
# below this threshold, not above it. `etnews_main` held 7.9h and `yna_industry`
# 6.5h; everything else stayed past 20h.
#
# So two hours is knowingly too loose for the fastest feed, and passing this
# check does **not** mean nothing was lost. That is not a bug to be fixed by
# lowering the number: GitHub fires roughly a third of the declared runs
# (31 declared, 6–10 observed per day), so a one-hour limit would fail nearly
# every run and say nothing a caller could act on. The threshold stays an early
# warning and `check_feed_continuity` remains the thing that reads evidence.
#
# Confirmed live the same morning, both statements in one run at 06:54Z:
# `collection_gap` passed at 1.5h while `feed_continuity` failed with
# `etnews_economy lost 0.4h`.
MAX_COLLECTION_GAP = dt.timedelta(hours=2)

# How long a feed may go unanswered before :func:`check_feed_continuity` stops
# waiting for evidence and fails.
#
# It is deliberately far above any observed single outage. The purpose is not to
# catch a timeout — the next run that answers measures what that cost — but to
# catch a feed that will never answer again, where no later run can settle the
# question and silence in the exit code would be the wrong report.
#
# Measured 2026-08-11 over the sixty preceding runs: every unanswered-feed event
# was a single run, the longest implied silence was 7.6h (전자신문, which had
# published nothing for 7.5h of it), and every recovering run passed the overlap
# comparison. A day is roughly three times the worst of those and still inside
# the two-day lookback that `_stored_files` gives this check to work with.
MAX_FEED_SILENCE = dt.timedelta(hours=24)

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


def check_feed_continuity(
    buffer_oldest: Mapping[str, pd.Timestamp],
    newest_stored: Mapping[str, pd.Timestamp],
    unfetched: Iterable[str] = (),
    *,
    now: pd.Timestamp | None = None,
) -> CheckResult:
    """Detect articles that were actually lost, rather than inferring it.

    :func:`check_collection_gap` compares a clock against a constant, so it can
    only guess: it fires on a long gap that cost nothing, and stays quiet on a
    short gap that emptied a fast feed. This check reads the evidence instead.

    A feed's buffer is a window over its own output. If the *oldest* item still
    in that window was published *after* the newest item already stored, then the
    window has moved past everything known and whatever fell between the two is
    gone — not delayed, gone, because RSS has no backfill. Anything else means
    the windows overlap and nothing was missed.

    Reported per feed rather than in aggregate: 한국경제 경제 held 4.0 hours of
    history when 인포스탁 held 101.6, so a gap that is harmless for most of the
    fourteen can still be lossy for two of them.

    ``unfetched`` names the feeds that did not answer this run, and exists
    because their absence is otherwise indistinguishable from health. A feed
    that fails to connect contributes no ``buffer_oldest`` entry, so the loop
    below never sees it and the check reports "all feeds overlap" while the
    riskiest feed in the set is unaccounted for. That masking was live on
    2026-08-06/07: 전자신문 failed to connect in ten of thirty runs, and every
    briefing header in between said only ``kr_news/fetch``, which reads as a
    transient blip rather than as articles being gone.

    **Unverifiable is reported, but the verdict waits for the evidence.** A feed
    that did not answer is named in the detail every time. It fails the check
    only once its silence passes :data:`MAX_FEED_SILENCE`, because until then the
    question it raises is answerable: the next run where the feed *does* answer
    compares its buffer against the same ``newest_stored`` and settles whether
    the outage cost anything. Failing at the moment the evidence is missing,
    rather than at the moment it arrives, is what produced four alarms on
    2026-08-11 for a feed that lost nothing — 전자신문 published nothing at all
    between 13:30Z and 21:00Z, and the 22:00Z run recovered every article. Over
    the sixty runs to 2026-08-11 **every** failure of this check was this branch
    and not one measured a loss, while each recovering run passed.

    The bound exists so a feed that never comes back cannot stay silent in the
    exit code: at that point no future run can settle the question either, and
    the honest report is failure. ``config/news_feeds.yaml`` disabled 머니투데이
    on exactly that reasoning — a check that fails every hour stops being read.

    Silence is measured from ``newest_stored``, the last article *published*
    rather than the last successful poll, because the poll history is not stored.
    A quiet feed therefore looks darker than it is, so the proxy errs early
    rather than late, which is the safe direction. One consequence to know:
    :func:`_stored_files` looks back two days, so a feed dark for longer drops
    out of ``newest_stored`` and stops being reported at all. The usable
    detection window is 24–48 hours.
    """
    now = to_utc(now or now_utc())
    losses: list[str] = []
    compared = 0

    for feed, oldest in sorted(buffer_oldest.items()):
        previous = newest_stored.get(feed)
        if previous is None:
            continue  # nothing stored for this feed yet; no window to compare
        compared += 1
        if oldest > previous:
            missed = (oldest - previous).total_seconds() / 3600
            losses.append(f"{feed} lost {missed:.1f}h (buffer starts at {oldest:%Y-%m-%d %H:%M}Z)")

    # Only feeds with stored history are unverifiable; one that has never been
    # collected has no window to have rolled past in the first place.
    blind = sorted(name for name in set(unfetched) if name in newest_stored)
    unknown = [
        f"{name} unverified, last stored article "
        f"{(pd.Timestamp(newest_stored[name]).tz_convert('UTC')):%Y-%m-%d %H:%M}Z"
        for name in blind
    ]
    stale = [name for name in blind if now - to_utc(newest_stored[name]) > MAX_FEED_SILENCE]

    if losses or unknown:
        parts = []
        if losses:
            parts.append(
                f"{len(losses)} of {compared} feeds rolled past the last stored article — "
                + "; ".join(losses)
            )
        if unknown:
            parts.append(
                f"{len(unknown)} feeds did not answer, so their loss is unmeasured — "
                + "; ".join(unknown)
            )
        if stale:
            parts.append(
                f"{len(stale)} silent past the "
                f"{MAX_FEED_SILENCE.total_seconds() / 3600:.0f}h limit, so no later run can "
                f"settle it — " + "; ".join(sorted(stale))
            )
        elif not losses:
            parts.append("the next run that answers will settle it")
        return CheckResult("feed_continuity", not (losses or stale), ". ".join(parts))
    if not compared:
        return CheckResult("feed_continuity", True, "no feed has prior articles to compare against")
    return CheckResult("feed_continuity", True, f"{compared} feeds overlap the stored history")


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
    buffer_oldest: Mapping[str, pd.Timestamp] | None = None,
    newest_stored: Mapping[str, pd.Timestamp] | None = None,
    unfetched: Iterable[str] = (),
) -> ValidationReport:
    """Run the four checks, one of them substituted. See the module docstring.

    ``buffer_oldest``, ``newest_stored`` and ``unfetched`` are what
    :func:`check_feed_continuity` needs. They default to empty, which makes that
    check a no-op — the right behaviour for callers validating a frame in
    isolation, since a frame carries no record of what its feeds were holding at
    the time, nor of which ones failed to answer.
    """
    return validate(
        COLLECTOR,
        [
            check_schema(df, SCHEMA),
            check_missing_ratio(df, MISSING_THRESHOLDS)
            if len(df)
            else CheckResult("missing_ratio", True, "no rows"),
            check_collection_gap(df, previous_run, now=now),
            check_feed_continuity(buffer_oldest or {}, newest_stored or {}, unfetched, now=now),
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


def _stored_files(root: Path, day: dt.date) -> list[tuple[dt.date, Path]]:
    """Run files for ``day`` and the day before, oldest first.

    The previous day is not optional. Reading only ``day`` makes the collector
    forget everything at 00:00 UTC — 09:00 KST, the Korean market open, the
    busiest hour there is. Every buffered article would re-read as new, and
    :func:`last_run_at` would report "first run" and skip the gap check exactly
    when it matters most. One day of lookback closes that while keeping the read
    bounded; feeds hold hours of history, never days.
    """
    directory = root / "kr" / "news"
    files: list[tuple[dt.date, Path]] = []
    for offset in (1, 0):
        stamp = day - dt.timedelta(days=offset)
        day_dir = directory / stamp.isoformat()
        if day_dir.exists():
            files.extend((stamp, path) for path in sorted(day_dir.glob("*.jsonl.gz")))
    return files


def _read_stored(root: Path, day: dt.date) -> Iterator[dict]:
    for _, path in _stored_files(root, day):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def seen_ids(root: Path, day: dt.date) -> set[str]:
    """Article ids already stored for ``day`` or the day before."""
    return {row["article_id"] for row in _read_stored(root, day)}


def newest_stored_per_feed(root: Path, day: dt.date) -> dict[str, pd.Timestamp]:
    """Latest ``published_at`` already stored, per feed.

    This is the reference point for :func:`check_feed_continuity`: anything a
    feed published after this instant that is no longer in its buffer was missed.
    """
    newest: dict[str, pd.Timestamp] = {}
    for row in _read_stored(root, day):
        published = to_utc(pd.Timestamp(row["published_at"]))
        current = newest.get(row["feed"])
        if current is None or published > current:
            newest[row["feed"]] = published
    return newest


def last_run_at(root: Path, day: dt.date) -> pd.Timestamp | None:
    """Timestamp of the most recent run stored for ``day`` or the day before."""
    files = _stored_files(root, day)
    if not files:
        return None
    stamp, path = files[-1]
    clock = path.stem.removesuffix(".jsonl").split("-")[0]
    return to_utc(pd.Timestamp(f"{stamp.isoformat()} {clock[:2]}:{clock[2:]}", tz="UTC"))


def write_run(df: pd.DataFrame, root: Path, at: pd.Timestamp) -> Path:
    """Write one run's new articles, never overwriting an existing file.

    An empty ``df`` writes an empty file rather than none. That file is the
    record that the run happened, which :func:`last_run_at` reads off the
    filename; readers skip it because :func:`_read_stored` yields per non-blank
    line and there are none.
    """
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


@dataclass(frozen=True)
class FeedFailure:
    """One feed that did not produce articles this run, and why it matters.

    ``transient`` separates the two kinds, because they deserve different
    verdicts. A network failure is an absence of evidence that a later run can
    resolve, so :func:`check_feed_continuity` decides what it cost. A parse
    failure is evidence of a defect — malformed XML is malformed for everyone,
    every run — and always fails the ``fetch`` check on its own.
    """

    name: str
    reason: str
    transient: bool


def _fetch_one(
    feed: NewsFeed,
    collected_at: pd.Timestamp,
    *,
    timeout: int,
    attempts: int,
) -> tuple[pd.DataFrame | None, FeedFailure | None]:
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
            return None, FeedFailure(feed.name, f"{feed.name}: {exc}", transient=False)

    return None, FeedFailure(feed.name, f"{last} after {attempts} attempts", transient=True)


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
    failures: list[FeedFailure] = []
    unfetched: list[str] = []

    for feed in feeds:
        parsed, failure = _fetch_one(feed, collected_at, timeout=timeout, attempts=attempts)
        if failure:
            failures.append(failure)
            unfetched.append(feed.name)
        elif parsed is not None and not parsed.empty:
            frames.append(parsed)

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(SCHEMA))

    # Taken before dedup, and it has to be: dedup removes exactly the overlap
    # with stored history, so the surviving rows would place every buffer's
    # start after the last stored article and report total loss every run.
    buffer_oldest = (
        {} if df.empty else df.groupby("feed")["published_at"].min().to_dict()  # type: ignore[assignment]
    )

    fetched = len(df)
    if not df.empty:
        df = df.drop_duplicates(subset="article_id", keep="first")
        already = seen_ids(root, collected_at.date())
        df = df[~df["article_id"].isin(already)].reset_index(drop=True)

    report = validate_frame(
        df,
        feeds,
        previous_run=last_run_at(root, collected_at.date()),
        now=collected_at,
        buffer_oldest=buffer_oldest,
        newest_stored=newest_stored_per_feed(root, collected_at.date()),
        unfetched=unfetched,
    )
    # A malformed feed always fails here. A feed that merely did not answer
    # defers to `feed_continuity`, which is the check holding the evidence about
    # what the silence cost — otherwise one outage fails two checks and mails an
    # alarm twice for a loss that has not been shown to exist.
    malformed = [failure for failure in failures if not failure.transient]
    continuity = next(r for r in report.results if r.name == "feed_continuity")
    report.add(
        CheckResult(
            "fetch",
            not malformed and continuity.passed,
            "; ".join(failure.reason for failure in failures)
            or f"{len(feeds)} feeds, {fetched} items seen, {len(df)} new",
        )
    )
    return df, report


def main() -> int:
    """Entry point for the hourly collection workflow.

    Two rules, kept separate because conflating them is what made this function
    wrong on 2026-08-08.

    **The run always writes what it collected, even when that is nothing.** A
    zero-row file records that the feeds were polled and held nothing new, which
    is a different fact from not having polled — and the only place that
    difference is recorded is the file's existence, since :func:`last_run_at`
    reads the run clock off the filename. Skipping the write froze
    ``previous_run`` through the quiet Korean night, and
    :func:`check_collection_gap` then reported a growing gap for a collector
    that was running on schedule and losing nothing.

    **The exit code reports validation, and nothing else.** Not whether articles
    arrived: a quiet hour is not a failure, and a run that stored articles while
    a feed timed out *is* one, because that feed's loss is unmeasured and
    unrecoverable. The workflow's commit step is ``if: always()`` so that a
    non-zero exit here still commits — a failing check must raise the alarm
    without costing the articles the run did collect.
    """

    feeds = load_news_feeds()
    now = now_utc()
    df, report = fetch(feeds, now=now)

    print(report.summary())
    for result in report.results:
        print(f"  {result}")

    path = write_run(df, Path("data/raw"), now)
    print(f"wrote {len(df)} articles to {path}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
