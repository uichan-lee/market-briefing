# API-KEYS.md

Step-by-step issuance walkthrough for every credential in `.env.example`.

`MANUAL-TASKS.md` §1 is the checklist — *what* is needed and whether it blocks. This file is *how* to get each one, written for someone sitting at the signup form.

Screen labels are quoted verbatim in Korean, because that is what the UI actually says. Everything else is English, per the repository language convention.

> [!warning] Signup flows change
> Every procedure below was checked on 2026-08-03. Securities and data vendors reorganize their portals often. If a screen does not match what is written here, trust the screen and fix this file.

---

## Order of work

Do **not** wait for all keys before writing code. The first collector needs none of them.

| # | Credential | Issue time | Unblocks | Do it |
|---|---|---|---|---|
| 0 | **KRX Data Marketplace** | minutes | `kr_flow`, short interest, market cap, fundamentals — **55% of the rating weight** | **first** |
| 1 | **KIS** | **days** — approval is not instant | real-time quotes (§3.1) | **first, because of the wait** |
| 2 | Naver | instant | `kr_news` → the entire news pipeline | today |
| 3 | DART | instant | `kr_filings` | today |
| 4 | FRED | instant | `macro` | today |
| 5 | SEC User-Agent | not an issuance | `us_filings` | today, 30 seconds |
| 6 | Alpaca *or* Tiingo | instant | `us_price` | later — US comes after KR is stable |
| 7 | SMTP | ~5 min | email delivery | later |

Everything except KIS is same-day. Start the KIS application, then do the rest while it is pending.

---

## 0. KRX Data Marketplace — the one that actually blocks the rating

> [!danger] This was not required when this document was first written
> The old 정보데이터시스템 let anyone query without an account. It has been replaced by the members-only **KRX Data Marketplace**, and login is mandatory. Verified 2026-08-03 by direct request: `data.krx.co.kr` returns **HTTP 400 with the body `LOGOUT`**.

**What still works without it:** daily OHLCV only. pykrx serves that through a Naver fallback, which is why `kr_price` is already built and passing.

**What does not:** net buying by investor type, short-interest balance, market cap, fundamentals.

That is not a minor gap. Those four supply **55% of the §2.2⑥ rating weight**:

| Feature | Weight | Status |
|---|---|---|
| `foreign_flow_5d` | 0.30 | blocked — SPEC §3.1 calls this the project's structural edge |
| `inst_flow_5d` | 0.15 | blocked |
| `short_ratio` | −0.10 | blocked |
| `valuation_band` | 0.05 | blocked |
| `news_polarity`, `rel_strength_20d`, `rev_4w` | 0.50 | available |

The surviving 45% is below `min_weight_coverage: 0.5`, so **every ticker would be forced to 관망**. The pipeline would run, publish, and say nothing.

### Registering

data.krx.co.kr → 회원가입

- Free. Data queries remain free; the change was to stop unauthenticated bulk scraping.
- Naver/Kakao social login is offered, but **register with a native ID and password.** pykrx performs a form login and needs an actual password; a social account has none it can use.

```
KRX_ID=
KRX_PW=
```

pykrx ≥ 1.2.8 reads these two environment variables and logs in automatically. Older pykrx has no login support at all — and 1.0.51, which is what dependency resolvers pick when pandas 3 is present, fails to import on Python 3.13 entirely.

> [!note] There is also an official API
> `openapi.krx.co.kr` is a separate, documented KRX Open API requiring an auth key with administrator approval. It is the cleaner long-term answer than scraping, and worth evaluating if the Marketplace login proves unreliable in CI. Not pursued yet.

---

## 1. KIS Open API — 한국투자증권

**Prerequisite:** a 한국투자증권 account. Ricky has this as of 2026-08-03.

### 1.1 The account question, answered

The application form says:

> 모의계좌
> 한국투자 홈페이지 혹은 MTS에서 모의투자 서비스 신청 후 발급받은 모의계좌번호로 API 신청을 하셔야 합니다.

So the 모의계좌 is a **separate signup that must happen first**. The API form does not create it; it only accepts a mock account number that already exists.

The form also says one application covers up to two accounts, with more added afterward:

> 한 번에 2개의 계좌까지 API 신청 가능합니다. 다계좌 API 신청을 원하실 경우, 신청하기 완료 후 신청정보 페이지에서 추가신청하기 기능을 이용하시기 바랍니다.

**Recommended sequence:**

1. Complete the application now with the **종합계좌** (real account) only. Do not abandon the form to go set up mock trading first.
2. Separately apply for 모의투자 (§1.3 below).
3. Return to KIS Developers → 신청정보 → **추가신청하기**, and add the 모의계좌.

This yields two independent key pairs. There is no cost to holding both, and §1.4 explains why both are worth having.

### 1.2 API그룹 — the one selection that matters

The form presents a list of API groups. **This selection is the strongest available enforcement of `CLAUDE.md` absolute rule 2** (read-only endpoints only), because a group that was never requested is one the code cannot reach regardless of what the code says.

**Select:**

