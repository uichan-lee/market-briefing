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

from src.collectors import kr_news
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
from src.collectors.validate import CheckResult, ValidationReport
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


# --- the feeds that did not answer -----------------------------------------


def test_a_feed_that_did_not_answer_is_reported_not_skipped():
    """The masking bug, in one assertion. A feed that fails to connect sends no
    buffer, so the comparison loop never sees it and every run in the outage
    read "1 feeds overlap the stored history" — a pass, while the fastest feed
    in the set was unaccounted for. Live 2026-08-06/07: 전자신문 failed to
    connect in ten of thirty runs and no header ever said so.

    Reporting is what this test guards, and it is unchanged. The *verdict*
    moved on 2026-08-11: a silence this short no longer fails, because the next
    run that answers measures what it cost. See the tests below for the bound
    at which waiting stops being honest."""
    oldest = {"hankyung_finance": NOW - dt.timedelta(hours=6)}
    stored = {
        "hankyung_finance": NOW - dt.timedelta(hours=4),
        "etnews_economy": NOW - dt.timedelta(hours=4),
    }
    result = check_feed_continuity(oldest, stored, unfetched=["etnews_economy"], now=NOW)

    assert result.passed
    assert "etnews_economy unverified" in result.detail
    assert "did not answer" in result.detail


def test_an_unanswered_feed_with_no_stored_history_is_not_reported():
    """Nothing has been collected from it, so no window can have rolled past.
    Reporting it would turn a newly added feed into a permanent failure."""
    result = check_feed_continuity({}, {}, unfetched=["brand_new"])
    assert result.passed


def test_loss_and_silence_are_reported_together():
    """The two are independent and one must not hide the other: a measured loss
    on one feed says nothing about the feed that never answered.

    The failure here comes from the measured loss alone — the silence is inside
    the waiting bound and contributes reporting, not a verdict."""
    oldest = {"hankyung_finance": NOW - dt.timedelta(hours=1)}
    stored = {
        "hankyung_finance": NOW - dt.timedelta(hours=4),
        "etnews_economy": NOW - dt.timedelta(hours=4),
    }
    result = check_feed_continuity(oldest, stored, unfetched=["etnews_economy"], now=NOW)

    assert not result.passed
    assert "hankyung_finance lost 3.0h" in result.detail
    assert "etnews_economy unverified" in result.detail


def test_a_clean_run_still_passes():
    """The guard must not turn every healthy run into a failure."""
    oldest = {"hankyung_finance": NOW - dt.timedelta(hours=6)}
    stored = {"hankyung_finance": NOW - dt.timedelta(hours=4)}
    result = check_feed_continuity(oldest, stored, unfetched=[], now=NOW)
    assert result.passed


# --- when the silence stops being answerable -------------------------------
#
# A feed that did not answer raises a question the next run can settle, by
# comparing that run's buffer against the same stored history. Failing while the
# evidence is merely absent mailed four alarms on 2026-08-11 for a feed that had
# published nothing for 7.5 hours and lost none of it. Failing once the evidence
# can no longer arrive is a different statement, and this is where it starts.


def test_a_short_silence_waits_for_the_next_run():
    stored = {"etnews_main": NOW - kr_news.MAX_FEED_SILENCE + dt.timedelta(hours=1)}
    result = check_feed_continuity({}, stored, unfetched=["etnews_main"], now=NOW)

    assert result.passed
    assert "etnews_main unverified" in result.detail
    assert "settle it" in result.detail


def test_a_silence_past_the_limit_fails():
    """No later run can answer for this one, so the honest report is failure —
    the reasoning config/news_feeds.yaml used to disable 머니투데이 rather than
    leave it failing every hour."""
    stored = {"etnews_main": NOW - kr_news.MAX_FEED_SILENCE - dt.timedelta(minutes=1)}
    result = check_feed_continuity({}, stored, unfetched=["etnews_main"], now=NOW)

    assert not result.passed
    assert "24h limit" in result.detail


