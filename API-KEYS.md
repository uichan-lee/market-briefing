# API-KEYS.md

Step-by-step issuance walkthrough for every credential in `.env.example`.

`MANUAL-TASKS.md` §1 is the checklist — *what* is needed and whether it blocks. This file is *how* to get each one, written for someone sitting at the signup form.

Screen labels are quoted verbatim in Korean, because that is what the UI actually says. Everything else is English, per the repository language convention.

> [!warning] Signup flows change
> Every procedure below was checked on 2026-08-03. Securities and data vendors reorganize their portals often. If a screen does not match what is written here, trust the screen and fix this file.

---

## Order of work

Four collectors — `kr_price`, `kr_news`, `macro`, `us_price` — are built and running. One credential remains outstanding.

| Credential | Status | Unblocks |
|---|---|---|
| **KRX Data Marketplace** | ✅ **held, verified 2026-08-04** | `kr_flow`, short interest, market cap, fundamentals — **55% of the rating weight**, now clear |
| **KIS** | ⬜ **outstanding** | real-time quotes (§3.1). Approval takes days, so start it early |
| ~~Naver~~ | ✖ not needed | outlet RSS replaced it — see §2 |
| DART | ✅ held | `kr_filings` |
| FRED | ✅ held | `macro` — already built and passing |
| SEC User-Agent | ✅ held | `us_filings` |
| **Alpaca** | ⬜ **outstanding** | `us_price` at watchlist scale — see §2b. Tiingo's 50/hour cannot serve 48 symbols with any headroom |
| Tiingo | ✅ held | `us_price` today, and the cross-check afterwards |
| SMTP | ✅ held | email delivery |

Only KIS remains, and it is the least urgent of the set: it adds real-time quotes on top of data pykrx already supplies. Start the application anyway, because its approval queue runs for days in the background.

> [!warning] Held locally is not the same as available to CI
> As of 2026-08-04 the repository has **no Actions secrets configured at all**. `collect-news.yml` has been running fine only because outlet RSS needs no credential. MANUAL-TASKS.md §1 has a loop that pushes every value from `.env` to `gh secret set` without printing any of them.

---

## 0. KRX Data Marketplace — resolved 2026-08-04

> [!tip] Registered, logged in, and verified against live endpoints
> `login_krx` returns `True`, and all six gated endpoints answer for 005930: investor net-buy by value and volume, short-interest balance, short volume, market cap, and PER/PBR fundamentals. `get_market_sector_classifications` also works and returns 943 KOSPI rows, which is what `scripts/config_helper.py` uses to resolve names to tickers.
>
> Two operational facts worth carrying forward:
>
> - **The session expires after 60 minutes.** `login_krx` reports 만료 시간 exactly one hour out. A short collector run never notices; a multi-year backfill must re-authenticate rather than assume one login covers the job.
> - **pykrx prints the login ID to stdout on every login.** It lands in Actions logs verbatim. The repository is private and an ID alone is not a credential, so this is a note rather than an incident — but check it before making the repository public. The password is never printed.
>
> The registration trap below is kept because it cost a day, and because the same failure would recur on any re-registration.

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

> [!warning] Do not register with Kakao or Naver social login
> pykrx performs a form login and needs an actual password, which a social account does not have. Worse, the recovery path is bad: withdrawing the social account and re-registering natively is blocked by "이미 사용중인 전화번호" — the phone number stays attached after withdrawal. Observed 2026-08-03.
>
> If this has already happened, the account is stuck in a state their own recovery flow cannot see: 회원가입 rejects the phone as **"이미 사용중인 전화번호"** while 아이디/비밀번호 찾기 on that same number answers **"관련 정보 없음"**. Observed 2026-08-03. Withdrawal appears to release the member record while leaving the phone number's uniqueness claim behind.
>
> Contacts, in the order worth trying:
>
> | Route | Detail | Why this order |
> |---|---|---|
> | **`krxdata@krx.co.kr`** | the Data Marketplace's own address, from the site footer | Written record, screenshots attach, and it goes to the team that owns the member table. Send it before calling |
> | 1577-0088 / 02-3774-9000 | KRX 대표번호, Seoul | Business hours. Ask for 정보사업 담당 — the switchboard does not own this system |
> | 051-662-2000 | Busan headquarters | Only if Seoul routes nowhere |
>
> Ask for **the withdrawn record to be purged so the number is freed**, and give both error messages verbatim — the contradiction between them is the evidence that this is their data inconsistency rather than a user mistake. Get a 접수번호.
>
> `openapi.krx.co.kr` is a separate registration system and worth trying first anyway; it may not share the member table that is stuck.

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