| Group | Why |
|---|---|
| `OAuth인증` | mandatory — issues the access token every other call needs |
| `[국내주식] 기본시세` | 현재가, 일/주/월봉 — the core quote data |
| `[국내주식] 종목정보` | ticker metadata |

**Consider adding** (harmless, all read-only, useful later):

- `[국내주식] 시세분석`
- `[국내주식] 순위분석`
- `[국내주식] 업종/기타`
- `[해외주식] 기본시세` — only when the US side starts

**Do not select:**

- `[국내주식] 주문/계좌`
- `[해외주식] 주문/계좌`
- `[국내선물옵션] 주문/계좌`, `[해외선물옵션] 주문/계좌`, `[장내채권] 주문/계좌`
- every `실시간시세` group — WebSocket streaming, which this project does not use (SPEC §2.1 is a scheduled batch, not a live feed)

> [!danger] 잔고조회 lives inside 주문/계좌
> SPEC §3.1 lists "balances" as a KIS use. Balance inquiry is grouped together with order placement under `주문/계좌`, so there is no way to request read-only balance access alone.
>
> Stage 1 does not need it. The shadow portfolio (SPEC §2.2⑦) is computed from the briefing's own ratings, not from a real balance. **Leave 주문/계좌 unselected.** If a real balance is ever genuinely required, that is a decision to revisit deliberately — and it would need `CLAUDE.md` rule 2 revisited with it, not quietly worked around.
>
> `# UNVERIFIED:` whether KIS enforces the group selection server-side, or whether it is only a declaration of intent. If it is merely declarative, the mock-account key (§1.4) is the real safety mechanism and this one is defense in depth.

### 1.3 Applying for 모의투자

한국투자 홈페이지 → 트레이딩 → 모의투자 → 주식/선물옵션 모의투자 → 모의투자안내 → 신청

- Membership signup plus registering a 모의투자 접속 비밀번호, email, and phone number.
- Mock trading is organized into 리그 (leagues) with participation periods — pick a stock league, not futures/options.
- The order password for a mock account is arbitrary; any string of one or more characters works.
- Up to 2 mock accounts.

> Reported limitation: mock trading is not available through the mobile app; HTS (eFriend Plus) is the supported client. This constrains manual use of the mock account but not API access, which is what this project needs.

### 1.4 Which key should `.env` hold?

This reverses the guidance previously given in `MANUAL-TASKS.md` §1. The reasoning:

| | 실전 (real) | 모의 (paper) |
|---|---|---|
| Rate limit | 20 calls/sec | 1 call/sec |
| Quote data | live | may differ from live |
| Accidental order risk | real money | none — no real assets exist |

For this project, **the rate limit difference is irrelevant** — 15 tickers polled once daily is 15 calls. So the paper account costs nothing that matters and removes the failure mode entirely.

**Put the 모의 key in `.env` as the default.** Keep the 실전 key available for the case where a needed quote endpoint turns out to be unsupported in the mock environment.

> `# UNVERIFIED:` which endpoints the mock environment actually serves. Reports exist of specific functions being unsupported there (order-inquiry is one), but no authoritative list of mock-unsupported quote endpoints was found. Determine this empirically when `src/collectors/` first calls KIS, and record the finding here.

### 1.5 Operational notes for whoever writes the collector

- **Access token:** valid 24 hours, and issuance is rate-limited to **once per minute**. The token must be cached to disk and reused — requesting one per API call will fail. Reissue on a schedule (roughly every 6 hours is the commonly used cadence), not on demand.
- **Per-second limits:** 20/sec real, 1/sec paper. Exceeding it returns error `EGW00201` (`초당 거래건수 초과`).
- **A stricter limit may apply to new customers.** The portal carries a notice titled `[중요] 한국투자증권 Open API 신규 고객 초당 호출 제한 안내`. Its actual value was not obtained. `# UNVERIFIED:` read the notice at apiportal.koreainvestment.com and record the number before sizing any batch loop.
- **Environments are separate hosts,** selected in client libraries by `prod` vs `vps`. Real and paper credentials are not interchangeable across them.
- Endpoints are addressed by a transaction ID (`tr_id`) — e.g. `FHKST01010100` for 주식현재가 시세. These are not guessable; read the documentation for each.

### 1.6 Two clauses on the form to remember

> 이용 기간 2026.08.03 ~ 2027.08.02 — 신청한 기간 중에만 사용 가능하며, 이용기간은 신청일로 부터 1년 입니다.

**Expires 2027-08-02.** Renew before then or the pipeline breaks on a date nobody is watching.

> 3개월간 거래내역이 없을 경우 서비스 접속이 차단될 수 있습니다. (재신청후 사용가능)

**This project never places a trade, by design.** If 거래내역 means order history rather than API call history, access will be cut off at the three-month mark and the remedy is reapplication. This is a live risk, not a hypothetical one — a read-only consumer is exactly the case the clause was not written for.

Ask KIS support what 거래내역 means here. If it means orders, the mock account is the answer: a trade there is free and satisfies the clause without real exposure.

