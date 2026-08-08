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


_US_TICKER = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")


@dataclass(frozen=True)
class WatchlistEntry:
    ticker: str
    name: str
    sector: str | None
    held: bool
    market: str  # "KR" or "US"


def _require_us_ticker(ticker: Any, source: str) -> str:
    """Validate a US symbol, with the YAML-boolean trap called out by name.

    The Korean side is protected against YAML's octal rule; the US side has the
    same class of hazard from a different rule. YAML 1.1 reads bare ``ON``,
    ``NO``, ``Y`` and ``OFF`` as booleans, so an unquoted ticker among those
    becomes ``True``/``False`` and every downstream lookup for it fails in a way
    that never mentions YAML. Quoting is required here for the same reason it is
    required there.
    """
    if isinstance(ticker, bool):
        raise ConfigError(
            f"{source}: ticker {ticker!r} was parsed as a boolean, which means it was left "
            "unquoted in YAML. YAML 1.1 reads ON/OFF/YES/NO/Y/N as booleans. Quote it."
        )
    if not isinstance(ticker, str):
        raise ConfigError(f"{source}: ticker {ticker!r} is {type(ticker).__name__}, expected str")
    if not _US_TICKER.match(ticker):
        raise ConfigError(
            f"{source}: ticker {ticker!r} is not a US symbol (uppercase letters, . or -)"
        )
    return ticker


