"""Synthetic availability and immutable-publication regressions."""

import datetime as dt
import gzip
import json

import pandas as pd
import pytest

from scripts.collect_daily import write_daily
from src.features.compute import load_raw
from src.llm.daily_scoring import load_news_polarity_frame, write_scores
from src.report.publication import replay_publication, write_publication
from src.report.render import ReportInputs, load_inputs, rate_all, read_status
from src.util.config import WatchlistEntry, load_rating
from src.util.point_in_time import parse_clocks, unknown_clocks, visible_versions

DAY = dt.date(2026, 8, 3)
EARLY = pd.Timestamp("2026-08-03 08:00Z")
LATE = pd.Timestamp("2026-08-04 08:00Z")


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_clock_precision_and_offsets_preserve_cutoff(strict, reverse):
    clocks = [
        "2026-08-03T08:00:00+00:00",
        "2026-08-04T17:00:00.123456+09:00",
        "2026-08-04T08:00:00Z",
        "2026-08-04T04:00:00.123455-0400",
    ]
    frame = pd.DataFrame({"id": range(4), "clock": clocks})
    if reverse:
        frame = frame.iloc[::-1]
    boundary = LATE + pd.Timedelta(microseconds=123456)
    selected = visible_versions(frame, boundary, strict=strict, clocks=("clock",))
    assert set(selected.id) == {0, 2, 3}
    assert str(selected.clock.dtype) == "datetime64[ns, UTC]"
    assert not unknown_clocks(frame, ("clock",))
    assert (
        len(
            visible_versions(
                frame, boundary + pd.Timedelta(nanoseconds=1), strict=strict, clocks=("clock",)
            )
        )
        == 4
    )


@pytest.mark.parametrize(
    "clock",
    [
        "2026-08-03T07:00:00",
        pd.Timestamp("2026-08-03 07:00"),
        "bad",
        "2026-13-03T07:00:00Z",
        None,
        pd.NaT,
        123,
    ],
)
def test_unknown_clocks_are_disclosed_and_excluded_from_strict(clock):
    frame = pd.DataFrame({"clock": [clock]})
    assert unknown_clocks(frame, ("clock",))
    assert visible_versions(frame, LATE, strict=True, clocks=("clock",)).empty
    displayed = visible_versions(frame, LATE, clocks=("clock",))
    assert len(displayed) == 1 and displayed.clock.isna().all()
    assert unknown_clocks(displayed, ("clock",))


def test_typed_aware_clocks_keep_timezone_evidence():
    clocks = pd.Series([pd.Timestamp("2026-08-03 17:00+09:00"), pd.NaT])
    assert parse_clocks(clocks).iloc[0] == EARLY
    assert parse_clocks(clocks).isna().tolist() == [False, True]


def prices(close=100):
    return pd.DataFrame(
        {
            "date": [pd.Timestamp(DAY)],
            "ticker": ["005930"],
            "close": [close],
            "known_at_utc": [pd.Timestamp("2026-08-03 07:00Z")],
        }
    )


def test_revision_filter_precedes_key_selection_and_exact_cutoff(tmp_path):
    directory = tmp_path / "kr" / "price"
    write_daily("kr_price", prices(), directory=directory, ingested_at=EARLY)
    write_daily("kr_price", prices(200), directory=directory, ingested_at=LATE)
    before = load_raw(tmp_path, "kr/price", as_of=LATE, strict=True)
    assert before.close.tolist() == [100]
    assert load_raw(tmp_path, "kr/price", as_of=EARLY, strict=True).empty
    assert load_raw(
        tmp_path, "kr/price", as_of=LATE + pd.Timedelta(seconds=1), strict=True
    ).close.tolist() == [200]


