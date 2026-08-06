"""Build the golden set. SPEC §7.3, MANUAL-TASKS §4.

    uv run python -m scripts.golden sample     # 1. pick candidates
    uv run python -m scripts.golden triage     # 2. sort into four buckets  (~17 min)
    uv run python -m scripts.golden label      # 3. score the chosen 100    (~30 min)
    uv run python -m scripts.golden recheck    # 4. next day, re-label ten
    uv run python -m scripts.golden verify     # 5. check the result

**No LLM touches any label, and none ever may.** The golden set exists to measure
how well models score articles (SPEC §7.4). A model that pre-filled the labels
would be marking its own paper, and a human correcting a pre-filled number
anchors to it — the measurement would come out flattering and nothing downstream
could detect that it had. Everything automated here is clerical: which articles
to look at, in what order, and whether the finished file is well-formed.

**Two passes, because the buckets cannot be known before reading.** SPEC asks for
25 clearly positive / 25 clearly negative / 25 ambiguous / 25 irrelevant, which
is a property of the article, so the first pass is a four-way sort at a few
seconds each and the second scores only the survivors on all five dimensions.
Labelling 200 articles on five dimensions to keep 100 would cost twice the time
for the same result.

Every pass is append-only and resumable: quit whenever, re-run to continue.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import random
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GOLDEN = ROOT / "data" / "golden"

CANDIDATES = GOLDEN / "candidates.jsonl"
TRIAGE = GOLDEN / "triage.jsonl"
LABELS = GOLDEN / "v1.jsonl"
RECHECK = GOLDEN / "recheck.jsonl"

# SPEC §7.3 composition: key → (stored name, prompt label, progress label).
# The progress label is separate because "명백한 긍정" and "명백한 부정" share a
# first word, and a counter reading "명백한 3/25  명백한 1/25" tells Ricky
# nothing about which bucket still needs filling.
BUCKETS = {
    "1": ("positive", "명백한 긍정", "긍정"),
    "2": ("negative", "명백한 부정", "부정"),
    "3": ("ambiguous", "애매", "애매"),
    "4": ("irrelevant", "무관 (종목은 언급되나 실적·주가와 무관)", "무관"),
}
PER_BUCKET = 25

# Candidates to triage. Twice the final size plus slack: buckets fill at very
# different rates — 무관 is common, 명백한 부정 is not — so a pool of exactly
# 100 would run out of one bucket long before the others.
POOL_SIZE = 240

# The five dimensions, in the order the prompt asks for them. SPEC §6.2.
DIMENSIONS = (
    ("relevance", 0.0, 1.0, "이 회사의 실적·주가와 얼마나 관련되나"),
    ("polarity", -1.0, 1.0, "방향만. 크기는 intensity가 받는다"),
    ("intensity", 0.0, 1.0, "재무적 충격의 크기"),
    ("uncertainty", 0.0, 1.0, "그 결과가 실제로 일어날지"),
    ("forwardness", 0.0, 1.0, "0=이미 반영된 과거, 1=미래 기대를 바꿈"),
)

# Items re-labelled a day later to measure Ricky against himself (SPEC §7.3).
RECHECK_SIZE = 10


# --- reading the corpus ----------------------------------------------------


def load_articles() -> dict[str, dict]:
    """Every collected article, de-duplicated by ``article_id``."""
    articles: dict[str, dict] = {}
    for path in sorted(glob.glob(str(RAW / "kr" / "news" / "*" / "*.jsonl.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                articles.setdefault(row["article_id"], row)
    return articles


def matched_pairs(articles: dict[str, dict]) -> list[tuple[str, str]]:
    """``(article_id, ticker)`` for every article the resolver attaches.

    The golden set scores *pairs*, not articles: SPEC §6.2's schema carries a
    ticker, and an article naming two companies is two judgments. Only matched
    articles are eligible — an article mentioning no watchlist name would be
    trivially irrelevant and would never reach the scoring stage in production
    either, so including it would measure nothing.
    """
    from src.entity.resolve import resolve
    from src.util.config import load_aliases

    matches, _ = resolve(articles.values(), load_aliases())
    if matches.empty:
        return []
    return [(str(row.article_id), str(row.ticker)) for row in matches.itertuples()]


# --- sampling --------------------------------------------------------------


def stratified_sample(
    pairs: Sequence[tuple[str, str]],
    articles: dict[str, dict],
    *,
    size: int = POOL_SIZE,
    seed: int = 20260806,
) -> list[tuple[str, str]]:
    """Spread the pool across tickers and days rather than sampling uniformly.

    Uniform sampling would hand back the corpus's own skew: measured
    2026-08-06, 삼성전자 held 138 of 479 pairs, so 29% of a random golden set
    would be one company and the bake-off would mostly measure how well a model
    reads Samsung coverage. Round-robin over tickers fixes that, and shuffling
    each ticker's own articles with a fixed seed keeps the choice reproducible
    while stopping a single busy day from filling a ticker's slots.
    """
    by_ticker: dict[str, list[tuple[str, str]]] = {}
    for article_id, ticker in pairs:
        by_ticker.setdefault(ticker, []).append((article_id, ticker))

    rng = random.Random(seed)
    for ticker in sorted(by_ticker):
        # Sort before shuffling: dict iteration order is stable but the file
        # order that produced it is not guaranteed, and the seed only makes the
        # shuffle reproducible if its input is.
        items = sorted(by_ticker[ticker], key=lambda pair: _published(articles, pair[0]))
        rng.shuffle(items)
        by_ticker[ticker] = items

    chosen: list[tuple[str, str]] = []
    tickers = sorted(by_ticker, key=lambda t: (-len(by_ticker[t]), t))
    while len(chosen) < size and any(by_ticker.values()):
        for ticker in tickers:
            if not by_ticker[ticker]:
                continue
            chosen.append(by_ticker[ticker].pop())
            if len(chosen) >= size:
                break
    return chosen


def _published(articles: dict[str, dict], article_id: str) -> str:
    return str(articles.get(article_id, {}).get("published_at", ""))


def write_candidates(pairs: Iterable[tuple[str, str]], articles: dict[str, dict]) -> int:
    from src.util.config import load_watchlist

    names = {entry.ticker: entry.name for entry in load_watchlist(market="KR")}
    GOLDEN.mkdir(parents=True, exist_ok=True)
    written = 0
    with CANDIDATES.open("w", encoding="utf-8") as handle:
        for article_id, ticker in pairs:
            article = articles[article_id]
            handle.write(
                json.dumps(
                    {
                        "article_id": article_id,
                        "ticker": ticker,
                        "name": names.get(ticker, ""),
                        "title": article.get("title", ""),
                        "description": article.get("description", ""),
                        "outlet": article.get("outlet", ""),
                        "link": article.get("link", ""),
                        "published_at": article.get("published_at", ""),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            written += 1
    return written


# --- append-only progress files -------------------------------------------


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    """Append one decision immediately.

    Written per item rather than at the end so that quitting — or a closed
    terminal — never costs work already done. Two hours of judgment is the most
    expensive thing in this repository.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def key_of(row: dict) -> tuple[str, str]:
    return (row["article_id"], row["ticker"])


