"""Tests for the vendor-neutral adapter and the scoring call. SPEC §7.1, §6.2.

No vendor is contacted. `_call` is the single seam where a vendor SDK is
invoked, so replacing it is enough to exercise every path — which is itself the
property CLAUDE.md rule 4 asks for.
"""

from __future__ import annotations

import json

import pytest

from src.llm import adapter, score
from src.llm.adapter import (
    CREDENTIALS,
    AdapterError,
    CredentialError,
    SchemaError,
    complete,
    missing_credential,
    route,
)

MODELS = {
    "scoring": {"provider": "anthropic", "model": "claude-sonnet-5", "temperature": 0},
    "synthesis": {"provider": "openai", "model": "gpt-5", "temperature": 0.3},
}

GOOD = {
    "relevance": 0.7,
    "polarity": 0.5,
    "intensity": 0.6,
    "uncertainty": 0.3,
    "forwardness": 0.8,
    "rationale": "수주 공시",
}


class _Response:
    """The shape litellm returns, reduced to what the adapter reads."""

    def __init__(self, content: str, *, prompt_tokens: int = 100, completion_tokens: int = 20):
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
    monkeypatch.setattr(adapter, "_cost", lambda response: 0.0012)


# --- routing and credentials ----------------------------------------------


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("anthropic", "claude-sonnet-5", "anthropic/claude-sonnet-5"),
        ("gemini", "gemini-3-pro", "gemini/gemini-3-pro"),
        ("openai", "gpt-5", "gpt-5"),  # litellm's default route takes a bare name
    ],
)
def test_the_provider_string_is_the_only_thing_that_selects_a_vendor(provider, model, expected):
    assert route(provider, model) == expected


def test_an_unknown_provider_names_the_ones_that_exist():
    with pytest.raises(AdapterError, match="anthropic"):
        route("hyperclova", "x")


