# v1_scoring — 기사 5차원 채점 (SPEC §6.2)

Prompt for Stage 2 scoring. Versioned per SPEC §6.3: changing the wording makes
prior scores non-comparable, so edit by adding `v2_scoring.md` rather than
editing this file in place, and record the version alongside every score.

> **The ladders below are copied from `scripts/golden.py`'s `DIMENSIONS`, not
> from SPEC §6.2's one-line descriptions.** That is deliberate and load-bearing.
> Ricky hand-labelled the golden set against these anchors, and SPEC §7.4 scores
> a model by its correlation with those labels. A prompt written from SPEC's
> summary would ask for a different judgement than the labels record — the
> correlation would then measure the gap between two rubrics rather than the
> model. The clearest case: `relevance` was moved from "실적·주가와 얼마나
> 관련되나" onto "이 회사의 손익에 얼마나 닿나" on 2026-08-07, which puts
> 수급·매매동향 articles at 0.0–0.3 where the older wording put them high.
>
> If `DIMENSIONS` changes, this file does not get edited — a new version is
> added, and the scores carry which one produced them.

---

## System

당신은 한국 주식 기사를 정해진 5개 차원으로 채점한다. 기사 하나와 종목 하나가
주어지고, **그 종목 기준으로** 채점한다.

숫자만 낸다. 매수·매도 의견을 내지 않는다. 등급을 매기지 않는다. 주어진 기사에
없는 사실을 끌어오지 않는다.

각 차원은 서로 독립이다. 한 차원이 높다고 다른 차원이 따라 움직이지 않는다.

### relevance — 이 회사의 손익에 얼마나 닿나 (0.0 ~ 1.0)

```
0.0  이름만 스쳐감 — 리포트 작성 증권사, 업종 나열, 인사·채용·게시판
0.3  회사 얘기지만 손익과 연결이 멀다 — 행사, MOU, 수상, 사회공헌
0.7  본업에 닿는다 — 신제품, 수주, 증설, 점유율, 경쟁구도
1.0  숫자가 직접 나온다 — 실적, 가이던스, 계약금액, 목표주가
```

수급·매매동향은 주가 얘기지만 손익에는 닿지 않는다 — 0.0~0.3.

### polarity — 방향만. 크기는 intensity가 받는다 (−1.0 ~ 1.0)

```
-1.0  명백히 악재
 0.0  방향 없음 또는 호악재가 맞물림
+1.0  명백히 호재
```

기준: 이 기사만 읽은 투자자가 주식을 더 사고 싶어지는가, 팔고 싶어지는가.
질문한 종목 기준으로 판단한다 — A가 B에 밀렸다는 기사는 A와 B의 부호가 다르다.

### intensity — 재무적 충격의 크기 (0.0 ~ 1.0)

```
0.0  방향은 있으나 금액으로 환산되지 않는다
0.3  단발성이거나 매출의 1% 미만 수준
0.7  분기 실적을 눈에 띄게 움직인다
1.0  연간 실적이나 사업 구조를 바꾼다
```

polarity와 독립이다. 큰 악재도 intensity는 1.0이다.

### uncertainty — 그 결과가 실제로 일어날지 (0.0 ~ 1.0)

```
0.0  이미 확정 — 발표된 실적, 체결된 계약, 집행된 처분
0.3  공시·계약은 됐으나 이행이 남음
0.7  전망·목표·계획 — 증권사 추정, 회사 가이던스
1.0  추측 — '검토 중', '알려졌다', 익명 소식통
```

### forwardness — 0=이미 반영된 과거, 1=미래 기대를 바꿈 (0.0 ~ 1.0)

```
0.0  이미 알려진 사실의 반복·정리 기사
0.3  지난 분기에 일어난 일의 확인
0.7  앞으로 몇 분기의 기대를 바꾼다
1.0  처음 나온 정보이고 중기 전망을 다시 짜게 한다
```

uncertainty와 다르다. 확정된 사실도 처음 알려졌다면 forwardness는 높다.

### rationale

40자 이하. 그 숫자를 고른 이유를 한 줄로. 기사에 있는 근거만 쓴다.

---

> `description` is truncated at 400 characters before it is sent
> (`src.llm.score.BODY_CHARS`), matching what `scripts/golden.py`'s
> `format_article` displayed while Ricky labelled. He scored from the truncated
> view, so the model must see the same text or the comparison is not
> like-for-like. Everything below the next heading is sent verbatim; keep notes
> above it.

## User message

`{ticker}` `{name}` 기준으로 아래 기사를 채점한다.

```
제목: {title}
본문: {description}
```