def load_watchlist(path: Path | None = None, *, market: str | None = None) -> list[WatchlistEntry]:
    """Load ``config/watchlist.yaml``.

    Both the ``kr`` and ``us`` sections are read. ``market`` filters to one of
    them; ``None`` returns both. Callers doing Korean-only work — alias
    scaffolding, news matching — must pass ``"KR"``, because a US symbol has no
    KRX listing and no Korean news to match against.

    An empty watchlist is allowed. It is the file's committed starting state and
    MANUAL-TASKS.md §2 leaves filling it to Ricky, so callers that require a
    populated list say so themselves rather than have loading fail here.

    Tickers are namespaced per market: ``005930`` and ``AAPL`` cannot collide,
    but two entries within the same market can, and that is rejected.
    """
    path = path or CONFIG_DIR / "watchlist.yaml"
    raw = _read_yaml(path) or {}
    source = path.name

    if market is not None and market not in ("KR", "US"):
        raise ConfigError(f"market must be 'KR', 'US' or None, got {market!r}")

    entries: list[WatchlistEntry] = []

    for section, validator in (("kr", _require_quoted_ticker), ("us", _require_us_ticker)):
        code = section.upper()
        if market is not None and market != code:
            continue

        seen: set[str] = set()
        for row in raw.get(section) or []:
            if not isinstance(row, dict):
                raise ConfigError(f"{source}: expected a mapping per entry, got {row!r}")
            missing = {"ticker", "name"} - row.keys()
            if missing:
                raise ConfigError(f"{source}: entry {row!r} is missing {sorted(missing)}")

            ticker = validator(row["ticker"], source)
            if ticker in seen:
                raise ConfigError(f"{source}: {code} ticker {ticker} listed twice")
            seen.add(ticker)

            entries.append(
                WatchlistEntry(
                    ticker=ticker,
                    name=str(row["name"]),
                    sector=row.get("sector"),
                    held=bool(row.get("held", False)),
                    market=code,
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

    # Designed weight for features nothing produces yet. The report header reads
    # it so the briefing keeps naming what is absent; rate() never does. A name
    # in both mappings is the one contradiction worth rejecting outright — it
    # would be counted as active weight and reported as missing in the same run.
    deferred = raw.get("deferred_weights") or {}
    if not isinstance(deferred, dict):
        raise ConfigError(f"{path.name}: 'deferred_weights' must be a mapping")
    for feature, weight in deferred.items():
        if not isinstance(weight, int | float):
            raise ConfigError(f"{path.name}: deferred weight for {feature!r} is not a number")
    both = sorted(deferred.keys() & weights.keys())
    if both:
        raise ConfigError(f"{path.name}: {both} are both active and deferred weights")

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
    """Load ``config/sector_mapping.yaml``.

    Rejects a ``us`` symbol that no collector fetches. This file drives SPEC
    §2.2①, whose whole content is a correlation between a US symbol and a Korean
    sector — so an uncollected symbol does not fail, it renders an empty row that
    reads as "the correlation broke down". That is the opposite of the truth and
    the reader cannot tell. The file carried ``RUT`` (the Russell 2000 index,
    which nothing collects) until 2026-08-06 for exactly this reason.
    """
    # Imported here rather than at module scope: config is loaded by everything,
    # and the collectors import config-adjacent utilities. The symbol universe
    # lives with the collector that fetches it, which is where it stays correct.
    from src.collectors.us_price import INDEX_ETFS

    path = path or CONFIG_DIR / "sector_mapping.yaml"
    raw = _read_yaml(path) or {}

    mappings = raw.get("mappings") or []
    known = {entry.ticker for entry in load_watchlist(market="KR")}

    for row in mappings:
        missing = {"us", "kr_sector", "tickers"} - row.keys()
        if missing:
            raise ConfigError(f"{path.name}: mapping {row!r} is missing {sorted(missing)}")

        symbol = row["us"]
        if symbol not in INDEX_ETFS:
            raise ConfigError(
                f"{path.name}: {symbol!r} is not collected — "
                f"us_price.INDEX_ETFS holds {sorted(INDEX_ETFS)}"
            )

        tickers = row["tickers"]
        if not isinstance(tickers, list) or not tickers:
            raise ConfigError(f"{path.name}: {symbol} needs a non-empty 'tickers' list")

        # A ticker outside the watchlist has no price history loaded, so it would
        # contribute silently nothing to the correlation. This mapping matched
        # zero tickers until 2026-08-06 for the analogous reason — it joined on a
        # sector label the watchlist does not use — and rendered as "이력 부족",
        # which reads as a broken-down correlation rather than a broken config.
        unknown = [t for t in tickers if t not in known]
        if unknown:
            raise ConfigError(
                f"{path.name}: {symbol} names {unknown} which are not in watchlist.yaml"
            )
    return mappings


@dataclass(frozen=True)
class NewsFeed:
    """One RSS feed. SPEC §3.1."""

    name: str
    outlet: str
    section: str
    url: str
    domain: str
    # Only for feeds emitting a naive pubDate. Declared here rather than assumed
    # in code: reading a timestamp in the wrong zone shifts an article by hours
    # and can make it appear knowable before it was published, which is the
    # look-ahead failure CLAUDE.md forbids. An assumption in config is
    # reviewable; the same assumption buried in a parser is not.
    timezone: str | None = None


def load_news_feeds(path: Path | None = None, *, enabled_only: bool = True) -> list[NewsFeed]:
    """Load and validate ``config/news_feeds.yaml``.

    Coverage equals this file — an outlet absent here is invisible to the
    pipeline — so the checks target the edits that would silently shrink or
    corrupt it. A duplicated ``name`` would make two feeds indistinguishable in
    stored rows; a duplicated ``url`` doubles a source's apparent article volume
    and skews ``news_volume_z`` without ever looking wrong.
    """
    path = path or CONFIG_DIR / "news_feeds.yaml"
    raw = _read_yaml(path) or {}
    entries = raw.get("feeds") or []

    feeds: list[NewsFeed] = []
    seen_names: dict[str, int] = {}
    seen_urls: dict[str, str] = {}

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{path.name}: feed #{index} is {type(entry).__name__}, expected a mapping"
            )

        missing = {"name", "outlet", "section", "url", "domain"} - entry.keys()
        if missing:
            raise ConfigError(f"{path.name}: feed #{index} is missing {sorted(missing)}")

        name, url = str(entry["name"]), str(entry["url"])

        if name in seen_names:
            raise ConfigError(
                f"{path.name}: duplicate feed name {name!r} (#{seen_names[name]} and #{index}); "
                f"names identify the source in every stored article and must be unique"
            )
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{path.name}: feed {name!r} url is not http(s): {url!r}")
        if url in seen_urls:
            raise ConfigError(
                f"{path.name}: {name!r} and {seen_urls[url]!r} share the url {url!r}; "
                f"the same source counted twice inflates news volume invisibly"
            )

        seen_names[name] = index
        seen_urls[url] = name

        if enabled_only and not entry.get("enabled", True):
            continue

        feeds.append(
            NewsFeed(
                name=name,
                outlet=str(entry["outlet"]),
                section=str(entry["section"]),
                url=url,
                domain=str(entry["domain"]),
                timezone=str(entry["timezone"]) if entry.get("timezone") else None,
            )
        )

    if not feeds:
        raise ConfigError(
            f"{path.name}: no enabled feeds; news collection would silently do nothing"
        )
    return feeds


def load_all() -> dict[str, Any]:
    """Load every config file. Useful as a startup smoke check."""
    return {
        "watchlist": load_watchlist(),
        "aliases": load_aliases(),
        "delivery": load_delivery(),
        "models": load_models(),
        "sector_mapping": load_sector_mapping(),
        "rating": load_rating(),
        "news_feeds": load_news_feeds(),
    }