def test_a_missing_key_is_reported_by_name_and_never_by_value(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert missing_credential("anthropic") == "ANTHROPIC_API_KEY"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    assert missing_credential("anthropic") is None


def test_a_missing_key_fails_before_any_call_is_made(monkeypatch):
    """The bake-off runs 1,500 calls; discovering a missing key on call 1,499
    wastes the other 1,498."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def explode(**kwargs):
        raise AssertionError("a call was made without a credential")

    monkeypatch.setattr(adapter, "_call", explode)

    with pytest.raises(CredentialError, match="ANTHROPIC_API_KEY"):
        complete(
            "scoring",
            system="s",
            user="u",
            schema=score.schema(),
            prompt_version="v1",
            models=MODELS,
        )


def test_the_error_does_not_carry_the_key_value(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        complete("scoring", system="s", user="u", schema={}, prompt_version="v1", models=MODELS)
    except CredentialError as exc:
        assert "sk-" not in str(exc)


# --- the request the adapter builds ---------------------------------------


def test_temperature_comes_from_config_not_from_code(monkeypatch, keyed):
    captured: dict = {}
    _answer(monkeypatch, json.dumps(GOOD), captured)

    complete(
        "scoring", system="s", user="u", schema=score.schema(), prompt_version="v1", models=MODELS
    )
    assert captured["temperature"] == 0

    complete(
        "synthesis", system="s", user="u", schema=score.schema(), prompt_version="v1", models=MODELS
    )
    assert captured["temperature"] == 0.3


def test_the_schema_is_sent_as_a_strict_json_schema(monkeypatch, keyed):
    captured: dict = {}
    _answer(monkeypatch, json.dumps(GOOD), captured)

    complete(
        "scoring", system="s", user="u", schema=score.schema(), prompt_version="v1", models=MODELS
    )
    sent = captured["response_format"]
    assert sent["type"] == "json_schema"
    assert sent["strict"] is True
    assert sent["json_schema"]["schema"]["additionalProperties"] is False


def test_a_candidate_model_overrides_config_without_editing_it(monkeypatch, keyed):
    """The bake-off drives one stage through three models. They are alternatives
    under test, not the configured choice, so config must not move."""
    captured: dict = {}
    _answer(monkeypatch, json.dumps(GOOD), captured)

    complete(
        "scoring",
        system="s",
        user="u",
        schema=score.schema(),
        prompt_version="v1",
        model="gemini-3-pro",
        provider="gemini",
        models=MODELS,
    )
    assert captured["model"] == "gemini/gemini-3-pro"
    assert MODELS["scoring"]["model"] == "claude-sonnet-5"


# --- what comes back -------------------------------------------------------


def test_a_good_answer_carries_its_provenance_and_its_cost(monkeypatch, keyed):
    _answer(monkeypatch, json.dumps(GOOD))

    result = complete(
        "scoring", system="s", user="u", schema=score.schema(), prompt_version="v1", models=MODELS
    )
    assert result.parsed == GOOD
    assert (result.model_id, result.prompt_version) == ("claude-sonnet-5", "v1")
    assert (result.input_tokens, result.output_tokens) == (100, 20)
    assert result.cost_usd == 0.0012
    assert result.latency_s >= 0


def test_unparseable_content_is_a_schema_failure_not_a_crash(monkeypatch, keyed):
    """SPEC §7.4 measures the share of calls that return valid JSON, so this
    has to be a distinguishable outcome rather than an exception from json."""
    _answer(monkeypatch, "여기 점수입니다: {relevance: 0.7")

    with pytest.raises(SchemaError, match="unparseable"):
        complete(
            "scoring",
            system="s",
            user="u",
            schema=score.schema(),
            prompt_version="v1",
            models=MODELS,
        )


def test_valid_json_that_is_not_an_object_is_also_a_schema_failure(monkeypatch, keyed):
    _answer(monkeypatch, "[0.7, 0.5]")

    with pytest.raises(SchemaError, match="expected an object"):
        complete(
            "scoring",
            system="s",
            user="u",
            schema=score.schema(),
            prompt_version="v1",
            models=MODELS,
        )


def test_an_unpriced_model_reports_no_cost_rather_than_guessing():
    """SPEC §7.4 decides the bake-off on cost per valid signal. A fabricated
    price there would pick the model, so an unpriced model reports None and the
    bake-off can say the number is missing."""
    unpriced = _Response(json.dumps(GOOD))
    unpriced.model = "a-model-litellm-has-never-heard-of"
    assert adapter._cost(unpriced) is None


def test_a_priced_model_gets_a_real_number_from_litellm():
    """Guards the assumption the line above rests on — that `completion_cost`
    returns a price for a model litellm knows, so `None` means "unpriced"
    rather than "always broken". Without this, `_cost`'s except branch would
    swallow a genuine breakage and every bake-off row would read "no price".

    This is the one test that builds a real litellm response object. CLAUDE.md
    rule 4 confines the SDK to `src/llm/adapter.py`; the rule is about the
    pipeline staying model-agnostic, and a fixture that proves the adapter's
    contract against the real type is what keeps that adapter honest. No
    network: pricing is a local table lookup.
    """
    from litellm.types.utils import Choices, Message, ModelResponse, Usage

    priced = ModelResponse(
        id="x",
        model="gpt-4o-mini",
        object="chat.completion",
        created=0,
        choices=[
            Choices(index=0, message=Message(role="assistant", content="{}"), finish_reason="stop")
        ],
    )
    priced.usage = Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120)

    cost = adapter._cost(priced)
    assert cost is not None and cost > 0


def test_an_unknown_stage_is_rejected(monkeypatch, keyed):
    with pytest.raises(AdapterError, match="redteam"):
        complete("redteam", system="s", user="u", schema={}, prompt_version="v1", models=MODELS)


# --- the scoring schema is the golden set's rubric -------------------------


def test_the_schema_bounds_are_the_bounds_the_labels_were_written_under():
    """If these drift from scripts/golden.py's DIMENSIONS, the bake-off measures
    the gap between two rubrics rather than the model."""
    from scripts.golden import DIMENSIONS

    properties = score.schema()["schema"]["properties"]
    for name, low, high, _, _ in DIMENSIONS:
        assert properties[name]["minimum"] == low, name
        assert properties[name]["maximum"] == high, name
    assert properties["polarity"]["minimum"] == -1.0


def test_the_body_is_truncated_to_what_ricky_saw():
    prompt = score.load_prompt()
    rendered = score.render_user(prompt, {"ticker": "005930", "description": "가" * 900})
    assert "가" * score.BODY_CHARS in rendered
    assert "가" * (score.BODY_CHARS + 1) not in rendered


def test_notes_above_the_user_heading_are_not_sent_to_the_model():
    """The prompt file carries editorial notes. They document the prompt; they
    are not part of it."""
    prompt = score.load_prompt()
    assert "truncated at 400 characters" not in prompt.user_template
    assert "{title}" in prompt.user_template


def test_a_prompt_missing_a_section_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(score, "PROMPTS", tmp_path)
    (tmp_path / "v9_scoring.md").write_text("## System\nonly this\n", encoding="utf-8")

    with pytest.raises(score.PromptError, match="User message"):
        score.load_prompt("v9")


def test_out_of_range_values_are_detectable(monkeypatch, keyed):
    """The strict schema forbids these. A non-empty result means the vendor did
    not enforce what it was given, which is worth measuring rather than
    trusting."""
    from scripts.golden import DIMENSIONS

    assert score.out_of_range(GOOD) == []
    assert score.out_of_range({**GOOD, "polarity": -1.4}) == ["polarity"]
    assert score.out_of_range({**GOOD, "relevance": "높음"}) == ["relevance"]
    assert set(score.out_of_range({})) == {name for name, *_ in DIMENSIONS}
