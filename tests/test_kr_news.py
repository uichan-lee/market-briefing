"""Tests for the KR news collector (SPEC §3.1).

Offline tests run against committed RSS payloads chosen to cover the three
description behaviours that actually occur: none (한국경제), short (매일경제),
and near-complete article text (뉴시스). One live test is marked ``network``.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

import pandas as pd
import pytest

from src.collectors.kr_news import (
    MAX_COLLECTION_GAP,
    SCHEMA,
    FeedError,
    article_id,
    check_collection_gap,
    check_feed_continuity,
    check_structural_invariants,
    fetch,
    last_run_at,
    newest_stored_per_feed,
    parse_feed,
    parse_pubdate,
    run_path,
    seen_ids,
    validate_frame,
    write_run,
)
from src.util.config import NewsFeed, load_news_feeds
from src.util.session import next_tradeable_open

FIXTURES = Path(__file__).parent / "fixtures"
NOW = pd.Timestamp("2026-08-03 09:00:00", tz="UTC")

FEEDS = {
    "hankyung_finance": NewsFeed(
        "hankyung_finance", "한국경제", "증권", "https://x/", "hankyung.com"
    ),
    "mk_stock": NewsFeed("mk_stock", "매일경제", "증권", "https://x/", "mk.co.kr"),
    "newsis_economy": NewsFeed("newsis_economy", "뉴시스", "경제", "https://x/", "newsis.com"),
}


def _parse(name: str) -> pd.DataFrame:
    xml = (FIXTURES / f"rss_{name}.xml").read_bytes()
    return parse_feed(xml, FEEDS[name], NOW)


@pytest.fixture
def frame() -> pd.DataFrame:
    return _parse("newsis_economy")


# --- parsing --------------------------------------------------------------


@pytest.mark.parametrize("name", list(FEEDS))
def test_every_fixture_parses_to_the_committed_schema(name):
    df = _parse(name)
    assert not df.empty
    assert list(df.columns) == list(SCHEMA)


@pytest.mark.parametrize("name", list(FEEDS))
def test_dtypes_match_the_declared_schema(name):
    df = _parse(name)
    for column, want in SCHEMA.items():
        assert str(df[column].dtype) == want, f"{name}.{column}"


def test_a_headline_only_outlet_still_yields_articles():
    """한국경제 supplies no description. That is valid data, not a fault — and
    SPEC §6.1 clustering pairs these with a body-carrying re-report."""
    df = _parse("hankyung_finance")
    assert len(df) > 10
    assert (df["description"] == "").all()
    assert (df["title"].str.len() > 0).all()


def test_a_long_description_outlet_carries_real_body_text():
    df = _parse("newsis_economy")
    assert df["description"].str.len().median() > 200


def test_published_at_is_timezone_aware_utc(frame):
    assert frame["published_at"].dt.tz is not None
    assert str(frame["published_at"].dt.tz) == "UTC"


def test_malformed_xml_raises_rather_than_returning_empty():
    """An empty frame would read as 'the outlet published nothing', which is a
    completely different fact from 'the feed is broken'."""
    with pytest.raises(FeedError, match="malformed XML"):
        parse_feed(b"<rss><item>", FEEDS["mk_stock"], NOW)


def test_an_item_without_a_parseable_date_is_dropped_not_defaulted():
    """A guessed timestamp would silently violate the look-ahead rule."""
    xml = b"""<rss><channel>
      <item><title>ok</title><link>https://mk.co.kr/1</link>
            <pubDate>Mon, 03 Aug 2026 10:00:00 +0900</pubDate></item>
      <item><title>no date</title><link>https://mk.co.kr/2</link></item>
      <item><title>junk date</title><link>https://mk.co.kr/3</link>
            <pubDate>not a date</pubDate></item>
    </channel></rss>"""
    df = parse_feed(xml, FEEDS["mk_stock"], NOW)
    assert list(df["title"]) == ["ok"]


def test_an_item_without_a_link_is_dropped():
    xml = b"""<rss><channel>
      <item><title>keep</title><link>https://mk.co.kr/1</link>
            <pubDate>Mon, 03 Aug 2026 10:00:00 +0900</pubDate></item>
      <item><title>no link</title>
            <pubDate>Mon, 03 Aug 2026 10:00:00 +0900</pubDate></item>
    </channel></rss>"""
    assert list(parse_feed(xml, FEEDS["mk_stock"], NOW)["title"]) == ["keep"]


def test_a_feed_whose_items_all_fail_to_parse_raises():
    """The bug this catches shipped once: 매일경제 emits '+09:00' where RFC 822
    wants '+0900', every item was silently dropped, parse returned an empty
    frame, and three outlets vanished without a single failed check."""
    xml = b"""<rss><channel>
      <item><title>t</title><link>https://mk.co.kr/1</link>
            <pubDate>garbage</pubDate></item>
    </channel></rss>"""
    with pytest.raises(FeedError, match="none parseable"):
        parse_feed(xml, FEEDS["mk_stock"], NOW)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mon, 03 Aug 2026 18:02:00 +0900", "2026-08-03T09:02:00+00:00"),
        ("Mon, 03 Aug 2026 18:02:00 +09:00", "2026-08-03T09:02:00+00:00"),  # 매일경제
        ("Mon, 3 Aug 2026 18:02:00 +0900", "2026-08-03T09:02:00+00:00"),
    ],
)
def test_offset_formats_seen_in_the_wild_all_parse(raw, expected):
    assert parse_pubdate(raw).isoformat() == expected


@pytest.mark.parametrize("raw", ["", "not a date", "Mon, 03 Aug 2026 18:02:00"])
def test_unparseable_or_naive_dates_return_none(raw):
    """A naive timestamp has no instant, and guessing one would break the
    look-ahead rule invisibly."""
    assert parse_pubdate(raw) is None


def test_mk_stock_actually_parses_now():
    """Regression guard on the offset bug, against the real committed payload."""
    df = _parse("mk_stock")
    assert len(df) > 10


# --- the look-ahead boundary ----------------------------------------------


def test_known_at_utc_is_the_next_session_open(frame):
    """CLAUDE.md: news published during a session is tradeable at the next
    session's open. Features join on this column, never on published_at."""
    row = frame.iloc[0]
    assert row["known_at_utc"] == next_tradeable_open("KR", row["published_at"])


