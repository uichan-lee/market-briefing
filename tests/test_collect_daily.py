"""Tests for the daily collection driver. SPEC §1, §12 step 11.

The tests that matter most pin two constraints that are invisible in the code's
happy path: the morning run must never touch KRX (124 requests against a block
near 250, for a session that has not opened), and a re-fetch of an
already-stored date must revise via ``-vN`` — never overwrite, never freeze a
late-publishing value out of the dataset.
"""

from __future__ import annotations

import json

import pandas as pd

from scripts.collect_daily import RUNS, _differs, write_daily, write_status


def frame(**overrides) -> pd.DataFrame:
    base = {
        "date": [pd.Timestamp("2026-08-05")],
        "ticker": ["005930"],
        "close": [79600],
    }
    base.update(overrides)
    return pd.DataFrame(base)


# --- the run definitions ---------------------------------------------------


def test_the_morning_run_contains_no_krx_source():
    """The constraint the whole schedule is built around: kr_flow costs 124
    KRX requests per run against a block observed near 250, and at 07:07 KST
    the session being reported on has not opened. A KRX call in the morning
    list would spend half the daily budget fetching nothing new."""
    assert "kr_flow" not in RUNS["morning"]
    assert "kr_price" not in RUNS["morning"]


def test_the_canonical_us_path_is_evening_and_the_preview_is_morning():
    """Two vendors, two paths, never mixed — notes/step11-plan.md."""
    assert "us_price" in RUNS["evening"]
    assert "us_price" not in RUNS["morning"]
    assert "us_price_preview" in RUNS["morning"]
    assert "us_price_preview" not in RUNS["evening"]


def test_both_runs_collect_news():
    """An extra poll is free (dedup) and produces the fresh feed-continuity
    check the report header needs."""
    assert "kr_news" in RUNS["morning"]
    assert "kr_news" in RUNS["evening"]


# --- revise-if-different ---------------------------------------------------


def test_a_new_date_writes_its_base_file(tmp_path):
    new, revised = write_daily("kr_price", frame(), directory=tmp_path)
    assert (new, revised) == (1, 0)
    assert (tmp_path / "2026-08-05.parquet").exists()


def test_an_identical_refetch_writes_nothing(tmp_path):
    """The common case. Without this, the nightly window re-fetch would grow a
    -vN file per date per day, all saying nothing."""
    write_daily("kr_price", frame(), directory=tmp_path)
    new, revised = write_daily("kr_price", frame(), directory=tmp_path)
    assert (new, revised) == (0, 0)
    assert len(list(tmp_path.glob("*.parquet"))) == 1


def test_changed_data_revises_and_the_original_survives(tmp_path):
    """The reason the whole mechanism exists: KRX short balance lands T+2 and
    WTI up to four days late, so an already-written date can gain content.
    CLAUDE.md rule 1 — the original is never overwritten."""
    write_daily("kr_flow", frame(close=[79600]), directory=tmp_path)
    new, revised = write_daily("kr_flow", frame(close=[79700]), directory=tmp_path)

    assert (new, revised) == (0, 1)
    assert pd.read_parquet(tmp_path / "2026-08-05.parquet").iloc[0]["close"] == 79600
    assert pd.read_parquet(tmp_path / "2026-08-05-v2.parquet").iloc[0]["close"] == 79700


def test_a_refetch_matching_the_latest_revision_writes_no_v3(tmp_path):
    """The comparison runs against the *newest* version, not the base —
    otherwise every run after a revision would mint another copy."""
    write_daily("kr_flow", frame(close=[79600]), directory=tmp_path)
    write_daily("kr_flow", frame(close=[79700]), directory=tmp_path)
    new, revised = write_daily("kr_flow", frame(close=[79700]), directory=tmp_path)

    assert (new, revised) == (0, 0)
    assert not (tmp_path / "2026-08-05-v3.parquet").exists()


def test_macro_rows_are_compared_on_their_own_key(tmp_path):
    macro = pd.DataFrame({"date": [pd.Timestamp("2026-08-05")], "series": ["vix"], "value": [15.9]})
    write_daily("macro", macro, directory=tmp_path)
    grown = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-08-05")] * 2,
            "series": ["vix", "wti"],
            "value": [15.9, 84.3],
        }
    )
    new, revised = write_daily("macro", grown, directory=tmp_path)
    assert (new, revised) == (0, 1), "a late-publishing series must land as a revision"