### 1.7 What is issued

A separate `APP Key` + `APP Secret` pair per account. A temporary password arrives by 알림톡; change it at apiportal.koreainvestment.com.

```
KIS_APP_KEY=
KIS_APP_SECRET=
```

**These keys permit trading within the account.** Treat them exactly as account credentials.

---

## 2. Naver Developers — news

developers.naver.com → 로그인 → Application → **애플리케이션 등록**

| Field | Value |
|---|---|
| 애플리케이션 이름 | anything (`market-briefing`) |
| 사용 API | **검색** — this must be checked |
| 환경 추가 | WEB 설정; 서비스 URL `http://localhost` is fine (search API is called server-side, so the URL is never used) |

```
NAVER_CLIENT_ID=
NAVER_CLIENT_SECRET=
```

> [!important] Naver search cannot be backfilled
> The search API caps both results per query and paging depth. Historical news cannot be retrieved in bulk — the corpus only accumulates from the day collection starts.
>
> This is why `MANUAL-TASKS.md` §4 places the golden set *after* a week of collection: there is nothing to sample from before then. It also means every day the collector is not running is a day permanently missing from the dataset.

---

## 3. DART OpenAPI — filings

opendart.fss.or.kr → 인증키 신청/관리 → **인증키 신청**

Email verification, then instant issue. A 40-character string.

```
DART_API_KEY=
```

A daily call limit applies per key. Fine for 15 tickers; a large backfill will hit it.

---

## 4. FRED — macro

fred.stlouisfed.org → My Account (top right) → create account → **API Keys** → Request API Key

Instant, free, generous limits.

```
FRED_API_KEY=
```

---

## 5. SEC EDGAR — not a key

No credential. SEC requires a descriptive `User-Agent` containing a contact email and **refuses requests without one**.

```
SEC_USER_AGENT=market-briefing <contact email>
```

Fill this in immediately — it needs no signup.

---

## 6. Alpaca or Tiingo — US quotes

Pick one (SPEC §3.2). What this project needs from the US side is **daily OHLCV**.

- **Tiingo** — tiingo.com, free tier centered on EOD daily bars. Account → API Token.
- **Alpaca** — alpaca.markets. The free tier's market data is understood to be limited to the **IEX feed**, which covers only a fraction of consolidated volume, so its closing prices can differ from the official close.

**Tiingo is the better fit** on that basis. However, free-tier terms were not verified as of 2026-08-03, and SPEC §3 explicitly warns that they change often — read the current signup page and go with whichever actually offers EOD daily bars.

```
TIINGO_API_KEY=
# leave ALPACA_* blank
```

**Decided 2026-08-03: Tiingo.** Recorded in SPEC §3.2; `ALPACA_*` stay blank.

---

## 7. SMTP — email delivery

**Create a dedicated account.** This credential goes into a GitHub Actions secret, and a CI secret must never grant access to a personal mailbox.

Gmail:

1. Create a new Google account.
2. Enable 2-Step Verification — the app-password menu does not appear without it.
3. myaccount.google.com/apppasswords → generate → 16 characters.

```
SMTP_PASSWORD=
```

The recipient address is **not** a secret and does not belong in `.env`. It goes in `config/delivery.yaml` under the email channel's `to:`, which is currently empty.

---

## 8. LLM provider keys

Not yet in `.env.example`. `config/models.yaml` specifies `provider: anthropic`, and the SPEC §7.4 bake-off compares providers, so at least one and probably several will be needed.

Deferred until `src/llm/adapter.py` is written, so the variable names match what the adapter reads rather than being guessed in advance.

---

## Handling

- **Never paste a key into a chat session with any AI tool, including Claude Code.** If a key needs checking, the check reads `.env` and reports presence/format only — never the value.
- `.gitignore` covers `.env`. Confirm: `git check-ignore -v .env`
- `.env.example` holds names and comments only, and is committed deliberately.
- GitHub repository secrets are set later, when the Actions workflow is written. Not needed to run locally.

---

## Sources

Checked 2026-08-03.

- [KIS Developers portal](https://apiportal.koreainvestment.com/intro) — API groups, portal notices
- [KIS Open API service guide](https://apiportal.koreainvestment.com/about-howto) — application flow
- [koreainvestment/open-trading-api](https://github.com/koreainvestment/open-trading-api) — official samples; `prod`/`vps` environments, mock-account call limits, `EGW00201`
- [한국투자증권 모의투자안내](https://securities.koreainvestment.com/main/research/virtual/_static/TF07da010000.jsp) — mock trading signup
- [모의거래 및 이수제도 안내](https://m.koreainvestment.com/main/research/virtual/_static/TF07db010000.jsp)
- [파이썬을 이용한 한국/미국 주식 자동매매 시스템 (wikidocs)](https://wikidocs.net/165188) — application walkthrough, selecting 종합계좌 and 모의계좌
- [초당 20건 제한 해결법](https://tgparkk.github.io/robotrader/2025/10/09/robotrader-1-70stocks-problem.html) — rate-limit behavior in practice