def test_known_at_utc_is_never_before_publication(frame):
    assert (frame["known_at_utc"] >= frame["published_at"]).all()


# --- article identity -----------------------------------------------------


def test_guid_is_preferred_over_link():
    """Outlets rewrite URLs after publication; a link hash would then read as a
    new article on the next poll and inflate news_volume_z."""
    assert article_id("g1", "https://a/1") == article_id("g1", "https://a/1?utm=x")
    assert article_id(None, "https://a/1") != article_id(None, "https://a/1?utm=x")


def test_article_id_is_stable_across_reparses():
    first, second = _parse("mk_stock"), _parse("mk_stock")
    assert list(first["article_id"]) == list(second["article_id"])


def test_article_ids_are_unique_within_a_feed(frame):
    assert not frame["article_id"].duplicated().any()


# --- check three: collection continuity -----------------------------------


def test_a_first_run_has_no_gap_to_report(frame):
    assert check_collection_gap(frame, None, now=NOW).passed


def test_an_hourly_cadence_passes(frame):
    previous = NOW - dt.timedelta(hours=1)
    assert check_collection_gap(frame, previous, now=NOW).passed


def test_a_long_silence_is_a_failure(frame):
    """Anything missed in the gap is unrecoverable, so silence is a defect
    rather than an idle period."""
    previous = NOW - MAX_COLLECTION_GAP - dt.timedelta(minutes=1)
    result = check_collection_gap(frame, previous, now=NOW)
    assert not result.passed
    assert "cannot be re-fetched" in result.detail


