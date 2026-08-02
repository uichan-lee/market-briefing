"""Loading and validation for the YAML files under ``config/``.

These files are hand-maintained, and two of them — ``watchlist.yaml`` and
``aliases.yaml`` — are the ones CLAUDE.md singles out as able to corrupt every
downstream number silently. So loading is not just parsing: the checks below
turn the failure modes that are easy to introduce by hand into loud errors at
startup rather than quiet wrong numbers in a report three weeks later.

Two failures are worth naming explicitly:

**Unquoted tickers.** YAML reads a bare leading-zero integer as octal, so
``000660`` becomes ``432``. ``005930`` survives only because ``9`` is not a valid
octal digit, which makes the corruption inconsistent and easy to miss. Tickers
must be quoted strings, and non-strings are rejected rather than coerced.

**Alias collisions.** The same surface form listed under two tickers means every
article containing it is attributed to whichever entry happened to be iterated
first — silently, and differently as the file is edited. This is a hard error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

_KR_TICKER = re.compile(r"^\d{6}$")


class ConfigError(ValueError):
    """Raised for a malformed or internally inconsistent config file."""


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise ConfigError(f"{path} does not exist")
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _require_quoted_ticker(ticker: Any, source: str) -> str:
    if isinstance(ticker, int):
        raise ConfigError(
            f"{source}: ticker {ticker!r} was parsed as an integer, which means it was "
            f"left unquoted in YAML. A leading-zero code like 000660 becomes {ticker} "
            'under YAML\'s octal rule. Quote it: "000660".'
        )
    if not isinstance(ticker, str):
        raise ConfigError(f"{source}: ticker {ticker!r} is {type(ticker).__name__}, expected str")
    if not _KR_TICKER.match(ticker):
        raise ConfigError(f"{source}: ticker {ticker!r} is not a 6-digit Korean ticker code")
    return ticker


# --- watchlist -----------------------------------------------------------


@dataclass(frozen=True)
class WatchlistEntry:
    ticker: str
    name: str
    sector: str | None
    held: bool


def load_watchlist(path: Path | None = None) -> list[WatchlistEntry]:
    """Load ``config/watchlist.yaml``.

    An empty Korean watchlist is allowed — it is the file's committed starting
    state, and MANUAL-TASKS.md §2 leaves filling it to Ricky. Callers that
    require a populated list should say so themselves rather than have loading
    fail here.
    """
    path = path or CONFIG_DIR / "watchlist.yaml"
    raw = _read_yaml(path) or {}
    source = path.name

    entries: list[WatchlistEntry] = []
    seen: set[str] = set()

    for row in raw.get("kr") or []:
        if not isinstance(row, dict):
            raise ConfigError(f"{source}: expected a mapping per entry, got {row!r}")
        missing = {"ticker", "name"} - row.keys()
        if missing:
            raise ConfigError(f"{source}: entry {row!r} is missing {sorted(missing)}")

        ticker = _require_quoted_ticker(row["ticker"], source)
        if ticker in seen:
            raise ConfigError(f"{source}: ticker {ticker} listed twice")
        seen.add(ticker)

        entries.append(
            WatchlistEntry(
                ticker=ticker,
                name=str(row["name"]),
                sector=row.get("sector"),
                held=bool(row.get("held", False)),
            )
        )

    return entries


# --- aliases -------------------------------------------------------------


@dataclass(frozen=True)
class AliasEntry:
    ticker: str
    canonical: str
    aliases: tuple[str, ...]
    exclude: tuple[str, ...]
    ambiguous_parents: tuple[str, ...]


def load_aliases(path: Path | None = None) -> dict[str, AliasEntry]:
    """Load and validate ``config/aliases.yaml``.

    Beyond parsing, this enforces the invariants that make entity resolution
    trustworthy: no alias may identify two different tickers, and no alias may
    appear in its own entry's ``exclude`` list.
    """
    path = path or CONFIG_DIR / "aliases.yaml"
    raw = _read_yaml(path) or {}
    source = path.name

    entries: dict[str, AliasEntry] = {}
    # alias surface form -> the ticker that already claimed it
    claimed: dict[str, str] = {}

    for ticker_raw, body in raw.items():
        ticker = _require_quoted_ticker(ticker_raw, source)
        if not isinstance(body, dict):
            raise ConfigError(f"{source}: entry for {ticker} is not a mapping")
        if "canonical" not in body:
            raise ConfigError(f"{source}: entry for {ticker} is missing 'canonical'")

        aliases = tuple(str(a) for a in body.get("aliases") or ())
        exclude = tuple(str(a) for a in body.get("exclude") or ())
        parents = tuple(str(a) for a in body.get("ambiguous_parents") or ())

        if not aliases:
            raise ConfigError(f"{source}: {ticker} has no aliases; it would never match")

        self_conflict = set(aliases) & set(exclude)
        if self_conflict:
            raise ConfigError(
                f"{source}: {ticker} lists {sorted(self_conflict)} in both aliases and exclude"
            )

        for alias in aliases:
            owner = claimed.get(alias)
            if owner is not None and owner != ticker:
                raise ConfigError(
                    f"{source}: alias {alias!r} is claimed by both {owner} and {ticker}. "
                    "An alias that identifies two tickers silently misattributes every "
                    "article containing it — resolve it by hand."
                )
            claimed[alias] = ticker

        entries[ticker] = AliasEntry(
            ticker=ticker,
            canonical=str(body["canonical"]),
            aliases=aliases,
            exclude=exclude,
            ambiguous_parents=parents,
        )

    return entries


# --- plain passthrough configs -------------------------------------------


def load_delivery(path: Path | None = None) -> dict[str, Any]:
    """Load ``config/delivery.yaml``.

    CLAUDE.md forbids any delivery channel not declared in this file, so an
    unrecognized ``type`` is rejected here rather than at send time.
    """
    path = path or CONFIG_DIR / "delivery.yaml"
    raw = _read_yaml(path) or {}
    known = {"vault", "email", "webhook"}

    for channel in raw.get("channels") or []:
        if not isinstance(channel, dict) or "type" not in channel:
            raise ConfigError(f"{path.name}: every channel needs a 'type', got {channel!r}")
        if channel["type"] not in known:
            raise ConfigError(
                f"{path.name}: unknown channel type {channel['type']!r}; "
                f"expected one of {sorted(known)}"
            )
    return raw


def load_models(path: Path | None = None) -> dict[str, Any]:
    """Load ``config/models.yaml``. Stages must each name a provider and model."""
    path = path or CONFIG_DIR / "models.yaml"
    raw = _read_yaml(path) or {}

    for stage in ("embedding", "scoring", "synthesis"):
        if stage not in raw:
            raise ConfigError(f"{path.name}: missing stage {stage!r}")
        missing = {"provider", "model"} - raw[stage].keys()
        if missing:
            raise ConfigError(f"{path.name}: stage {stage!r} is missing {sorted(missing)}")
    return raw


def load_rating(path: Path | None = None) -> dict[str, Any]:
    """Load ``config/rating.yaml`` (SPEC §2.2⑥).

    Validates the shape the rating depends on. The cut points must be ordered
    and positive: the scale is symmetric by construction, so an out-of-order or
    negative cut point silently produces a rating scale that never reaches its
    outer buckets.
    """
    path = path or CONFIG_DIR / "rating.yaml"
    raw = _read_yaml(path) or {}

    weights = raw.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise ConfigError(f"{path.name}: 'weights' must be a non-empty mapping")
    for feature, weight in weights.items():
        if not isinstance(weight, int | float):
            raise ConfigError(f"{path.name}: weight for {feature!r} is not a number")

    cut_points = raw.get("cut_points")
    if not isinstance(cut_points, dict):
        raise ConfigError(f"{path.name}: 'cut_points' must be a mapping")
    missing = {"strong", "moderate", "weak"} - cut_points.keys()
    if missing:
        raise ConfigError(f"{path.name}: cut_points is missing {sorted(missing)}")
    if not (cut_points["strong"] > cut_points["moderate"] > cut_points["weak"] > 0):
        raise ConfigError(
            f"{path.name}: cut_points must satisfy strong > moderate > weak > 0, got {cut_points}"
        )

    coverage = (raw.get("confidence") or {}).get("min_weight_coverage", 0.0)
    if not 0.0 <= coverage <= 1.0:
        raise ConfigError(f"{path.name}: min_weight_coverage must be in [0, 1], got {coverage}")

    return raw


def load_sector_mapping(path: Path | None = None) -> list[dict[str, Any]]:
    """Load ``config/sector_mapping.yaml``."""
    path = path or CONFIG_DIR / "sector_mapping.yaml"
    raw = _read_yaml(path) or {}

    mappings = raw.get("mappings") or []
    for row in mappings:
        missing = {"us", "kr_sector"} - row.keys()
        if missing:
            raise ConfigError(f"{path.name}: mapping {row!r} is missing {sorted(missing)}")
    return mappings


def load_all() -> dict[str, Any]:
    """Load every config file. Useful as a startup smoke check."""
    return {
        "watchlist": load_watchlist(),
        "aliases": load_aliases(),
        "delivery": load_delivery(),
        "models": load_models(),
        "sector_mapping": load_sector_mapping(),
        "rating": load_rating(),
    }