> [!danger] developers.naver.com no longer issues 검색 API access
> **The instructions above no longer work, and the classic key cannot be fixed.** Established 2026-08-03 by direct request; recorded here because the failure looks like an ordinary bad key and is not one.
>
> Symptom one — the classic key authenticates but has no API attached:
>
> ```
> GET https://openapi.naver.com/v1/search/news.json
> → 401 {"errorMessage":"Scopes are Empty : Authentication failed.","errorCode":"024"}
> ```
>
> Symptom two — 검색 cannot be added to the application. It is absent from the list when creating an app, appears later under **API 설정**, and submitting it returns:
>
> > 애플리케이션 설정 실패 — 신규로 등록할 수 없는 API가 선택되었습니다.
>
> The API moved to **NAVER API HUB** on NAVER Cloud Platform. The new gateway is live and rejects classic credentials outright, which rules out any header-level workaround:
>
> | Endpoint | Headers | Result |
> |---|---|---|
> | `naverapihub.apigw.ntruss.com/search/v1/news` | NCP | 401 `Invalid authentication information` |
> | `naverapihub.apigw.ntruss.com/search/v1/news` | classic | 401 `Authentication information are missing` |
> | `openapi.naver.com/v1/search/news.json` | classic | 401 `Scopes are Empty` |

### The migration — NOT the current path

> [!note] RSS was chosen instead; this section is retained as the fallback
> `config/news_feeds.yaml` now collects from 15 Korean outlets directly, free and with no account. Follow the steps below only if measurement shows RSS is insufficient — the golden set and bake-off are what would show that.

1. Sign up at **ncloud.com** and enable **NAVER API HUB → 검색**.
2. Issue an API key. It is an NCP key, unrelated to the developers.naver.com pair.
3. The request changes shape:

| | Classic (dead) | API Hub |
|---|---|---|
| URL | `https://openapi.naver.com/v1/search/news.json` | `https://naverapihub.apigw.ntruss.com/search/v1/news` |
| Auth headers | `X-Naver-Client-Id` / `X-Naver-Client-Secret` | `X-NCP-APIGW-API-KEY-ID` / `X-NCP-APIGW-API-KEY` |

Query parameters appear unchanged: `query`, `display` (1–100), `start` (1–1000), `sort` (`sim` \| `date`).

`.env` therefore needs new variable names once this is done. `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` are dead weight until then.

> [!warning] This is a decision, not just a signup
> NCP registers a **payment method at account creation**. API HUB is currently **한시적 무료** with paid conversion to be announced in advance, so a card sits on file against a service that is scheduled to start charging. That is a different commitment from the old free 25,000 calls/day, and worth deciding deliberately rather than clicking through.
>
### GDELT was evaluated and rejected — 2026-08-03

Measured, so nobody has to re-open it. Six GKG translingual files sampled across ~31 hours:

```
9,577 rows total   →   124 from .kr domains  (1.3%)
extrapolated ~1,984 Korean articles/day, ALL topics

Korean outlets present:
   hani.co.kr  zdnet.co.kr  wikitree.co.kr  newsway.co.kr
   etoday.co.kr  kbs.co.kr  seoul.co.kr  ecomedia.co.kr
```

Checked for 21 Korean financial outlets — 한국경제, 매일경제, 연합뉴스, 조선비즈, 이데일리, 서울경제, 머니투데이, 파이낸셜뉴스, 아시아경제, 헤럴드경제, 뉴시스, 더벨, 전자신문 among them. **All 21 absent.** The Korean panel GDELT does carry is a general-news daily, a tech site, and a celebrity-gossip site.

Two further notes, both recorded because they look like solvable problems and are not:

- Korean content lives in the **translingual** feed (`lastupdate-translation.txt`), not the main one. The main GKG feed contains zero `.kr` rows. Checking only the main feed would produce a falsely absolute "GDELT has no Korean news".
- The DOC query API returned **HTTP 429 on every attempt** from this machine, including a single cold request after backoff to four minutes. It is IP-range throttling, not request spacing. GitHub Actions runners are also cloud IPs, so the query API is a production risk regardless. The bulk file feed at `data.gdeltproject.org` is *not* throttled and downloads fine — the content simply is not there.

**Verdict: GDELT cannot supply `news_polarity`, `news_volume_z`, or the golden set.** Building the LLM stage on a panel that excludes every outlet reporting Korean earnings and contracts would measure the wrong thing well.

### Korean outlet RSS — free, and viable

Tested 2026-08-03. Exactly the outlets GDELT lacks:

