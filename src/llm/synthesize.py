"""Stage 3: report synthesis and red-team. SPEC §2.2⑤, §2.2⑧, §12 steps 6-8.

Both sections are one structured call each against the ``synthesis`` stage
(``config/models.yaml``), and both are prose — CLAUDE.md rule 3 permits this
only because ``src/report/consistency.py`` checks the output before
``src/report/render.py`` publishes it. See CLAUDE.md's "commentary and
red-team exceptions, in full".

Neither function here swallows a failure — a missing credential or a bad
response propagates as :class:`~src.llm.adapter.AdapterError`. Degrading to a
stated per-run absence on failure is the caller's job
(:func:`src.report.render._llm_section`), so that a direct caller (e.g. a
bake-off or manual verification script) sees the real error.
"""

from __future__ import annotations

from typing import Any

from src.llm.adapter import Completion, complete
from src.llm.score import Prompt, PromptError, load_prompt

__all__ = ["Prompt", "PromptError", "load_prompt", "run_redteam", "run_synthesis"]


def _prose_schema(key: str) -> dict[str, Any]:
    """The JSON schema every §2.2⑤/⑧ call is held to: one required string field.

    :func:`~src.llm.adapter.complete` has no free-text path — even prose
    output must be wrapped in structured output. ``key`` names the field so
    the raw JSON a debugging session sees says what it is.
    """
    return {
        "name": key,
        "schema": {
            "type": "object",
            "properties": {key: {"type": "string"}},
            "required": [key],
            "additionalProperties": False,
        },
        "strict": True,
    }


def run_synthesis(
    sections: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    models: dict[str, Any] | None = None,
) -> str:
    """SPEC §2.2⑧. ``sections`` is the already-rendered header + ①⑨②③④⑥, joined.

    ⑤'s output is deliberately not part of ``sections`` — feeding one LLM's
    prose into another as input would compound hallucination risk and break
    the "reads the rendered deterministic sections" property CLAUDE.md names
    for ⑧.
    """
    prompt = load_prompt("v1", kind="synthesis")
    completion: Completion = complete(
        "synthesis",
        system=prompt.system,
        user=f"{prompt.user_template}\n\n---\n\n{sections}",
        schema=_prose_schema("commentary"),
        prompt_version=prompt.version,
        model=model,
        provider=provider,
        models=models,
    )
    return str(completion.parsed["commentary"]).strip()


def run_redteam(
    sections: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    models: dict[str, Any] | None = None,
) -> str:
    """SPEC §2.2⑤. ``sections`` is ①②③④ only — never ⑥, never ⑨.

    The model is deliberately never shown §2.2⑥'s ratings (see
    ``src/llm/prompts/v1_redteam.md``'s System section) — it argues against
    ①-④'s conclusions on their own evidence, per SPEC's literal
    "counterarguments only against the conclusions from ①-④".
    """
    prompt = load_prompt("v1", kind="redteam")
    completion: Completion = complete(
        "synthesis",
        system=prompt.system,
        user=f"{prompt.user_template}\n\n---\n\n{sections}",
        schema=_prose_schema("redteam"),
        prompt_version=prompt.version,
        model=model,
        provider=provider,
        models=models,
    )
    return str(completion.parsed["redteam"]).strip()