# --- selection after triage ------------------------------------------------


def select_for_labelling(
    triaged: Sequence[dict], *, per_bucket: int = PER_BUCKET
) -> tuple[list[dict], dict[str, int]]:
    """The 100 that go on to full scoring, and how full each bucket is.

    Takes the first ``per_bucket`` of each bucket in triage order, so the
    selection is a function of what Ricky did rather than of a second random
    draw he cannot see.
    """
    picked: list[dict] = []
    counts: dict[str, int] = {name: 0 for name, _, _ in BUCKETS.values()}
    for row in triaged:
        bucket = row["bucket"]
        if counts.get(bucket, 0) >= per_bucket:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
        picked.append(row)
    return picked, counts


# --- display ---------------------------------------------------------------


def format_article(row: dict, *, index: int, total: int) -> str:
    published = str(row.get("published_at", ""))[:16].replace("T", " ")
    description = (row.get("description") or "").strip()
    if len(description) > 400:
        description = description[:400] + " …"
    lines = [
        "",
        "─" * 72,
        f"[{index}/{total}]  {row['ticker']} {row.get('name', '')}"
        f"   ·  {row.get('outlet', '')}  {published}",
        "",
        f"  {row.get('title', '')}",
    ]
    if description:
        lines += ["", *(f"  {chunk}" for chunk in _wrap(description, 68))]
    else:
        lines += ["", "  (본문 없음 — 헤드라인만 제공하는 매체)"]
    lines += ["", f"  {row.get('link', '')}", "─" * 72]
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