# --- lost-article detection -----------------------------------------------
#
# The gap check above infers loss from a clock. These assert the check that
# measures it: a buffer whose oldest surviving item postdates the newest stored
# one has rolled past everything known, and RSS cannot serve the difference.


def test_overlapping_buffers_report_no_loss():
    oldest = {"hankyung_finance": NOW - dt.timedelta(hours=4)}
    stored = {"hankyung_finance": NOW - dt.timedelta(hours=1)}
    result = check_feed_continuity(oldest, stored)
    assert result.passed
    assert "1 feeds overlap" in result.detail


def test_a_buffer_that_rolled_past_the_stored_history_is_a_failure():
    oldest = {"hankyung_finance": NOW - dt.timedelta(hours=1)}
    stored = {"hankyung_finance": NOW - dt.timedelta(hours=4)}
    result = check_feed_continuity(oldest, stored)
    assert not result.passed
    assert "hankyung_finance lost 3.0h" in result.detail


def test_only_the_feeds_that_actually_lost_articles_are_named():
    """The point of doing this per feed: the measured buffers span 4 hours to
    101, so one gap is simultaneously lossy and harmless."""
    oldest = {
        "hankyung_finance": NOW - dt.timedelta(hours=1),
        "infostock_all": NOW - dt.timedelta(hours=90),
    }
    stored = {
        "hankyung_finance": NOW - dt.timedelta(hours=4),
        "infostock_all": NOW - dt.timedelta(hours=4),
    }
    result = check_feed_continuity(oldest, stored)
    assert not result.passed
    assert "1 of 2 feeds" in result.detail
    assert "infostock_all" not in result.detail


def test_a_feed_with_no_stored_history_is_skipped_not_failed():
    """A newly added feed has nothing to compare against. That is not loss."""
    result = check_feed_continuity({"brand_new": NOW}, {})
    assert result.passed
    assert "no feed has prior articles" in result.detail


def test_feed_continuity_is_inert_when_the_caller_supplies_nothing(frame):
    """validate_frame is also called on frames in isolation, which carry no
    record of what the buffers held."""
    report = validate_frame(frame, list(FEEDS.values()), previous_run=None, now=NOW)
    assert report.ok, report.summary()
    assert any(r.name == "feed_continuity" and r.passed for r in report.results)


# --- check four's substitute ----------------------------------------------


def test_a_clean_frame_passes_the_invariants(frame):
    assert check_structural_invariants(frame, list(FEEDS.values()), now=NOW).passed


def test_an_epoch_timestamp_is_caught(frame):
    broken = frame.copy()
    broken.loc[broken.index[0], "published_at"] = pd.Timestamp("1970-01-01", tz="UTC")
    result = check_structural_invariants(broken, list(FEEDS.values()), now=NOW)
    assert not result.passed
    assert "older than" in result.detail


def test_a_future_timestamp_is_caught(frame):
    broken = frame.copy()
    broken.loc[broken.index[0], "published_at"] = NOW + dt.timedelta(days=2)
    result = check_structural_invariants(broken, list(FEEDS.values()), now=NOW)
    assert not result.passed
    assert "future" in result.detail


def test_a_link_outside_the_declared_domain_is_caught(frame):
    """Catches a feed redirected or replaced with someone else's content — the
    well-formed-but-wrong case a schema check sails past."""
    broken = frame.copy()
    broken.loc[broken.index[0], "link"] = "https://example.com/hijacked"
    result = check_structural_invariants(broken, list(FEEDS.values()), now=NOW)
    assert not result.passed
    assert "declared domain" in result.detail


def test_duplicated_article_ids_are_caught(frame):
    doubled = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    result = check_structural_invariants(doubled, list(FEEDS.values()), now=NOW)
    assert not result.passed
    assert "duplicated" in result.detail


def test_the_full_report_passes_on_clean_input(frame):
    report = validate_frame(frame, list(FEEDS.values()), previous_run=None, now=NOW)
    assert report.ok, report.summary()


# --- storage --------------------------------------------------------------