def test_partial_refresh_retains_omissions_and_compares_accumulated_state(tmp_path):
    def batch(series, values):
        return pd.DataFrame(
            {
                "date": [pd.Timestamp(DAY)] * len(series),
                "series": series,
                "value": values,
                "known_at_utc": [EARLY] * len(series),
            }
        )

    write_daily("macro", batch(["vix", "wti"], [10.0, 80.0]), directory=tmp_path, ingested_at=EARLY)
    assert write_daily(
        "macro", batch(["vix", "usdkrw"], [20.0, 1300.0]), directory=tmp_path, ingested_at=LATE
    ) == (0, 1)
    loaded = load_raw(tmp_path.parent, tmp_path.name, key=("date", "series"))
    assert dict(zip(loaded.series, loaded.value, strict=True)) == {
        "vix": 20.0,
        "wti": 80.0,
        "usdkrw": 1300.0,
    }
    assert write_daily("macro", batch(["wti"], [80.0]), directory=tmp_path, ingested_at=LATE) == (
        0,
        0,
    )
    assert write_daily(
        "macro",
        batch(["vix"], [None]),
        directory=tmp_path,
        ingested_at=LATE + pd.Timedelta(seconds=1),
    ) == (0, 1)
    loaded = load_raw(tmp_path.parent, tmp_path.name, key=("date", "series"))
    assert pd.isna(loaded.loc[loaded.series == "vix", "value"].iloc[0])


def test_legacy_has_no_fabricated_historical_ingestion(tmp_path):
    directory = tmp_path / "kr" / "price"
    directory.mkdir(parents=True)
    prices().to_parquet(directory / f"{DAY}.parquet", index=False)
    evidence = []
    assert not load_raw(tmp_path, "kr/price", as_of=LATE, provenance=evidence).empty
    assert evidence[0]["legacy"]
    assert load_raw(tmp_path, "kr/price", as_of=LATE, strict=True).empty


def test_raw_clock_sorting_uses_utc_and_discloses_naive_evidence(tmp_path):
    directory = tmp_path / "kr" / "price"
    directory.mkdir(parents=True)
    # Local text order is opposite UTC order; both are strictly before LATE.
    for version, value, clock in (
        ("", 100, "2026-08-03T10:00:00+09:00"),
        ("-v2", 200, "2026-08-03T02:00:00.123456Z"),
    ):
        frame = prices(value).assign(ingested_at_utc=clock)
        frame.to_parquet(directory / f"{DAY}{version}.parquet", index=False)
    evidence = []
    assert load_raw(
        tmp_path, "kr/price", as_of=LATE, strict=True, provenance=evidence
    ).close.tolist() == [200]
    assert all(not item["legacy"] for item in evidence)
    prices(300).assign(ingested_at_utc="2026-08-03T03:00:00").to_parquet(
        directory / f"{DAY}-v3.parquet", index=False
    )
    evidence = []
    load_raw(tmp_path, "kr/price", as_of=LATE, provenance=evidence)
    assert evidence[-1]["legacy"]
    assert load_raw(tmp_path, "kr/price", as_of=LATE, strict=True).close.tolist() == [200]


@pytest.mark.parametrize("source", ["scores", "news"])
@pytest.mark.parametrize("clock", ["2026-08-03T08:00:00", "invalid"])
def test_score_and_news_provenance_discloses_unsupported_clocks(tmp_path, source, clock):
    news_dir = tmp_path / "raw" / "kr" / "news" / str(DAY)
    news_dir.mkdir(parents=True)
    article = {
        "article_id": "a1",
        "known_at_utc": EARLY.isoformat(),
        "collected_at_utc": clock if source == "news" else EARLY.isoformat(),
    }
    with gzip.open(news_dir / "0800.jsonl.gz", "wt") as handle:
        handle.write(json.dumps(article) + "\n")
    score_dir = tmp_path / "scores"
    score_dir.mkdir()
    row = {
        "article_id": "a1",
        "ticker": "005930",
        "model_id": "m",
        "prompt_version": "v1",
        "relevance": 1.0,
        "polarity": 0.2,
        "intensity": 0.5,
        "uncertainty": 0.1,
        "score_id": "s1",
        "score_completed_at_utc": clock if source == "scores" else EARLY.isoformat(),
        "score_archived_at_utc": EARLY.isoformat(),
    }
    (score_dir / "scores.jsonl").write_text(json.dumps(row) + "\n")
    evidence = []
    assert len(load_news_polarity_frame(tmp_path, as_of=LATE, provenance=evidence)) == 1
    assert next(item for item in evidence if item["source"] == source)["legacy"]
    assert load_news_polarity_frame(tmp_path, as_of=LATE, strict=True).empty


