"""The vendor-neutral model layer. SPEC §7.1.

**This is the only file in the repository allowed to import a vendor SDK**
(CLAUDE.md rule 4). Everything else names a stage — ``scoring``, ``synthesis``
— and the provider comes from ``config/models.yaml``. Swapping vendors is a
change to one string in that file and nothing else.

`litellm` is the vendor layer, which SPEC §7.1 names as one of the two
acceptable shapes ("a unified `litellm` layer, or a thin custom wrapper
combined with per-vendor SDKs"). One dependency covers all three bake-off
candidates and, more usefully, it normalises the part that actually differs
between them: structured output. Anthropic, OpenAI and Gemini each express a
JSON schema differently, and litellm transforms one ``response_format`` into
each vendor's own form.

**Credentials are read from the environment by litellm itself**, under the
names each provider documents — ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``,
``GEMINI_API_KEY``. This module never reads or logs a key, per API-KEYS.md's
handling rules; it only reports whether one is present, so a missing key
produces an actionable message rather than a vendor stack trace.

**Determinism is the caller's, not this layer's.** ``temperature`` comes from
``config/models.yaml`` — 0 for scoring (SPEC §6.3), 0.3 for synthesis, which is
prose and deliberately outside the evaluation path (PREREGISTRATION §8.4).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from src.util.config import load_models

# Which environment variable each provider's credentials live under. These are
# litellm's own names, not this project's choice — the adapter exists to match
# them rather than invent a parallel set. API-KEYS.md §8 documents issuance
# against exactly this table.
CREDENTIALS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

# litellm routes on a `provider/model` string. OpenAI is the default route and
# takes a bare model name; the others are prefixed.
_ROUTE_PREFIX = {"anthropic": "anthropic/", "openai": "", "gemini": "gemini/"}


class AdapterError(RuntimeError):
    """A model call that cannot be used, with the reason a caller can act on."""


class CredentialError(AdapterError):
    """No API key present for the requested provider."""


class SchemaError(AdapterError):
    """The model answered, but not with the schema that was asked for.

    Kept distinct from the other failures because SPEC §7.4 measures it: the
    "schema compliance rate" metric is the share of calls that avoid this.
    """


@dataclass(frozen=True)
class Completion:
    """One model call's result and what it cost to get.

    ``model_id`` and ``prompt_version`` are carried because SPEC §6.2 puts them
    in the stored score record — a number is not comparable to another number
    without knowing which model and which prompt produced it.
    """

    parsed: dict[str, Any]
    model_id: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_s: float


def route(provider: str, model: str) -> str:
    """The model string litellm routes on."""
    if provider not in _ROUTE_PREFIX:
        raise AdapterError(
            f"unknown provider {provider!r}; config/models.yaml must name one of "
            f"{sorted(_ROUTE_PREFIX)}"
        )
    return f"{_ROUTE_PREFIX[provider]}{model}"


def missing_credential(provider: str) -> str | None:
    """The environment variable this provider needs, when it is not set.

    Presence only — the value is never read, returned or logged (API-KEYS.md).
    """
    name = CREDENTIALS.get(provider)
    if name is None:
        raise AdapterError(f"unknown provider {provider!r}")
    return None if os.environ.get(name) else name


def _call(**kwargs: Any) -> Any:
    """The single point where a vendor SDK is invoked.

    Imported inside the function rather than at module scope so that importing
    this module — which the bake-off does to read `CREDENTIALS` and `route`
    before it has decided to call anything — does not require the SDK to be
    installed, and so that tests can replace this one function.
    """
    from litellm import completion

    return completion(**kwargs)


def _cost(response: Any) -> float | None:
    """USD for one call, or ``None`` when litellm has no price for the model.

    A missing price is reported rather than guessed. SPEC §7.4 decides the
    bake-off on cost per valid signal, and a fabricated number there would pick
    the model.
    """
    try:
        from litellm import completion_cost

        return float(completion_cost(completion_response=response))
    except Exception:  # noqa: BLE001 — litellm raises bare exceptions for unpriced models
        return None


def complete(
    stage: str,
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    prompt_version: str,
    model: str | None = None,
    provider: str | None = None,
    models: dict[str, Any] | None = None,
) -> Completion:
    """Run one structured call for ``stage`` and return the parsed JSON.

    ``model``/``provider`` override ``config/models.yaml`` so the bake-off can
    drive the same stage through each candidate without editing config — the
    candidates are alternatives under test, not the configured choice.

    Raises :class:`CredentialError` before making any call when the provider's
    key is absent, and :class:`SchemaError` when the answer does not parse or
    does not match ``schema``. Both are separated from transport failures so
    SPEC §7.4's compliance rate measures the model rather than the network.
    """
    config = models if models is not None else load_models()
    if stage not in config:
        raise AdapterError(f"config/models.yaml has no stage {stage!r}")
    settings = config[stage]

    provider = provider or settings["provider"]
    model = model or settings["model"]

    absent = missing_credential(provider)
    if absent:
        raise CredentialError(
            f"{provider} needs {absent} in the environment. See API-KEYS.md §8; "
            f"the value is never read by this process beyond presence."
        )

    request: dict[str, Any] = {
        "model": route(provider, model),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_schema", "json_schema": schema, "strict": True},
    }
    if "temperature" in settings:
        request["temperature"] = settings["temperature"]

    started = time.perf_counter()
    response = _call(**request)
    latency = time.perf_counter() - started

    content = response.choices[0].message.content
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"{model} returned unparseable content: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SchemaError(f"{model} returned {type(parsed).__name__}, expected an object")

    usage = response.usage
    return Completion(
        parsed=parsed,
        model_id=model,
        prompt_version=prompt_version,
        input_tokens=int(usage.prompt_tokens),
        output_tokens=int(usage.completion_tokens),
        cost_usd=_cost(response),
        latency_s=latency,
    )