def test_the_2026_08_11_alarms_would_not_fire_now():
    """Regression, replayed from the four runs that mailed that morning plus the
    2026-08-10 19:37 KST one. Every value below is from the Actions log: the
    feeds that did not answer, and the `last stored article` each failure
    printed. Not one of them lost an article — 전자신문 published nothing
    between 13:30Z and 21:00Z, and the 22:00Z run recovered the rest."""
    observed = [
        (
            "2026-08-10 10:37",
            {"etnews_main": "2026-08-10 08:06", "asiae_stock": "2026-08-10 08:39"},
        ),
        ("2026-08-10 18:05", {"etnews_main": "2026-08-10 13:30"}),
        (
            "2026-08-10 19:18",
            {"etnews_main": "2026-08-10 13:30", "asiae_stock": "2026-08-10 10:18"},
        ),
        ("2026-08-10 21:03", {"etnews_main": "2026-08-10 13:30"}),
        ("2026-08-10 22:58", {"etnews_main": "2026-08-10 21:00"}),
    ]
    for ran_at, silent in observed:
        stored = {feed: pd.Timestamp(when, tz="UTC") for feed, when in silent.items()}
        result = check_feed_continuity(
            {}, stored, unfetched=list(silent), now=pd.Timestamp(ran_at, tz="UTC")
        )
        assert result.passed, f"{ran_at} still fails: {result.detail}"
        for feed in silent:
            assert f"{feed} unverified" in result.detail


def test_a_measured_loss_still_fails_even_though_silence_no_longer_does():
    """This branch has never fired in production — sixty runs to 2026-08-11 and
    every continuity failure was the silence branch — so the regression test is
    the only thing holding it."""
    oldest = {"etnews_main": NOW - dt.timedelta(hours=1)}
    stored = {"etnews_main": NOW - dt.timedelta(hours=5)}
    result = check_feed_continuity(oldest, stored, unfetched=[], now=NOW)

    assert not result.passed
    assert "etnews_main lost 4.0h" in result.detail


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


# A run that polled and found nothing is not a run that did not happen, and the
# file's existence is the only place that difference is recorded. Skipping the
# write froze `last_run_at`, so `check_collection_gap` measured time since the
# last *article* instead of time since the last *poll* — and reported a growing
# gap through the quiet Korean night of 2026-08-08 for a collector that was
# running on schedule and losing nothing.


def test_a_run_with_nothing_new_still_records_that_it_ran(tmp_path):
    empty = pd.DataFrame(columns=list(SCHEMA))
    path = write_run(empty, tmp_path, NOW)

    assert path.exists()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        assert handle.read() == ""
    assert last_run_at(tmp_path, NOW.date()) == NOW
    assert seen_ids(tmp_path, NOW.date()) == set()
    assert newest_stored_per_feed(tmp_path, NOW.date()) == {}


def test_an_empty_run_does_not_hide_the_articles_around_it(tmp_path, frame):
    write_run(frame, tmp_path, NOW - dt.timedelta(hours=1))
    write_run(pd.DataFrame(columns=list(SCHEMA)), tmp_path, NOW)

    assert seen_ids(tmp_path, NOW.date()) == set(frame["article_id"])
    assert newest_stored_per_feed(tmp_path, NOW.date()) == {
        "newsis_economy": frame["published_at"].max()
    }


def test_quiet_hours_do_not_accumulate_a_gap(tmp_path, frame):
    """Regression for 2026-08-08: five scheduled runs failed in a row because
    the feeds had nothing new, not because anything was missed."""
    write_run(frame, tmp_path, NOW)
    quiet = NOW
    for _ in range(5):
        quiet += dt.timedelta(hours=1)
        write_run(pd.DataFrame(columns=list(SCHEMA)), tmp_path, quiet)
        result = check_collection_gap(
            pd.DataFrame(columns=list(SCHEMA)),
            last_run_at(tmp_path, quiet.date()),
            now=quiet + dt.timedelta(hours=1),
        )
        assert result.passed, result.detail


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