def test_a_column_set_change_counts_as_different():
    assert _differs(frame(), frame().drop(columns=["close"]), ["date", "ticker"])


def test_a_shrunken_fetch_never_becomes_a_revision(tmp_path):
    """Hit live on 2026-08-06: the second Tiingo run inside one rate-limit
    hour returned 23 of 48 tickers, and the partial frame was minted as a -v2
    of a complete file. Less is not different — a fetch that failed partway
    must leave the store alone."""
    complete = frame(
        ticker=["005930", "000660"], close=[79600, 170000], date=[pd.Timestamp("2026-08-05")] * 2
    )
    write_daily("kr_price", complete, directory=tmp_path)

    partial = frame(ticker=["005930"], close=[79600], date=[pd.Timestamp("2026-08-05")])
    new, revised = write_daily("kr_price", partial, directory=tmp_path)

    assert (new, revised) == (0, 0)
    assert not (tmp_path / "2026-08-05-v2.parquet").exists()


def test_a_parquet_dtype_roundtrip_is_not_a_revision(tmp_path):
    """Measured live on 2026-08-06: datetime64[s] survives parquet as [ms],
    DataFrame.equals is dtype-strict, and macro minted identical -v2/-v3/-v4
    files inside ten minutes. Identity must be about values."""
    seconds = frame()
    seconds["date"] = seconds["date"].astype("datetime64[s]")
    write_daily("kr_price", seconds, directory=tmp_path)

    new, revised = write_daily("kr_price", seconds, directory=tmp_path)
    assert (new, revised) == (0, 0)
    assert not (tmp_path / "2026-08-05-v2.parquet").exists()


def test_a_grown_fetch_still_revises(tmp_path):
    """The inverse must keep working — that is the T+2 short-balance case."""
    partial = frame(ticker=["005930"], close=[79600], date=[pd.Timestamp("2026-08-05")])
    write_daily("kr_price", partial, directory=tmp_path)

    complete = frame(
        ticker=["005930", "000660"], close=[79600, 170000], date=[pd.Timestamp("2026-08-05")] * 2
    )
    new, revised = write_daily("kr_price", complete, directory=tmp_path)

    assert (new, revised) == (0, 1)


# --- the mid-session guard --------------------------------------------------


def test_kr_end_holds_back_the_session_still_trading():
    """A fetch during the session would store a provisional close whose
    known_at_utc claims finality — the look-ahead failure in miniature."""
    from scripts.collect_daily import kr_end

    mid_session = pd.Timestamp("2026-08-06 02:00", tz="UTC")  # 11:00 KST Thursday
    assert kr_end(mid_session) == pd.Timestamp("2026-08-05").date()

    after_close = pd.Timestamp("2026-08-06 07:00", tz="UTC")  # 16:00 KST
    assert kr_end(after_close) == pd.Timestamp("2026-08-06").date()


def test_kr_end_on_a_weekend_is_fridays_session():
    from scripts.collect_daily import kr_end

    sunday = pd.Timestamp("2026-08-09 03:00", tz="UTC")
    assert kr_end(sunday) == pd.Timestamp("2026-08-07").date()


def test_kr_end_and_the_renderer_agree():
    """These were written separately and only the collector got the guard. On
    2026-08-06 the renderer asked for a session that had not opened and
    published thirty-one empty ratings; they now share one implementation."""
    from scripts.collect_daily import kr_end
    from src.report.render import resolve_run

    for stamp in ("2026-08-06 02:00", "2026-08-06 12:37", "2026-08-06 15:00", "2026-08-09 03:00"):
        at = pd.Timestamp(stamp, tz="UTC")
        assert kr_end(at) == resolve_run("evening", at)[0], stamp


# --- the status handoff ----------------------------------------------------


def test_write_status_roundtrips(tmp_path, monkeypatch):
    import scripts.collect_daily as mod

    monkeypatch.setattr(mod, "STATUS", tmp_path)
    outcomes = {
        "kr_news": {
            "ok": False,
            "detail": "x",
            "summary": "kr_news: 1/5 checks FAILED",
            "failures": [{"name": "feed_continuity", "detail": "etnews_economy lost 5.3h"}],
        }
    }
    path = write_status("evening", pd.Timestamp("2026-08-06 12:37", tz="UTC"), outcomes)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run"] == "evening"
    assert payload["collectors"]["kr_news"]["failures"][0]["name"] == "feed_continuity"
