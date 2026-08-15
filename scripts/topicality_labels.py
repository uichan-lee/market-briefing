"""Build the topicality label set. SPEC §6.1 Stage 1, SPEC §12 step 6.

    uv run python -m scripts.topicality_labels sample   # 1. pick candidates
    uv run python -m scripts.topicality_labels label     # 2. y/n per article  (~15 min)

Separate from ``scripts/golden.py``'s golden set (``data/golden/v1.jsonl``) on
purpose: that set measures P&L *materiality* (SPEC §6.2's ``relevance``),
which ``notes/step6-plan.md``'s design section shows is a different, and
sometimes opposite, construct from *topicality* — is this article about the
company at all. Calibrating ``src/embed/topicality.py`` against ``v1.jsonl``
would check it against the wrong question.

What IS reused, because it is generic corpus/sampling infrastructure rather
than anything materiality-specific: ``scripts.golden``'s ``load_articles``,
``matched_pairs``, ``stratified_sample``, and the small append-only-file
helpers (``read_jsonl``/``append_jsonl``/``key_of``/``format_article``/
``_prompt``). ``golden.py`` itself is not modified — its own CLI, tests and
five-dimension scoring stay exactly as they are.

**No LLM touches any label, and none ever may** — the same rule
``scripts/golden.py`` states, for the same reason: this set exists to measure
whether ``src/embed/topicality.py``'s bge-m3 similarity agrees with a human
judgment, and a model-filled label would be marking its own paper.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from scripts.golden import _prompt as prompt
from scripts.golden import (
    append_jsonl,
    format_article,
    key_of,
    load_articles,
    matched_pairs,
    read_jsonl,
    stratified_sample,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "data" / "golden"

CANDIDATES = GOLDEN / "topicality_candidates.jsonl"
LABELS = GOLDEN / "topicality_v1.jsonl"

# Small and purpose-built, per notes/step6-plan.md — not SPEC §7.3's 100.
# A binary judgment carries more signal per label than golden.py's five
# continuous dimensions, so a smaller pool is enough to calibrate one cut
# point against.
POOL_SIZE = 150

# A different seed from golden.py's SAMPLE (20260806): the two pools are
# meant to be independent draws, not overlapping subsets of the same one.
SAMPLE_SEED = 20260816


def write_candidates(pairs, articles: dict[str, dict]) -> int:
    """Same row shape as ``scripts.golden.write_candidates``, written to this
    module's own ``CANDIDATES`` path rather than golden.py's."""
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


def run_sample(size: int) -> int:
    print("기사를 읽는 중…")
    articles = load_articles()
    pairs = matched_pairs(articles)
    print(f"  고유 기사 {len(articles):,}건 → 매칭된 (기사,종목) 쌍 {len(pairs):,}개")
    if not pairs:
        print("매칭된 기사가 없습니다. 수집기를 더 돌리세요.")
        return 1

    chosen = stratified_sample(pairs, articles, size=size, seed=SAMPLE_SEED)
    written = write_candidates(chosen, articles)

    spread: dict[str, int] = {}
    for _, ticker in chosen:
        spread[ticker] = spread.get(ticker, 0) + 1
    top = sorted(spread.items(), key=lambda kv: -kv[1])[:5]
    print(f"  후보 {written}건 → {CANDIDATES}")
    print(f"  종목 {len(spread)}개에 분산. 최다: {', '.join(f'{t} {n}건' for t, n in top)}")
    print("\n다음: uv run python -m scripts.topicality_labels label")
    return 0


def run_label() -> int:
    candidates = read_jsonl(CANDIDATES)
    if not candidates:
        print("후보가 없습니다. 먼저 `sample`을 실행하세요.")
        return 1

    labelled = read_jsonl(LABELS)
    done = {key_of(row) for row in labelled}
    remaining = [row for row in candidates if key_of(row) not in done]

    print(f"\n관련성(topicality) 라벨링 — 남은 {len(remaining)}건 (완료 {len(done)}건)")
    print("질문: 이 기사가 이 종목에 관한 것입니까? (실적과의 관련성이 아니라 주제 자체)")
    print("  y = 그렇다    n = 아니다 (동명이인, 스쳐가는 언급, 실은 다른 회사 얘기)")
    print("  s = 건너뛰기    q = 저장하고 종료\n")

    for index, row in enumerate(remaining, start=1):
        print(format_article(row, index=index, total=len(remaining)))
        while True:
            answer = prompt("  이 종목에 관한 기사입니까? [y/n/s/q] > ").lower()
            if answer == "q":
                print(f"\n저장 완료. {LABELS}")
                return 0
            if answer == "s":
                break
            if answer in ("y", "n"):
                append_jsonl(
                    LABELS,
                    {
                        "article_id": row["article_id"],
                        "ticker": row["ticker"],
                        "topical": answer == "y",
                        "labeled_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    },
                )
                break
            print("  y, n, s, q 중 하나를 입력하세요.")

    print(f"\n라벨링 완료 {len(read_jsonl(LABELS))}건 → {LABELS}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sampler = sub.add_parser("sample", help="후보를 추출한다")
    sampler.add_argument("--size", type=int, default=POOL_SIZE)
    sub.add_parser("label", help="y/n 라벨링")

    args = parser.parse_args(argv)

    if args.command == "sample":
        return run_sample(args.size)
    return run_label()


if __name__ == "__main__":
    sys.exit(main())