def test_run_path_partitions_by_day_and_run(tmp_path):
    path = run_path(tmp_path, NOW)
    assert path.parent.name == "2026-08-03"
    assert path.name == "0900.jsonl.gz"


def test_a_written_run_round_trips(tmp_path, frame):
    path = write_run(frame, tmp_path, NOW)
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    assert len(rows) == len(frame)
    assert rows[0]["article_id"] == frame.iloc[0]["article_id"]


def test_a_rerun_never_overwrites(tmp_path, frame):
    """CLAUDE.md rule 1. data/raw/ is the backtest dataset."""
    first = write_run(frame, tmp_path, NOW)
    second = write_run(frame, tmp_path, NOW)
    assert first != second
    assert first.exists() and second.exists()
    assert "-v2" in second.name


def test_seen_ids_reads_back_what_was_written(tmp_path, frame):
    write_run(frame, tmp_path, NOW)
    assert seen_ids(tmp_path, NOW.date()) == set(frame["article_id"])


def test_seen_ids_is_empty_for_an_untouched_day(tmp_path):
    assert seen_ids(tmp_path, dt.date(2026, 1, 1)) == set()


def test_last_run_at_finds_the_most_recent_run(tmp_path, frame):
    write_run(frame, tmp_path, NOW - dt.timedelta(hours=3))
    write_run(frame, tmp_path, NOW)
    assert last_run_at(tmp_path, NOW.date()) == NOW


def test_last_run_at_is_none_before_any_run(tmp_path):
    assert last_run_at(tmp_path, NOW.date()) is None


def test_a_rerun_suffix_does_not_break_the_run_timestamp(tmp_path, frame):
    """``1251-v2.jsonl.gz`` is the path CLAUDE.md rule 1 produces. Parsing the
    whole stem as a clock would raise on it."""
    write_run(frame, tmp_path, NOW)
    write_run(frame, tmp_path, NOW)
    assert last_run_at(tmp_path, NOW.date()) == NOW


def test_newest_stored_is_tracked_per_feed(tmp_path, frame):
    other = frame.copy()
    other["feed"] = "mk_stock"
    other["article_id"] = other["article_id"] + "x"
    other["published_at"] = other["published_at"] - dt.timedelta(days=1)
    write_run(pd.concat([frame, other], ignore_index=True), tmp_path, NOW)

    newest = newest_stored_per_feed(tmp_path, NOW.date())
    assert newest["newsis_economy"] == frame["published_at"].max()
    assert newest["mk_stock"] == other["published_at"].max()


# The 00:00 UTC rollover is 09:00 KST — the Korean market open, and the busiest
# hour of the day. Reading only the current day's directory would make the
# collector forget everything at exactly that moment: every buffered article
# would re-read as new, and the gap check would report a first run.


def test_dedup_survives_the_day_boundary(tmp_path, frame):
    yesterday = NOW - dt.timedelta(hours=1)  # 2026-08-02 23:00Z
    write_run(frame, tmp_path, yesterday)
    assert seen_ids(tmp_path, NOW.date()) == set(frame["article_id"])


def test_the_gap_check_still_has_a_reference_across_the_day_boundary(tmp_path, frame):
    yesterday = NOW - dt.timedelta(hours=1)
    write_run(frame, tmp_path, yesterday)
    assert last_run_at(tmp_path, NOW.date()) == yesterday


def test_the_lookback_stops_at_one_day(tmp_path, frame):
    """Bounded on purpose. Feeds hold hours of history, never days, so reading
    further back costs time every run and buys nothing."""
    write_run(frame, tmp_path, NOW - dt.timedelta(days=2))
    assert seen_ids(tmp_path, NOW.date()) == set()


# --- config ---------------------------------------------------------------


def test_the_committed_feed_config_loads():
    feeds = load_news_feeds()
    assert len(feeds) >= 10
    assert all(f.url.startswith("https://") for f in feeds)


def test_every_committed_feed_declares_a_domain():
    assert all(f.domain and "." in f.domain for f in load_news_feeds())