def test_missing_or_duplicate_keys_rejected_before_any_write(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        write_daily("kr_price", pd.concat([prices(), prices()]), directory=tmp_path)
    assert not list(tmp_path.glob("*.parquet"))


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("fractional_first", [False, True])
def test_late_score_of_old_article_excluded_before_dedup(
    tmp_path, monkeypatch, strict, fractional_first
):
    early = EARLY + pd.Timedelta(microseconds=123456) if fractional_first else EARLY
    late = LATE if fractional_first else LATE + pd.Timedelta(microseconds=123456)
    news = tmp_path / "raw" / "kr" / "news" / str(DAY)
    news.mkdir(parents=True)
    with gzip.open(news / "0800.jsonl.gz", "wt") as handle:
        handle.write(
            json.dumps(
                {
                    "article_id": "a1",
                    "known_at_utc": "2026-08-03T07:00:00Z",
                    "collected_at_utc": EARLY.isoformat(),
                    "title": "title",
                    "link": "link",
                }
            )
            + "\n"
        )
    row = {
        "article_id": "a1",
        "ticker": "005930",
        "model_id": "m",
        "prompt_version": "v1",
        "relevance": 1.0,
        "polarity": 0.2,
        "intensity": 0.5,
        "uncertainty": 0.1,
        "score_completed_at_utc": early.isoformat(),
    }
    monkeypatch.setattr("src.llm.daily_scoring.now_utc", lambda: early)
    write_scores(tmp_path, DAY, "m", "v1", [row])
    monkeypatch.setattr("src.llm.daily_scoring.now_utc", lambda: late)
    write_scores(
        tmp_path,
        DAY,
        "m",
        "v1",
        [{**row, "polarity": -0.8, "score_completed_at_utc": late.isoformat()}],
    )
    provenance = []
    assert load_news_polarity_frame(
        tmp_path, as_of=late, strict=strict, provenance=provenance
    ).polarity.tolist() == [0.2]
    assert all(not entry["legacy"] for entry in provenance)
    assert load_news_polarity_frame(tmp_path, as_of=EARLY, strict=True).empty
    assert load_news_polarity_frame(
        tmp_path, as_of=late + pd.Timedelta(nanoseconds=1), strict=strict
    ).polarity.tolist() == [-0.8]
    archived = json.loads((tmp_path / "scores" / f"{DAY}__m__v1.jsonl").read_text().splitlines()[0])
    assert archived["score_id"] and archived["prompt_sha256"]


def test_status_historical_clock_does_not_use_future_run(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    for name, at in (("old", EARLY), ("new", LATE)):
        (status / f"{name}.json").write_text(
            json.dumps(
                {"at": at.isoformat(), "collectors": {name: {"failures": [{"name": "schema"}]}}}
            )
        )
    assert read_status(tmp_path, as_of=EARLY + pd.Timedelta(minutes=1)) == (["old/schema"], [])
    assert read_status(tmp_path, as_of=LATE) == ([], [])  # exact exclusion; old status expired


def sample_inputs(tmp_path):
    values = {name + "_z": [1.0] for name in load_rating()["weights"]}
    feature = pd.DataFrame({"date": [pd.Timestamp(DAY)], "ticker": ["005930"], **values})
    return ReportInputs(
        day=DAY,
        as_of=LATE,
        root=tmp_path,
        watchlist=[WatchlistEntry("005930", "Samsung", "tech", False, "KR")],
        features=feature,
        rating_config=load_rating(),
        collector_failures=["synthetic warning"],
        publication_warnings=["synthetic prose warning"],
    )


def test_pinned_replay_survives_later_archives_and_never_calls_model(tmp_path, monkeypatch):
    import src.llm.adapter as adapter

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not call a model")

    monkeypatch.setattr(adapter, "complete", forbidden)
    inputs = sample_inputs(tmp_path)
    path = write_publication(inputs, rate_all(inputs), report="synthetic report", run="evening")
    initial = path.read_bytes()
    write_daily(
        "kr_price", prices(999), directory=tmp_path / "raw" / "kr" / "price", ingested_at=LATE
    )
    assert replay_publication(path)["verified"]
    assert path.read_bytes() == initial
    retry = write_publication(inputs, rate_all(inputs), report="synthetic report", run="evening")
    assert retry != path
    assert (
        len(list((tmp_path / "publications" / "blobs").glob("*.parquet"))) == 2
    )  # features + shared empty frame
    manifest = json.loads(path.read_text())
    observations = json.loads((path.parent / "blobs" / manifest["observations_blob"]).read_text())
    assert not observations[0]["research_eligible"]
    assert "missing_core_source_evidence" in observations[0]["ineligible_reasons"]
    assert "outcome" not in observations[0]


def test_corrupt_blob_and_changed_config_fail_replay(tmp_path, monkeypatch):
    inputs = sample_inputs(tmp_path)
    path = write_publication(inputs, rate_all(inputs), report="synthetic", run="evening")
    manifest = json.loads(path.read_text())
    blob = path.parent / "blobs" / manifest["report_blob"]
    blob.write_text("corrupt")
    with pytest.raises(ValueError, match="hash mismatch"):
        replay_publication(path)


def test_display_inputs_exclude_future_price_and_disclose_legacy(tmp_path):
    directory = tmp_path / "raw" / "us" / "price"
    directory.mkdir(parents=True)
    base = prices().assign(ticker="SPY")
    base.to_parquet(directory / f"{DAY}.parquet", index=False)
    future = base.assign(
        date=pd.Timestamp("2026-08-04"), close=200, known_at_utc=LATE + pd.Timedelta(days=1)
    )
    future.to_parquet(directory / "2026-08-04.parquet", index=False)
    inputs = load_inputs(DAY, root=tmp_path, as_of=LATE)
    assert inputs.us_prices.close.tolist() == [100]
    assert any("시점 증거 없음" in failure for failure in inputs.collector_failures)


def test_completion_before_cutoff_but_late_archive_is_excluded(tmp_path, monkeypatch):
    news = tmp_path / "raw" / "kr" / "news" / str(DAY)
    news.mkdir(parents=True)
    with gzip.open(news / "0800.jsonl.gz", "wt") as handle:
        handle.write(
            json.dumps(
                {
                    "article_id": "a1",
                    "known_at_utc": EARLY.isoformat(),
                    "collected_at_utc": EARLY.isoformat(),
                }
            )
            + "\n"
        )
    monkeypatch.setattr("src.llm.daily_scoring.now_utc", lambda: LATE)
    write_scores(
        tmp_path,
        DAY,
        "m",
        "v1",
        [
            {
                "article_id": "a1",
                "ticker": "005930",
                "model_id": "m",
                "prompt_version": "v1",
                "score_completed_at_utc": EARLY.isoformat(),
                "relevance": 1.0,
                "polarity": 1.0,
                "intensity": 1.0,
                "uncertainty": 0.0,
            }
        ],
    )
    assert load_news_polarity_frame(tmp_path, as_of=LATE, strict=True).empty


def test_shared_render_warnings_and_immutable_dispatch_receipt(tmp_path):
    from src.notify.base import DeliveryResult
    from src.report.publication import write_delivery_receipt
    from src.report.render import build_summary, build_summary_html

    inputs = sample_inputs(tmp_path)
    results = rate_all(inputs)
    assert "synthetic prose warning" in build_summary(inputs, results)
    assert "synthetic prose warning" in build_summary_html(inputs, results)
    path = write_publication(inputs, results, report="synthetic", run="evening")
    before = path.read_bytes()
    receipt = write_delivery_receipt(
        path,
        [DeliveryResult("vault", True, "synthetic"), DeliveryResult("email", False, "synthetic")],
    )
    data = json.loads(receipt.read_text())
    assert [row["delivered"] for row in data["channels"]] == [True, False]
    assert data["dispatch_completed_at_utc"] and not data["inbox_receipt_confirmed"]
    assert path.read_bytes() == before


def test_publication_archive_failure_still_delivers_shared_warning(tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module("src.report.render")
    from src.notify.base import DeliveryResult
    from src.report.render import render_header

    inputs = sample_inputs(tmp_path)
    monkeypatch.setattr(module, "load_inputs", lambda *args, **kwargs: inputs)
    monkeypatch.setattr(module, "write_ratings", lambda *args: None)
    monkeypatch.setattr(module, "load_rating_history", lambda *args: pd.DataFrame())
    monkeypatch.setattr(
        module,
        "render",
        lambda inputs, **kwargs: (
            render_header(inputs, extra_warnings=inputs.publication_warnings) + "synthetic body"
        ),
    )
    monkeypatch.setattr("src.util.config.load_delivery", lambda: {"channels": []})
    monkeypatch.setattr("src.notify.base.unavailable_channels", lambda _: [])

    def failed(*args, **kwargs):
        raise OSError("synthetic archive failure")

    monkeypatch.setattr("src.report.publication.write_publication", failed)
    sent = []

    def deliver(report, *args, **kwargs):
        sent.append((report, kwargs["summary"], kwargs["summary_html"]))
        return [DeliveryResult("vault", True, "synthetic")]

    monkeypatch.setattr("src.notify.base.deliver", deliver)
    assert module.main(["--day", str(DAY), "--data-root", str(tmp_path)]) == 0
    assert all("학습 기록 없음" in content for content in sent[0])
    assert not list((tmp_path / "publications").glob("*.json"))


def test_changed_code_identity_rejects_replay(tmp_path):
    inputs = sample_inputs(tmp_path)
    path = write_publication(inputs, rate_all(inputs), report="synthetic", run="evening")
    data = json.loads(path.read_text())
    data["code_config_identity"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="code/config changed"):
        replay_publication(path)


def test_direct_feature_inputs_also_respect_ingestion_clock():
    from src.features.compute import compute

    flow = pd.DataFrame(
        {
            "date": [pd.Timestamp(DAY)],
            "ticker": ["005930"],
            "foreign_net": [100.0],
            "inst_net": [100.0],
            "trading_value": [1000.0],
            "short_balance": [100.0],
            "shares_outstanding": [1000.0],
            "pbr": [1.0],
            "known_at_utc": [EARLY],
            "ingested_at_utc": [LATE],
        }
    )
    out = compute(
        flow, pd.DataFrame(), [WatchlistEntry("005930", "Samsung", "tech", False, "KR")], as_of=LATE
    )
    assert out.empty


@pytest.mark.parametrize(
    "at, eligible", [("2026-08-03T23:59:59Z", True), ("2026-08-04T00:00:00Z", False)]
)
def test_research_observation_must_be_recorded_before_next_open(
    tmp_path, monkeypatch, at, eligible
):
    from src.util.point_in_time import evidence

    inputs = sample_inputs(tmp_path)
    inputs.as_of = EARLY
    directory = tmp_path / "raw" / "kr" / "price"
    write_daily("kr_price", prices(), directory=directory, ingested_at=EARLY)
    source = evidence(directory / f"{DAY}.parquet")
    inputs.provenance = [
        {**source, "source": name, "legacy": False} for name in ("kr/price", "kr/investor_flow")
    ]
    monkeypatch.setattr("src.report.publication.now_utc", lambda: pd.Timestamp(at))
    path = write_publication(inputs, rate_all(inputs), report="synthetic", run="evening")
    manifest = json.loads(path.read_text())
    rows = json.loads((path.parent / "blobs" / manifest["observations_blob"]).read_text())
    assert rows[0]["research_eligible"] is eligible
    if not eligible:
        assert "not_recorded_before_next_open" in rows[0]["ineligible_reasons"]