| Feed | Items | `description` |
|---|---|---|
| 한국경제 증권 / 산업 | 50 each | empty — title only |
| 매일경제 증권 / 기업 | 50 each | ~102자 |
| 연합뉴스 경제 | 120 | ~83자 |
| 전자신문 | 30 | ~247자 |

All carry `title`, `link`, and a timezone-aware `pubDate`, which is what the look-ahead rule joins on. Sample headline pulled live: *"두산, SK실트론 잘 샀네 … 증권가, 목표가 잇단 상향"* — precisely the article class this project exists to score.

Three real costs, none fatal:

- **No search.** The firehose arrives per outlet and is filtered locally by `config/aliases.yaml` — which is what Stage 0 entity resolution already does, so the work is not new. It also removes dependence on Naver's search ranking.
- **No backfill, and a rollover risk Naver does not have.** A feed holds 50–120 items. If an outlet publishes more than that between polls, the excess is lost permanently. Two runs a day is not enough. Measured buffer spans run from 4.0 hours (한국경제 경제) to 101.6 (인포스탁), so collection is twice an hour through the KRX session and hourly otherwise.
- **Body text varies from empty to ~250 characters.** 한국경제 gives headline only. Scoring SPEC §6.2's five dimensions off a bare headline is materially weaker than off Naver's description passage, unless article bodies are fetched separately.

Feeds that failed and would need replacing: 서울경제 (404), 헤럴드경제 (malformed XML), 이데일리 (connection reset).

> [!important] Naver search cannot be backfilled
> The search API caps both results per query and paging depth. Historical news cannot be retrieved in bulk — the corpus only accumulates from the day collection starts.
>
> This is why `MANUAL-TASKS.md` §4 places the golden set *after* a week of collection: there is nothing to sample from before then. It also means every day the collector is not running is a day permanently missing from the dataset.

---

## 2b. Alpaca — US market data

> [!warning] One check decides whether this source is usable at all
> Alpaca's documentation contradicts itself about the free plan. The plan
> comparison lists Basic as **IEX only** for equities; the Market Data FAQ says
> a *historical* query needs only an `end` at least 15 minutes old to reach
> **SIP**. Both were read on 2026-08-04.
>
> The gap is not cosmetic. IEX is one exchange, SIP is the consolidated tape of
> all of them, and Alpaca's own FAQ gives AAPL on 2023-09-29 as **923,134
> shares on IEX against 51,861,083 on SIP** — a factor of 56. IEX-only bars
> would give this project the wrong volume for every US name and a close that
> is one venue's last print rather than the official close.
>
> **Settle it empirically, not by reading harder:**
>
> ```bash
> set -a; source .env; set +a
> uv run python -c "from src.collectors.us_price_alpaca import probe_feed; print(probe_feed())"
> ```
>
> It fetches SPY for 2024-01-02, the date the fourth check pins at a close of
> **472.65** already confirmed against Yahoo Finance. A matching close with
> volume in the tens of millions means consolidated data and the switch goes
> ahead. A volume near a million means IEX, and this source cannot be used on
> the free plan whatever the `feed` parameter said.

### Why the source changed

Tiingo works and is not broken. It stopped fitting when the US watchlist reached
40 names:

| | Tiingo free | Alpaca free |
|---|---|---|
| Symbols per request | **1** | comma-separated list |
| Rate limit | **50 / hour** | 200 / minute |
| Cost of one 48-symbol run | **48 requests** | 1 per page |

48 of 50 leaves no headroom. A single retry pushes the run over, and a 429
mid-run leaves the rest of the watchlist without data. This is a request-shape
problem, not a data-quality one — which is exactly why the feed question above
has to be answered before the switch is worth making.

### Registering

`alpaca.markets` → Sign up → Home → **Generate API Keys**.

- **Paper trading keys are sufficient.** This project never places an order, and
  CLAUDE.md rule 2 forbids writing code that could. Paper keys make that
  structural rather than a matter of discipline.
- The secret is shown **once**. Copy it immediately; regenerating invalidates the
  previous pair.
- No card is required for the Basic plan.

```
ALPACA_API_KEY_ID=
ALPACA_API_SECRET_KEY=
```

Push both to Actions secrets afterwards — MANUAL-TASKS.md §1 has the loop.

> [!note] Tiingo is kept, not retired
> It stays as the collector in use until the probe passes, and afterwards as a
> second opinion: two independent sources agreeing on the pinned SPY close is a
> stronger guarantee than either alone. `TIINGO_API_KEY` stays in `.env`.

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
