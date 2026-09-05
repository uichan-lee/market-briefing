"""Tests for Stage 3: report synthesis and red-team. SPEC §2.2⑤, §2.2⑧.

Offline tests mirror tests/test_adapter.py's pattern: `_call` is the single
seam where a vendor SDK is invoked, so replacing it exercises every path
without contacting a vendor.
"""

from __future__ import annotations

import json

import pytest

from src.llm import adapter
from src.llm.adapter import CREDENTIALS, CredentialError
from src.llm.score import PromptError
from src.llm.synthesize import SynthesisDisabledError, load_prompt, run_redteam, run_synthesis

MODELS = {
    "synthesis": {"provider": "anthropic", "model": "claude-sonnet-5", "temperature": 0.3},
}


class _Response:
    """The shape litellm returns, reduced to what the adapter reads."""

    def __init__(self, content: str, *, prompt_tokens: int = 200, completion_tokens: int = 80):
        message = type("M", (), {"content": content})()
        self.choices = [type("C", (), {"message": message})()]
        self.usage = type(
            "U", (), {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
        )()


@pytest.fixture
def keyed(monkeypatch):
    for name in CREDENTIALS.values():
        monkeypatch.setenv(name, "test-key-not-real")


def _answer(monkeypatch, content: str, captured: dict | None = None):
    def fake(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return _Response(content)

    monkeypatch.setattr(adapter, "_call", fake)
    monkeypatch.setattr(adapter, "_cost", lambda response: None)


# --- run_synthesis / run_redteam -------------------------------------------


def test_run_synthesis_sends_the_rendered_sections_and_returns_the_commentary(monkeypatch, keyed):
    captured: dict = {}
    _answer(monkeypatch, json.dumps({"commentary": "**오늘의 한 줄:** 테스트"}), captured)

    result = run_synthesis("## ① 미국 → 한국 전이\n\n일부 렌더된 섹션", models=MODELS)

    assert result == "**오늘의 한 줄:** 테스트"
    assert "일부 렌더된 섹션" in captured["messages"][1]["content"]
    # The prompt's own preamble is sent too, not just the raw sections.
    assert "요약" in captured["messages"][1]["content"] or len(captured["messages"][1]["content"])


def test_run_redteam_sends_the_rendered_sections_and_returns_the_redteam_field(monkeypatch, keyed):
    captured: dict = {}
    _answer(monkeypatch, json.dumps({"redteam": "- **테스트** — 근거"}), captured)

    result = run_redteam("## ① 미국 → 한국 전이\n\n일부 렌더된 섹션", models=MODELS)

    assert result == "- **테스트** — 근거"
    assert "일부 렌더된 섹션" in captured["messages"][1]["content"]


def test_both_functions_route_through_the_synthesis_stage(monkeypatch, keyed):
    captured: dict = {}
    _answer(monkeypatch, json.dumps({"commentary": "x"}), captured)
    run_synthesis("s", models=MODELS)
    assert captured["model"] == "anthropic/claude-sonnet-5"
    assert captured["temperature"] == 0.3
    captured.clear()
    _answer(monkeypatch, json.dumps({"redteam": "x"}), captured)
    run_redteam("s", models=MODELS)
    assert captured["model"] == "anthropic/claude-sonnet-5"


def test_disabled_synthesis_never_calls_a_vendor(monkeypatch):
    models = {"synthesis": {"enabled": False, "provider": "anthropic", "model": "claude-sonnet-5"}}

    def forbidden(**kwargs):
        raise AssertionError(f"vendor call must not happen: {kwargs}")

    monkeypatch.setattr(adapter, "_call", forbidden)
    with pytest.raises(SynthesisDisabledError, match="비용 절약"):
        run_synthesis("rendered", models=models)
    with pytest.raises(SynthesisDisabledError, match="비용 절약"):
        run_redteam("rendered", models=models)


def test_a_credential_error_propagates_uncaught(monkeypatch):
    """Neither function swallows a failure — degrading to a stated absence on
    failure is render._llm_section's job, not this module's."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(CredentialError):
        run_synthesis("s", models=MODELS)
    with pytest.raises(CredentialError):
        run_redteam("s", models=MODELS)


def test_the_schema_forbids_extra_fields(monkeypatch, keyed):
    captured: dict = {}
    _answer(monkeypatch, json.dumps({"commentary": "x"}), captured)
    run_synthesis("s", models=MODELS)
    schema = captured["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["commentary"]
    assert schema["additionalProperties"] is False


# --- v1_redteam.md -----------------------------------------------------


def test_v1_redteam_prompt_loads_and_has_both_sections():
    prompt = load_prompt("v1", kind="redteam")
    assert prompt.system
    assert prompt.user_template


def test_v1_redteam_forbids_the_seven_reserved_labels_in_its_system_prompt():
    prompt = load_prompt("v1", kind="redteam")
    for label in ("강한 매수", "매수", "약한 매수", "관망", "약한 매도", "매도", "강한 매도"):
        assert label in prompt.system


def test_v1_redteam_user_template_documents_that_six_and_nine_are_excluded():
    """The model must not be shown §2.2⑥'s ratings or §2.2⑨'s regime — checked
    for real at the render.py level (test_render.py's redteam-input tests,
    where the actual rendered sections are assembled). This just confirms the
    prompt's own text states the exclusion, so a future edit that quietly adds
    ⑥/⑨ back has to also delete a sentence that says they're deliberately
    missing."""
    prompt = load_prompt("v1", kind="redteam")
    assert "①" in prompt.user_template
    assert "not" in prompt.user_template.lower()
    assert "⑥" in prompt.user_template  # names what's excluded, doesn't include it


def test_a_prompt_missing_a_section_is_rejected(tmp_path, monkeypatch):
    import src.llm.score as score

    monkeypatch.setattr(score, "PROMPTS", tmp_path)
    (tmp_path / "v9_redteam.md").write_text("## System\nonly this\n", encoding="utf-8")

    with pytest.raises(PromptError, match="User message"):
        load_prompt("v9", kind="redteam")


# --- live, manual verification only -----------------------------------


@pytest.mark.network
def test_live_synthesis_and_redteam_pass_consistency_against_real_output():
    """Not run by default. Requires ANTHROPIC_API_KEY. Confirms the real
    v1_synthesis / v1_redteam prompts, against a real model, produce output
    src.report.consistency.check_commentary() accepts — the one thing no
    offline test can prove, since every offline test injects a fixed string.

    Run explicitly: uv run pytest tests/test_synthesize.py -m network -v
    """
    import datetime as dt

    import pandas as pd

    from src.report.consistency import check_commentary
    from src.report.render import (
        ReportInputs,
        rate_all,
        render_calendar,
        render_header,
        render_news,
        render_ratings,
        render_regime,
        render_scan,
        render_transmission,
    )
    from src.util.config import AliasEntry, WatchlistEntry

    # Built through the real render_* functions, not a hand-written fixture —
    # this is the one test that must see exactly the input shape production
    # sends, including a real §2.2⑥ rating table. A stripped-down fixture
    # missing ⑥ entirely was tried first and failed: the model correctly
    # narrated the *absence* of a rating using reserved-vocabulary words
    # ("등급표가 없어... 강한 매수로 이어지는지"), which check_commentary()
    # rightly read as a contradiction. That was a test-fixture bug, not a
    # prompt bug — real ⑧/⑤ input always includes ⑥'s rendered text.
    day = dt.date(2026, 8, 25)
    source = ReportInputs(
        day=day,
        as_of=pd.Timestamp("2026-08-25 12:37", tz="UTC"),
        watchlist=[
            WatchlistEntry(
                ticker="005930", name="삼성전자", sector="반도체", held=False, market="KR"
            )
        ],
        features=pd.DataFrame(
            [
                {
                    "date": pd.Timestamp(day),
                    "ticker": "005930",
                    "foreign_flow_5d": 2.0,
                    "foreign_flow_5d_z": 2.0,
                    "inst_flow_5d": 2.0,
                    "inst_flow_5d_z": 2.0,
                    "rel_strength_20d": 2.0,
                    "rel_strength_20d_z": 2.0,
                    "short_ratio": 2.0,
                    "short_ratio_z": 2.0,
                    "valuation_band": 2.0,
                    "valuation_band_z": 2.0,
                }
            ]
        ),
        rating_config={
            "weights": {
                "foreign_flow_5d": 0.30,
                "inst_flow_5d": 0.15,
                "rel_strength_20d": 0.15,
                "short_ratio": -0.10,
                "valuation_band": 0.05,
            },
            "deferred_weights": {"news_polarity": 0.20},
            "cut_points": {"strong": 2.0, "moderate": 1.0, "weak": 0.4},
            "confidence": {"min_weight_coverage": 0.5, "max_rationale_terms": 4},
        },
        aliases={
            "005930": AliasEntry(
                ticker="005930",
                canonical="삼성전자",
                aliases=("삼성전자",),
                exclude=(),
                ambiguous_parents=(),
            )
        },
    )
    results = rate_all(source)
    assert results["005930"].rating.value == "매수", (
        "test fixture drifted from the rating it expects"
    )

    header = render_header(source)
    ratings_section = render_ratings(source, results)
    synthesis_input = "\n".join(
        [
            header,
            render_transmission(source),
            render_regime(source),
            render_scan(source),
            render_news(source),
            render_calendar(source),
            ratings_section,
        ]
    )
    redteam_input = "\n".join(
        [
            render_transmission(source),
            render_scan(source),
            render_news(source),
            render_calendar(source),
        ]
    )

    commentary = run_synthesis(synthesis_input)
    report = check_commentary(commentary, results, source.aliases)
    assert report.ok, report.summary()

    redteam = run_redteam(redteam_input)
    report = check_commentary(redteam, results, source.aliases)
    assert report.ok, report.summary()