# --- exit code ------------------------------------------------------------
#
# The exit code answers "did validation pass", not "did articles arrive". Those
# two were conflated until 2026-08-08, which inverted the alarm: quiet hours
# that lost nothing mailed a failure, while runs that stored articles with a
# feed timed out — unmeasured, unrecoverable loss — went green and silent.


def _run_main(monkeypatch, tmp_path, df, results):
    report = ValidationReport("kr_news")
    for name, passed in results:
        report.add(CheckResult(name, passed, "test"))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(kr_news, "load_news_feeds", lambda: list(FEEDS.values()))
    monkeypatch.setattr(kr_news, "now_utc", lambda: NOW)
    monkeypatch.setattr(kr_news, "fetch", lambda feeds, now: (df, report))
    return kr_news.main()


def test_a_quiet_run_that_passed_its_checks_exits_zero(monkeypatch, tmp_path):
    empty = pd.DataFrame(columns=list(SCHEMA))
    assert _run_main(monkeypatch, tmp_path, empty, [("fetch", True)]) == 0
    assert last_run_at(tmp_path / "data" / "raw", NOW.date()) == NOW


def test_a_failed_check_exits_nonzero_even_though_articles_arrived(monkeypatch, tmp_path, frame):
    """The 2026-08-08 20:50Z case: 6 articles stored, etnews_main timed out, run
    reported green. The articles must still be written — the workflow's commit
    step is ``if: always()`` so the alarm never costs them."""
    assert _run_main(monkeypatch, tmp_path, frame, [("fetch", False)]) == 1
    assert seen_ids(tmp_path / "data" / "raw", NOW.date()) == set(frame["article_id"])


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


# --- what a feed outage does to the run's verdict ---------------------------
#
# One outage used to fail two checks — `fetch` on the connection and
# `feed_continuity` on the silence — so a single unreachable feed mailed an
# alarm and printed two header lines for a loss never shown to exist.


GOOD_FEED = NewsFeed("mk_stock", "매일경제", "증권", "https://good/", "mk.co.kr")
BROKEN_FEED = NewsFeed("hankyung_finance", "한국경제", "증권", "https://broken/", "hankyung.com")


def _fetch_with(monkeypatch, tmp_path, *, content: bytes | None):
    """Poll two feeds where one of them fails. ``content`` None means it never
    answered; bytes mean it answered with something unparseable."""
    import requests as real_requests

    from src.collectors import kr_news

    good = (FIXTURES / "rss_mk_stock.xml").read_bytes()

    class Response:
        def __init__(self, body):
            self.status_code = 200
            self.content = body

    def dispatch(url, **kwargs):
        if url != BROKEN_FEED.url:
            return Response(good)
        if content is None:
            raise real_requests.ConnectTimeout("boom")
        return Response(content)

    monkeypatch.setattr(kr_news.requests, "get", dispatch)
    monkeypatch.setattr(kr_news.time, "sleep", lambda _: None)
    return kr_news.fetch([GOOD_FEED, BROKEN_FEED], root=tmp_path, now=NOW, timeout=1, attempts=2)


def test_a_feed_that_did_not_answer_no_longer_fails_the_run(monkeypatch, tmp_path):
    _, report = _fetch_with(monkeypatch, tmp_path, content=None)

    assert report.ok, report.summary()
    fetch_result = next(r for r in report.results if r.name == "fetch")
    assert "hankyung_finance" in fetch_result.detail, "the outage must still be named"


def test_a_feed_that_answered_with_garbage_still_fails_the_run(monkeypatch, tmp_path):
    """Malformed XML is a defect, not an absence of evidence. No later run
    resolves it, so it fails now."""
    _, report = _fetch_with(monkeypatch, tmp_path, content=b"<rss><channel></channel></rss>")

    assert not report.ok
    assert [r.name for r in report.failures] == ["fetch"]


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
    assert "3 attempts" in failure.reason
    # Transient: a later run can still measure what this outage cost, so the
    # verdict belongs to check_feed_continuity rather than to the fetch check.
    assert failure.transient


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
    assert "none parseable" in failure.reason
    # Not transient: no later run resolves malformed XML, so this one fails the
    # fetch check on its own.
    assert not failure.transient
