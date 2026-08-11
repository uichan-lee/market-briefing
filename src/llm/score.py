"""Stage 2 scoring: one article, one ticker, five numbers. SPEC §6.2.

Holds the prompt loader, the JSON schema the model is held to, and the call
that turns an article into a score record. The vendor never appears here — it
is reached through :mod:`src.llm.adapter`, per CLAUDE.md rule 4.

**The dimension bounds are imported from the golden-set rubric, not restated.**
`scripts/golden.py`'s ``DIMENSIONS`` is what Ricky labelled against and what
SPEC §7.4 measures a model against; a second copy of those numbers here would
be a place for the two to drift apart silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.golden import DIMENSIONS
from src.llm.adapter import Completion, complete

PROMPTS = Path(__file__).parent / "prompts"

# Ricky labelled from a view that truncated the body at this width
# (`scripts.golden.format_article`). The model sees the same, or the golden-set
# correlation compares two different readings of the same article.
BODY_CHARS = 400


class PromptError(RuntimeError):
    """A prompt file that does not have the sections the loader needs."""


@dataclass(frozen=True)
class Prompt:
    """One versioned prompt, split into its system and user halves."""

    version: str
    system: str
    user_template: str


def load_prompt(version: str = "v1", *, kind: str = "scoring") -> Prompt:
    """Read ``prompts/{version}_{kind}.md`` and split it on its two headings.

    Prompts live as files rather than string literals (SPEC §6.3) so that a
    diff of the prompt is reviewable and a version can be pinned to the scores
    it produced.
    """
    path = PROMPTS / f"{version}_{kind}.md"
    if not path.exists():
        raise PromptError(f"no prompt at {path}")
    text = path.read_text(encoding="utf-8")

    sections = re.split(r"^## (System|User message)\s*$", text, flags=re.MULTILINE)
    found = dict(zip(sections[1::2], sections[2::2], strict=True))
    missing = {"System", "User message"} - found.keys()
    if missing:
        raise PromptError(f"{path.name} is missing section(s) {sorted(missing)}")
    return Prompt(version, found["System"].strip(), found["User message"].strip())


def schema() -> dict[str, Any]:
    """The JSON schema every scoring call is held to.

    Built from ``DIMENSIONS`` so the bounds the model is given are the bounds
    the labels were written under. ``strict`` schemas forbid extra keys, which
    is what makes SPEC §7.4's compliance rate a real measurement rather than a
    lenient parse.
    """
    properties: dict[str, Any] = {
        name: {"type": "number", "minimum": low, "maximum": high, "description": hint}
        for name, low, high, hint, _ in DIMENSIONS
    }
    properties["rationale"] = {"type": "string", "maxLength": 40}
    return {
        "name": "article_score",
        "schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
        "strict": True,
    }


def render_user(prompt: Prompt, article: dict[str, Any]) -> str:
    """Fill the user half of the prompt with one article."""
    return (
        prompt.user_template.replace("{ticker}", str(article.get("ticker", "")))
        .replace("{name}", str(article.get("name", "")))
        .replace("{title}", str(article.get("title", "")))
        .replace("{description}", str(article.get("description", ""))[:BODY_CHARS])
    )


def out_of_range(parsed: dict[str, Any]) -> list[str]:
    """Dimensions whose value sits outside its declared bounds.

    The schema already forbids these, so a non-empty result means the vendor
    did not enforce what it was given — worth measuring rather than trusting.
    """
    bad = []
    for name, low, high, _, _ in DIMENSIONS:
        value = parsed.get(name)
        if not isinstance(value, int | float) or not (low <= float(value) <= high):
            bad.append(name)
    return bad


def score_article(
    article: dict[str, Any],
    *,
    prompt: Prompt,
    model: str | None = None,
    provider: str | None = None,
    models: dict[str, Any] | None = None,
) -> Completion:
    """Score one ``(article, ticker)`` pair through the configured scoring model."""
    return complete(
        "scoring",
        system=prompt.system,
        user=render_user(prompt, article),
        schema=schema(),
        prompt_version=prompt.version,
        model=model,
        provider=provider,
        models=models,
    )