def test_committed_feed_names_are_unique():
    names = [f.name for f in load_news_feeds()]
    assert len(names) == len(set(names))


# --- live -----------------------------------------------------------------


@pytest.mark.network
def test_live_fetch_then_immediate_refetch_finds_nothing_new(tmp_path):
    """Proves dedup works rather than merely existing: hourly polling re-reads
    the same buffer, so the second run must be almost entirely already-seen."""
    feeds = load_news_feeds()[:3]

    first, report = fetch(feeds, root=tmp_path, now=pd.Timestamp.now(tz="UTC"))
    assert report.ok, report.summary()
    assert len(first) > 0
    write_run(first, tmp_path, pd.Timestamp.now(tz="UTC"))

    second, _ = fetch(feeds, root=tmp_path, now=pd.Timestamp.now(tz="UTC"))
    assert len(second) < len(first) * 0.2


def test_a_naive_timestamp_needs_a_declared_zone():
    """인포스탁 emits '2026-08-03 17:11:18'. Without a declared zone the parser
    refuses rather than guessing — reading KST as UTC would place every article
    9 hours early and make it knowable before publication."""
    assert parse_pubdate("2026-08-03 17:11:18") is None
    assert parse_pubdate("2026-08-03 17:11:18", "Asia/Seoul").isoformat() == (
        "2026-08-03T08:11:18+00:00"
    )


def test_a_declared_zone_never_overrides_an_explicit_offset():
    """The feed's declaration is a fallback for naive values only."""
    explicit = parse_pubdate("Mon, 03 Aug 2026 18:02:00 +0900", "America/New_York")
    assert explicit.isoformat() == "2026-08-03T09:02:00+00:00"


def test_the_committed_config_declares_a_zone_only_where_needed():
    feeds = {f.name: f for f in load_news_feeds()}
    assert feeds["infostock_all"].timezone == "Asia/Seoul"
    assert feeds["hankyung_finance"].timezone is None


def test_a_transient_network_failure_is_retried(monkeypatch):
    """A dropped feed costs articles that cannot be re-fetched later, so the
    seconds spent retrying are cheap. 머니투데이 timed out from an Actions runner
    while answering fine locally."""
    import requests

    from src.collectors import kr_news

    calls = {"n": 0}
    body = (FIXTURES / "rss_mk_stock.xml").read_bytes()

    class Response:
        status_code = 200
        content = body

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectTimeout("boom")
        return Response()

    monkeypatch.setattr(kr_news.requests, "get", flaky)
    monkeypatch.setattr(kr_news.time, "sleep", lambda _: None)

    parsed, failure = kr_news._fetch_one(FEEDS["mk_stock"], NOW, timeout=1, attempts=3)
    assert failure is None
    assert calls["n"] == 2
    assert len(parsed) > 10


def test_a_persistent_failure_reports_the_attempt_count(monkeypatch):
    import requests

    from src.collectors import kr_news

    def always_fails(*args, **kwargs):
        raise requests.ConnectTimeout("boom")

    monkeypatch.setattr(kr_news.requests, "get", always_fails)
    monkeypatch.setattr(kr_news.time, "sleep", lambda _: None)

    parsed, failure = kr_news._fetch_one(FEEDS["mk_stock"], NOW, timeout=1, attempts=3)
    assert parsed is None
    assert "3 attempts" in failure


def test_a_parse_failure_is_not_retried(monkeypatch):
    """Malformed XML will be malformed again; retrying only wastes the window."""
    from src.collectors import kr_news

    calls = {"n": 0}

    class Response:
        status_code = 200
        content = b"<rss><channel></channel></rss>"

    def counted(*args, **kwargs):
        calls["n"] += 1
        return Response()

    monkeypatch.setattr(kr_news.requests, "get", counted)
    monkeypatch.setattr(kr_news.time, "sleep", lambda _: None)

    parsed, failure = kr_news._fetch_one(FEEDS["mk_stock"], NOW, timeout=1, attempts=3)
    assert parsed is None
    assert calls["n"] == 1
    assert "none parseable" in failure