# --- parsing input ---------------------------------------------------------


class InputError(ValueError):
    """A malformed score line, reported back to the prompt rather than raised."""


def parse_scores(text: str) -> dict[str, float]:
    """Parse five space-separated numbers into the §6.2 dimensions.

    One line of five numbers rather than five prompts: the judgment is a single
    act and splitting it into five round trips triples the wall time without
    improving any of them.
    """
    parts = text.replace(",", " ").split()
    if len(parts) != len(DIMENSIONS):
        raise InputError(f"숫자 {len(DIMENSIONS)}개가 필요합니다 (입력 {len(parts)}개)")

    scores: dict[str, float] = {}
    for (name, low, high, _), raw in zip(DIMENSIONS, parts, strict=True):
        try:
            value = float(raw)
        except ValueError as exc:
            raise InputError(f"{name}: {raw!r}은 숫자가 아닙니다") from exc
        if not low <= value <= high:
            raise InputError(f"{name}은 {low}~{high} 범위여야 합니다 (입력 {value})")
        scores[name] = value
    return scores


# --- the interactive passes ------------------------------------------------


def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        return "q"


def run_triage() -> int:
    candidates = read_jsonl(CANDIDATES)
    if not candidates:
        print("후보가 없습니다. 먼저 `sample`을 실행하세요.")
        return 1

    done = {key_of(row) for row in read_jsonl(TRIAGE)}
    remaining = [row for row in candidates if key_of(row) not in done]
    counts: dict[str, int] = {name: 0 for name, _, _ in BUCKETS.values()}
    for row in read_jsonl(TRIAGE):
        counts[row["bucket"]] = counts.get(row["bucket"], 0) + 1

    print(f"\n1단계 — 4지선다 분류. 남은 후보 {len(remaining)}건 (완료 {len(done)}건)")
    print("각 기사를 읽고 어느 쪽인지만 고르세요. 숫자는 2단계에서 매깁니다.\n")
    for key, (_, label, _) in BUCKETS.items():
        print(f"  {key} = {label}")
    print("  s = 건너뛰기    q = 저장하고 종료\n")

    for index, row in enumerate(remaining, start=1):
        if all(counts.get(name, 0) >= PER_BUCKET for name, _, _ in BUCKETS.values()):
            print("\n네 버킷이 모두 찼습니다. `label`로 넘어가세요.")
            break

        print(format_article(row, index=index, total=len(remaining)))
        status = "  ".join(
            f"{short} {counts.get(name, 0)}/{PER_BUCKET}" for name, _, short in BUCKETS.values()
        )
        print(f"  진행: {status}")

        while True:
            answer = _prompt("  분류 [1/2/3/4/s/q] > ").lower()
            if answer == "q":
                print(f"\n저장 완료. {TRIAGE}")
                return 0
            if answer == "s":
                break
            if answer in BUCKETS:
                name = BUCKETS[answer][0]
                if counts.get(name, 0) >= PER_BUCKET:
                    print(f"  '{BUCKETS[answer][1]}' 버킷은 이미 찼습니다. 다른 분류를 고르세요.")
                    continue
                append_jsonl(TRIAGE, {**row, "bucket": name})
                counts[name] = counts.get(name, 0) + 1
                break
            print("  1, 2, 3, 4, s, q 중 하나를 입력하세요.")

    print(f"\n분류 완료 {sum(counts.values())}건 → {TRIAGE}")
    return 0


