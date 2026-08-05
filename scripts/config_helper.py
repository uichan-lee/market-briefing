"""Operator tooling for the two hand-maintained config files.

MANUAL-TASKS.md §2 (`watchlist.yaml`) and §3 (`aliases.yaml`) are the only two
BLOCKING tasks that are pure hand work. This script removes the mechanical parts
of both and leaves every judgment call with Ricky.

    uv run python scripts/config_helper.py find 한화에어로스페이스 LG에너지솔루션
    uv run python scripts/config_helper.py scaffold
    uv run python scripts/config_helper.py audit --samples 5

``find`` and ``scaffold`` read the KRX listing and therefore need ``KRX_ID`` and
``KRX_PW``. ``audit`` is entirely offline.

Where this sits against CLAUDE.md
---------------------------------
CLAUDE.md forbids auto-generating or auto-extending ``config/aliases.yaml``,
because a wrong alias corrupts every downstream number silently while a missing
alias only loses coverage. ``scaffold`` is built to respect that asymmetry
rather than to work around it:

* It never writes ``config/aliases.yaml``. Output goes to a separate draft file
  that Ricky reads, edits, and copies from by hand.
* It leaves ``aliases`` empty. That is the field that can misattribute, and the
  one that needs somebody who reads Korean financial headlines.
* It fills only ``exclude`` and ``ambiguous_parents``. Both can *only ever
  reduce* what matches, so a wrong entry there costs coverage and never
  correctness — the safe side of the very asymmetry the rule protects.

``audit`` writes nothing at all.

Matching is delegated to :mod:`src.entity.resolve`, the production resolver, so
``audit`` measures exactly what the pipeline will do rather than an approximation
of it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.entity.resolve import resolve_article
from src.util.config import CONFIG_DIR, AliasEntry, load_aliases, load_watchlist
from src.util.session import previous_trading_day

ROOT = Path(__file__).resolve().parents[1]
NEWS_DIR = ROOT / "data" / "raw" / "kr" / "news"
DRAFT_PATH = CONFIG_DIR / "aliases.draft.yaml"

# A prefix shared by at least this many listed companies is treated as a group
# name (삼성, SK, 한화) rather than part of a company's own identity.
_GROUP_MIN_MEMBERS = 3


# --- KRX listing ---------------------------------------------------------


def _listing(as_of: dt.date | None = None) -> pd.DataFrame:
    """Every listed KOSPI/KOSDAQ name with its KRX sector, indexed by ticker.

    KRX publishes the classification per trading day, so an as-of date is
    required; the previous trading day is used because today's file does not
    exist until the session closes.
    """
    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        raise SystemExit(
            "KRX_ID and KRX_PW are not set. Run `set -a; source .env; set +a` first — "
            "`find` and `scaffold` read the KRX listing. `audit` needs neither."
        )

    from src.util.krx import KrxSessionError, import_pykrx_stock

    try:
        # Imported lazily; `audit` must not require a login at all.
        stock = import_pykrx_stock()
    except KrxSessionError as exc:
        raise SystemExit(str(exc)) from exc

    day = as_of or previous_trading_day("KR", dt.date.today())
    stamp = day.strftime("%Y%m%d")

    frames = []
    for market in ("KOSPI", "KOSDAQ"):
        df = stock.get_market_sector_classifications(stamp, market)
        df = df[["종목명", "업종명"]].copy()
        df["market"] = market
        frames.append(df)

    listing = pd.concat(frames)
    listing.index.name = "ticker"
    return listing.rename(columns={"종목명": "name", "업종명": "sector"})


# --- find ----------------------------------------------------------------


def find(queries: list[str], listing: pd.DataFrame) -> None:
    """Resolve names or tickers to watchlist rows, printed ready to paste."""
    print("# Paste under `kr:` in config/watchlist.yaml. Set `held` honestly.")
    print("# Tickers are quoted deliberately — see the octal warning in that file.\n")

    for query in queries:
        if query.isdigit() and query in listing.index:
            hits = listing.loc[[query]]
        else:
            hits = listing[listing["name"].str.contains(query, case=False, regex=False)]
            # An exact name wins over its own substring matches. 현대차 is a
            # listed company *and* a prefix of 현대차증권 and three preferred
            # shares; without this the most ordinary query in the file is
            # rejected as ambiguous.
            exact = hits[hits["name"] == query]
            if len(exact) == 1:
                hits = exact

        if hits.empty:
            print(f"  # NOT FOUND: {query!r} — check the spelling against KRX")
            continue
        if len(hits) > 1:
            names = ", ".join(f"{t}={r['name']}" for t, r in hits.iterrows())
            print(f"  # AMBIGUOUS: {query!r} matches {len(hits)} listings — {names}")
            continue

        ticker = hits.index[0]
        row = hits.iloc[0]
        print(f'  - ticker: "{ticker}"')
        print(f"    name: {row['name']}")
        print(f"    sector: {row['sector']}")
        print("    held: false")
        print()


# --- scaffold ------------------------------------------------------------


@dataclass(frozen=True)
class Scaffold:
    ticker: str
    canonical: str
    must_exclude: list[str]
    group: str | None
    siblings: list[str]


def _group_prefix(name: str, all_names: list[str]) -> str | None:
    """The shortest prefix of ``name`` shared by enough listings to be a group.

    Derived from the listing rather than a hardcoded chaebol list, so a name
    this script has never seen still gets a sensible answer. 삼성전자 yields
    삼성; NAVER yields nothing, because no prefix of it is shared.
    """
    for length in range(2, len(name)):
        prefix = name[:length]
        members = sum(1 for other in all_names if other.startswith(prefix))
        if members >= _GROUP_MIN_MEMBERS:
            return prefix
    return None


def build_scaffold(ticker: str, listing: pd.DataFrame) -> Scaffold:
    """Collect the mechanically-derivable half of one alias entry.

    ``must_exclude`` holds listings that *contain* the canonical name — 삼성전자우
    against 삼성전자. Those are the ones that falsely match no matter how
    carefully the aliases are written, because any alias equal to the canonical
    name is a substring of them.
    """
    canonical = listing.loc[ticker, "name"]
    all_names = listing["name"].tolist()

    must_exclude = sorted(other for other in all_names if other != canonical and canonical in other)

    group = _group_prefix(canonical, all_names)
    siblings = (
        sorted(
            other
            for other in all_names
            if other != canonical and other.startswith(group) and other not in must_exclude
        )
        if group
        else []
    )
    return Scaffold(ticker, canonical, must_exclude, group, siblings)


def _yaml_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]" if items else "[]"


def scaffold(listing: pd.DataFrame, path: Path = DRAFT_PATH) -> None:
    """Write an alias worksheet covering every watchlist ticker."""
    # KR only: scaffolding reads the KRX listing, and a US symbol has none.
    watchlist = load_watchlist(market="KR")
    if not watchlist:
        raise SystemExit("config/watchlist.yaml has no kr entries — do MANUAL-TASKS.md §2 first.")

    existing = load_aliases()
    lines = [
        "# WORKSHEET — not loaded by anything. Generated by scripts/config_helper.py.",
        "#",
        "# `aliases` is deliberately empty on every entry. It is the only field that",
        "# can misattribute an article, so CLAUDE.md leaves it to Ricky; `exclude` and",
        "# `ambiguous_parents` can only ever drop a match, so they are pre-filled.",
        "#",
        "# Fill in `aliases`, delete what is wrong, then copy entries into",
        "# config/aliases.yaml. Run `audit` afterwards to see what actually matched.",
        "",
    ]

    for entry in watchlist:
        if entry.ticker not in listing.index:
            lines.append(f'# "{entry.ticker}" ({entry.name}) is not in the KRX listing — delisted?')
            lines.append("")
            continue

        card = build_scaffold(entry.ticker, listing)
        if entry.ticker in existing:
            lines.append("# already present in config/aliases.yaml — compare, do not re-paste")

        lines.append(f'"{card.ticker}":')
        lines.append(f"  canonical: {card.canonical}")
        lines.append("  aliases: []   # TODO(Ricky): 한글 표기, 띄어쓰기 변형, 영문명, 약칭")
        lines.append(f"  exclude: {_yaml_list(card.must_exclude)}")
        lines.append(f"  ambiguous_parents: {_yaml_list([card.group] if card.group else [])}")

        if card.must_exclude:
            lines.append(
                f"  # exclude above is mandatory: {len(card.must_exclude)} listing(s) contain "
                f"{card.canonical!r} as a substring."
            )
        if card.siblings:
            shown = ", ".join(card.siblings[:12])
            more = f" (+{len(card.siblings) - 12} more)" if len(card.siblings) > 12 else ""
            lines.append(f"  # {card.group} affiliates, add to exclude only if an alias is loose:")
            lines.append(f"  #   {shown}{more}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)} — {len(watchlist)} ticker(s)")


# --- audit ---------------------------------------------------------------


def _read_corpus() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(NEWS_DIR.glob("*/*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            rows.extend(json.loads(line) for line in handle if line.strip())
    # The collector dedups within a day; across days a re-delivered item can
    # reappear, and counting it twice would overstate every alias equally.
    unique = {row["article_id"]: row for row in rows}
    return list(unique.values())


def match_article(text: str, entries: dict[str, AliasEntry]) -> tuple[set[str], set[str]]:
    """Return (tickers matched, tickers seen only through a group name).

    Delegates to :mod:`src.entity.resolve`, which is the production resolver.
    This used to carry its own copy of the masking logic; two implementations of
    the same subtle rule drift, and the one that drifts is whichever is not
    covered by the tests that matter. The audit now measures exactly what the
    pipeline will do.
    """
    matches, ambiguous = resolve_article(text, entries)
    return {m.ticker for m in matches}, ambiguous


def audit(samples: int = 0) -> int:
    """Score aliases.yaml against the news already collected. Returns an exit code."""
    entries = load_aliases()
    # KR only: the corpus is Korean-language news, which US symbols never match.
    watchlist = {e.ticker: e.name for e in load_watchlist(market="KR")}
    corpus = _read_corpus()

    if not corpus:
        raise SystemExit(f"no articles under {NEWS_DIR.relative_to(ROOT)} — nothing to audit")

    per_ticker: Counter[str] = Counter()
    per_alias: Counter[tuple[str, str]] = Counter()
    headlines: dict[str, list[str]] = defaultdict(list)
    ambiguous_only = 0
    multi = 0

    for row in corpus:
        text = f"{row.get('title', '')} {row.get('description', '')}"
        matched, amb = match_article(text, entries)

        for ticker in matched:
            per_ticker[ticker] += 1
            if len(headlines[ticker]) < samples:
                headlines[ticker].append(row.get("title", ""))
            for alias in entries[ticker].aliases:
                if alias in text:
                    per_alias[(ticker, alias)] += 1

        if matched:
            multi += len(matched) > 1
        elif amb:
            ambiguous_only += 1

    total = len(corpus)
    hit = sum(per_ticker.values())

    print(f"corpus: {total} unique articles under {NEWS_DIR.relative_to(ROOT)}")
    print(f"aliases.yaml: {len(entries)} ticker(s); watchlist has {len(watchlist)}")
    print(f"ambiguous-only (group name, no alias): {ambiguous_only} ({ambiguous_only / total:.1%})")
    print(f"articles matching 2+ tickers: {multi}\n")

    print(f"{'ticker':<8} {'canonical':<16} {'articles':>8}")
    for ticker, entry in entries.items():
        print(f"{ticker:<8} {entry.canonical:<16} {per_ticker[ticker]:>8}")

    uncovered = sorted(set(watchlist) - set(entries))
    if uncovered:
        named = ", ".join(f"{t} {watchlist[t]}" for t in uncovered)
        print(f"\nNO ALIAS ENTRY ({len(uncovered)}): {named}")

    dead = [
        (ticker, alias)
        for ticker, entry in entries.items()
        for alias in entry.aliases
        if per_alias[(ticker, alias)] == 0
    ]
    if dead:
        print(f"\nnever matched ({len(dead)}) — dead weight, or the corpus is still too small:")
        for ticker, alias in dead:
            print(f"  {ticker}  {alias}")

    if samples:
        print("\n--- samples: read these for misattribution ---")
        for ticker, titles in headlines.items():
            print(f"\n{ticker} {entries[ticker].canonical}")
            for title in titles:
                print(f"  · {title}")

    print(f"\n{hit} ticker-article matches across {total} articles")
    return 0


# --- entry point ---------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_find = sub.add_parser("find", help="resolve names to watchlist rows")
    p_find.add_argument("queries", nargs="+", help="company name (partial ok) or 6-digit ticker")

    sub.add_parser("scaffold", help="write config/aliases.draft.yaml from the watchlist")

    p_audit = sub.add_parser("audit", help="score aliases.yaml against collected news")
    p_audit.add_argument("--samples", type=int, default=0, help="headlines to print per ticker")

    args = parser.parse_args(argv)

    if args.command == "find":
        find(args.queries, _listing())
    elif args.command == "scaffold":
        scaffold(_listing())
    elif args.command == "audit":
        return audit(args.samples)
    return 0


if __name__ == "__main__":
    sys.exit(main())