def run_label(*, target: Path = LABELS, source: Sequence[dict] | None = None) -> int:
    triaged = read_jsonl(TRIAGE)
    if not triaged:
        print("분류 결과가 없습니다. 먼저 `triage`를 실행하세요.")
        return 1

    picked = list(source) if source is not None else select_for_labelling(triaged)[0]
    done = {key_of(row) for row in read_jsonl(target)}
    remaining = [row for row in picked if key_of(row) not in done]

    print(f"\n2단계 — 5개 차원 채점. 남은 {len(remaining)}건 (완료 {len(done)}건)")
    print("  한 줄에 숫자 5개를 공백으로 구분해 입력합니다. 예: 0.9 -0.4 0.6 0.3 0.8\n")
    for name, low, high, hint in DIMENSIONS:
        print(f"  {name:<12} [{low:g}~{high:g}]  {hint}")
    print("\n  s = 건너뛰기    q = 저장하고 종료")
    print("  ⚠ 그 뒤 주가가 어떻게 됐는지 보지 말고 매길 것 (MANUAL-TASKS §4)\n")

    for index, row in enumerate(remaining, start=1):
        print(format_article(row, index=index, total=len(remaining)))
        print(f"  분류: {row.get('bucket', '?')}")
        print(f"  순서: {' '.join(name for name, _, _, _ in DIMENSIONS)}")

        while True:
            answer = _prompt("  점수 > ")
            if answer.lower() == "q":
                print(f"\n저장 완료. {target}")
                return 0
            if answer.lower() == "s":
                break
            try:
                scores = parse_scores(answer)
            except InputError as exc:
                print(f"  {exc}")
                continue
            append_jsonl(
                target,
                {
                    "article_id": row["article_id"],
                    "ticker": row["ticker"],
                    "bucket": row.get("bucket", ""),
                    **scores,
                    "labeled_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                },
            )
            break

    print(f"\n채점 완료 → {target}")
    return 0


def run_recheck(*, size: int = RECHECK_SIZE, seed: int = 20260807) -> int:
    """Re-label a subset a day later, without showing the first answers.

    SPEC §7.3: if Ricky's two passes disagree more than the models disagree with
    each other, the schema is underdefined and the bake-off would be measuring
    noise. The first answers are deliberately not displayed — seeing them turns
    the check into a memory test.
    """
    labelled = read_jsonl(LABELS)
    if len(labelled) < size:
        print(f"채점이 {len(labelled)}건뿐입니다. {size}건 이상 끝난 뒤 실행하세요.")
        return 1

    first_day = min(str(row.get("labeled_at", ""))[:10] for row in labelled)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    if first_day == today:
        print("⚠ 오늘 매긴 라벨입니다. 하루 지난 뒤에 실행하세요 (기억이 아니라 기준을 재는 검사).")
        return 1

    triaged = {key_of(row): row for row in read_jsonl(TRIAGE)}
    rng = random.Random(seed)
    chosen = rng.sample(sorted(labelled, key=key_of), size)
    source = [triaged[key_of(row)] for row in chosen if key_of(row) in triaged]

    print(f"\n재라벨링 검사 — {len(source)}건. 첫 회차 답은 보여주지 않습니다.")
    return run_label(target=RECHECK, source=source)


# --- verification ----------------------------------------------------------


def verify() -> tuple[bool, list[str]]:
    """Check the finished set, and report Ricky's agreement with himself."""
    problems: list[str] = []
    labelled = read_jsonl(LABELS)

    if not labelled:
        return False, [f"{LABELS} 가 비어 있습니다"]

    counts: dict[str, int] = {}
    for row in labelled:
        counts[row.get("bucket", "?")] = counts.get(row.get("bucket", "?"), 0) + 1
    for name, label, _ in BUCKETS.values():
        have = counts.get(name, 0)
        if have != PER_BUCKET:
            problems.append(f"{label}: {have}건 (목표 {PER_BUCKET})")

    seen = set()
    for row in labelled:
        key = key_of(row)
        if key in seen:
            problems.append(f"중복: {key}")
        seen.add(key)

    for row in labelled:
        for name, low, high, _ in DIMENSIONS:
            if name not in row:
                problems.append(f"{key_of(row)}: {name} 없음")
            elif not low <= float(row[name]) <= high:
                problems.append(f"{key_of(row)}: {name}={row[name]} 범위 밖")

    # A golden set whose polarity never leaves the middle cannot separate models.
    strong = sum(1 for row in labelled if abs(float(row.get("polarity", 0))) >= 0.5)
    if labelled and strong < len(labelled) * 0.25:
        problems.append(f"|polarity| ≥ 0.5 인 항목이 {strong}건뿐 — 쉬운 사례만 모였을 수 있습니다")

    recheck = read_jsonl(RECHECK)
    if recheck:
        first = {key_of(row): row for row in labelled}
        gaps = []
        for row in recheck:
            other = first.get(key_of(row))
            if not other:
                continue
            gaps.append(
                max(abs(float(row[name]) - float(other[name])) for name, _, _, _ in DIMENSIONS)
            )
        if gaps:
            worst = max(gaps)
            mean = sum(gaps) / len(gaps)
            print(f"재라벨링 일치도: 평균 차이 {mean:.2f}, 최대 {worst:.2f} ({len(gaps)}건)")
            if mean > 0.25:
                problems.append(
                    f"두 회차 평균 차이 {mean:.2f} — 스키마가 덜 정의됐을 수 있습니다 (SPEC §7.3)"
                )

    return not problems, problems


# --- entry point -----------------------------------------------------------


def run_sample(size: int) -> int:
    print("기사를 읽는 중…")
    articles = load_articles()
    pairs = matched_pairs(articles)
    print(f"  고유 기사 {len(articles):,}건 → 매칭된 (기사,종목) 쌍 {len(pairs):,}개")
    if not pairs:
        print("매칭된 기사가 없습니다. 수집기를 더 돌리세요.")
        return 1

    chosen = stratified_sample(pairs, articles, size=size)
    written = write_candidates(chosen, articles)

    spread: dict[str, int] = {}
    for _, ticker in chosen:
        spread[ticker] = spread.get(ticker, 0) + 1
    top = sorted(spread.items(), key=lambda kv: -kv[1])[:5]
    print(f"  후보 {written}건 → {CANDIDATES}")
    print(f"  종목 {len(spread)}개에 분산. 최다: {', '.join(f'{t} {n}건' for t, n in top)}")
    print("\n다음: uv run python -m scripts.golden triage")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sampler = sub.add_parser("sample", help="후보를 추출한다")
    sampler.add_argument("--size", type=int, default=POOL_SIZE)
    sub.add_parser("triage", help="1단계 — 4지선다 분류")
    sub.add_parser("label", help="2단계 — 5개 차원 채점")
    sub.add_parser("recheck", help="하루 뒤 재라벨링 검사")
    sub.add_parser("verify", help="완성된 골든셋을 검사한다")

    args = parser.parse_args(argv)

    if args.command == "sample":
        return run_sample(args.size)
    if args.command == "triage":
        return run_triage()
    if args.command == "label":
        return run_label()
    if args.command == "recheck":
        return run_recheck()

    ok, problems = verify()
    if ok:
        print(f"✅ 골든셋 정상 — {len(read_jsonl(LABELS))}건")
        return 0
    print("❌ 확인이 필요합니다:")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
