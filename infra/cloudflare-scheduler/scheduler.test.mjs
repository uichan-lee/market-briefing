import assert from "node:assert/strict";
import test from "node:test";
import { dispatch, handleScheduled } from "./scheduler.mjs";

const env = { GITHUB_DISPATCH_TOKEN: "test-token" };
const now = new Date("2026-09-28T20:25:00Z");
const cron = "10,15,25,40,55 * * * *";
function response(status = 204, body = {}) {
  return new Response(status === 204 ? null : JSON.stringify(body), { status });
}
function run(overrides = {}) {
  return { id: 36478113582, created_at: "2026-09-28T20:17:15Z",
    head_branch: "main", event: "workflow_dispatch",
    status: "completed", conclusion: "success", ...overrides };
}
async function check(replies, at = now) {
  const calls = [], logs = [];
  const original = console.log;
  console.log = message => logs.push(JSON.parse(message));
  let index = 0;
  try {
    await handleScheduled(cron, at, env, async (url, init) => {
      calls.push({ url, init });
      if (init.method === "POST") return response();
      assert.ok(index < replies.length, "unexpected additional read");
      const reply = replies[index++];
      if (reply instanceof Error) throw reply;
      return reply instanceof Response ? reply : response(200, { workflow_runs: reply });
    });
  } finally {
    console.log = original;
  }
  return { calls, logs, alerts: calls.filter(c => c.init.method === "POST").map(c => JSON.parse(c.init.body).inputs) };
}

test("dispatch preserves ref, inputs, identity headers and one POST", async () => {
  const calls = [];
  await dispatch(async (url, init) => { calls.push({url, init}); return response(); },
    env.GITHUB_DISPATCH_TOKEN, "report.yml", {run: "morning"});
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /report.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(calls[0].init.body), {ref: "main", inputs: {run: "morning"}});
  assert.equal(calls[0].init.headers.Authorization, "Bearer test-token");
  assert.equal(calls[0].init.headers["User-Agent"], "market-briefing-scheduler");
});
test("all production crons retain their single intended dispatch", async () => {
  const calls = [];
  for (const value of ["17,47 0-6 * * *", "17 7-23 * * *", "7 22 * * SUN-THU", "37 12 * * MON-FRI"]) {
    await handleScheduled(value, now, env, async (url, init) => {calls.push({url, init}); return response();});
  }
  assert.equal(calls.length, 4);
  assert.match(calls[0].url, /collect-news.yml/);
  assert.match(calls[1].url, /collect-news.yml/);
  assert.deepEqual(JSON.parse(calls[2].init.body).inputs, {run:"morning"});
  assert.deepEqual(JSON.parse(calls[3].init.body).inputs, {run:"evening"});
});
test("eight-minute-old success suppresses alert and records a healthy decision", async () => {
  const result = await check([[run()]]);
  assert.equal(result.alerts.length, 0);
  const query = new URL(result.calls[0].url).searchParams;
  assert.equal(query.get("branch"), "main");
  assert.equal(query.get("event"), "workflow_dispatch");
  assert.equal(query.get("per_page"), "20");
  assert.equal(result.calls[0].init.cache, "no-store");
  assert.equal(result.logs.at(-1).decision, "healthy");
  assert.equal(result.logs[0].run_id, 36478113582);
  assert.equal(result.logs[0].age_minutes, 7.75);
});
test("unsorted list selects latest creation, not first or latest successful run", async () => {
  const failed = run({id: 42, created_at:"2026-09-28T20:18:00Z", conclusion:"failure"});
  const result = await check([[run(), failed], [run(), failed]]);
  assert.match(result.alerts[0].reason, /실패.*failure/);
  assert.match(result.alerts[0].details, /runs\/42/);
});
test("stale first response is confirmed with created filter and fresh result suppresses alert", async () => {
  const result = await check([[run({created_at:"2026-09-28T10:00:00Z"})], [run()]]);
  assert.equal(result.alerts.length, 0);
  assert.equal(new URL(result.calls[1].url).searchParams.get("created"), ">=2026-09-28T18:25:00.000Z");
  assert.equal(result.logs.at(-1).initial_decision, "stale");
  assert.equal(result.logs.at(-1).decision, "healthy");
  assert.ok(result.calls.every(c => c.init.cache === "no-store"));
});
test("real gap alerts with both observations and threshold", async () => {
  const result = await check([[run({created_at:"2026-09-28T10:00:00Z"})], []]);
  assert.equal(result.alerts.length, 1);
  assert.match(result.alerts[0].reason, /120분.*기록 없음/);
  assert.match(result.alerts[0].details, /initial: run=36478113582/);
  assert.match(result.alerts[0].details, /confirmation: run=미확인/);
  assert.match(result.alerts[0].details, /threshold_minutes=120/);
});
test("missing list then current run recovers; twice empty alerts", async () => {
  assert.equal((await check([[], [run()]])).alerts.length, 0);
  assert.match((await check([[], []])).alerts[0].reason, /기록 없음/);
});
test("exactly 120 minutes is healthy; one millisecond older requires confirmation", async () => {
  assert.equal((await check([[run({created_at:"2026-09-28T18:25:00Z"})]])).alerts.length, 0);
  assert.equal((await check([[run({created_at:"2026-09-28T18:24:59.999Z"})], []])).alerts.length, 1);
});
test("unfinished statuses defer only within the news grace period", async () => {
  for (const status of ["queued", "in_progress", "waiting", "requested", "pending"]) {
    assert.equal((await check([[run({status, conclusion:null})], [run()]])).alerts.length, 0);
    const result = await check([[run({status, conclusion:null})], [run({status, conclusion:null})]]);
    assert.equal(result.alerts.length, 0);
    assert.equal(result.logs.at(-1).decision, "pending");
  }
});
test("malformed JSON, missing list, null and invalid timestamps fail closed", async () => {
  for (const first of [new Response("{"), response(200, {}), [null],
    [run({created_at:"nonsense"})], [run({created_at:"2026-09-28T20:17:00"})],
    [run({created_at:"2026-09-28T20:26:00Z"})], [run({id:-1})],
    [run({head_branch:"other"})], [run({event:"push"})],
    [run({status:"unrecognized"})], [run({conclusion:null})]]) {
    const result = await check([first, response(200, {})]);
    assert.match(result.alerts[0].reason, /확인 실패/);
    assert.doesNotMatch(result.alerts[0].reason, /초과함/);
  }
});
test("confirmation query returning out-of-window rows is unknown, not confirmed stale", async () => {
  const old = run({created_at:"2026-09-28T10:00:00Z"});
  const result = await check([[old], [old]]);
  assert.match(result.alerts[0].reason, /확인 실패/);
});
test("failed revalidation reports unknown rather than confirmed outage", async () => {
  const result = await check([[run({created_at:"2026-09-28T10:00:00Z"})], response(403, {message:"forbidden"})]);
  assert.match(result.alerts[0].reason, /미확인/);
  assert.match(result.alerts[0].details, /HTTP 403/);
});
test("GET retries 429 and 5xx, never adds POST retry", async () => {
  const result = await check([response(429), response(503), [run()]]);
  assert.equal(result.calls.length, 3);
  assert.equal(result.alerts.length, 0);
});
test("exhausted transport retries are revalidated and surfaced", async () => {
  const result = await check(Array.from({length:6}, () => new TypeError("unreachable")));
  assert.match(result.alerts[0].reason, /확인 실패/);
  assert.equal(result.calls.length, 7);
});
test("HTTP errors cannot leak token in structured logs or alert details", async () => {
  const result = await check([response(403, {message:"bad test-token"}), new Response("bad test-token", {status:403})]);
  assert.doesNotMatch(JSON.stringify(result.logs), /test-token/);
  assert.doesNotMatch(JSON.stringify(result.alerts), /test-token/);
  assert.match(result.alerts[0].details, /redacted/);
});
test("lost POST response and 503 are never retried", async () => {
  for (const error of [false, true]) {
    let calls = 0;
    const result = await dispatch(async () => {calls++; if(error) throw new TypeError("lost"); return response(503);},
      env.GITHUB_DISPATCH_TOKEN, "report.yml");
    assert.equal(result.ok, false);
    assert.equal(calls, 1);
  }
});
test("scheduled dispatch and alert dispatch failure reject", async () => {
  await assert.rejects(handleScheduled("17 7-23 * * *", now, env, async () => response(503)),
    /HTTP 503/);
  await assert.rejects(handleScheduled(cron, now, env, async (_url, init) =>
    init.method === "GET" ? response(200, {workflow_runs:[]}) : response(403)), /HTTP 403/);
});
test("morning and evening watchdog thresholds and routing stay unchanged", async () => {
  const morning = await check([[run({created_at:"2026-09-28T23:17:00Z"})], [], []], new Date("2026-09-28T23:40:00Z"));
  assert.match(morning.alerts[0].reason, /아침 리포트/);
  assert.match(morning.alerts[0].details, /threshold_minutes=120/);
  const evening = await check([[], []], new Date("2026-09-28T13:15:00Z"));
  assert.match(evening.alerts[0].reason, /저녁 리포트/);
  assert.match(evening.alerts[0].details, /threshold_minutes=40/);
});
test("unrelated watchdog slots do not call GitHub", async () => {
  const result = await check([], new Date("2026-09-28T20:15:00Z"));
  assert.equal(result.calls.length, 0);
});
test("plain text rejection and token-bearing errors retain safe diagnosis", async () => {
  await assert.rejects(handleScheduled("17 7-23 * * *", now, env,
    async () => new Response("User-Agent required; test-token", {status:403})),
    /HTTP 403: User-Agent required; \[redacted\]/);
});


test("ten-minute grace boundary is inclusive; one millisecond later alerts", async () => {
  const pending = run({created_at:"2026-09-28T20:15:00Z", status:"in_progress", conclusion:null});
  assert.equal((await check([[pending], [pending]])).alerts.length, 0);
  const old = {...pending, created_at:"2026-09-28T20:14:59.999Z"};
  const result = await check([[old], [old]]);
  assert.match(result.alerts[0].reason, /미완료/);
  assert.match(result.alerts[0].details, /pending_grace_minutes=10/);
});
test("follow-up catches the actual cancelled run after a deferred check", async () => {
  const pending = run({id:36829488727, created_at:"2026-10-01T07:17:39Z", status:"in_progress", conclusion:null});
  assert.equal((await check([[pending], [pending]], new Date("2026-10-01T07:25:27Z"))).alerts.length, 0);
  const cancelled = {...pending, status:"completed", conclusion:"cancelled"};
  const result = await check([[cancelled], [cancelled]], new Date("2026-10-01T07:40:00Z"));
  assert.match(result.alerts[0].reason, /실패.*cancelled/);
});
test("follow-up checks completion and does not mistake a stuck queue for health", async () => {
  const old = run({created_at:"2026-09-28T20:17:00Z", status:"queued", conclusion:null});
  const result = await check([[old], [old]], new Date("2026-09-28T20:40:00Z"));
  assert.match(result.alerts[0].reason, /미완료.*queued/);
  const completed = {...old, status:"completed", conclusion:"success"};
  assert.equal((await check([[completed]], new Date("2026-09-28T20:40:00Z"))).alerts.length, 0);
});
test("failed completed run has no grace and remains visible despite an older success", async () => {
  const failed = run({id:51, conclusion:"timed_out"});
  const older = run({id:50, created_at:"2026-09-28T20:00:00Z"});
  const result = await check([[older, failed], [older, failed]]);
  assert.match(result.alerts[0].reason, /실패.*timed_out/);
});
test("report completion still has no news grace period", async () => {
  const pending = run({created_at:"2026-09-28T13:10:00Z", status:"in_progress", conclusion:null});
  const result = await check([[pending], [pending]], new Date("2026-09-28T13:15:00Z"));
  assert.match(result.alerts[0].reason, /저녁 리포트.*미완료/);
  assert.match(result.alerts[0].details, /pending_grace_minutes=0/);
});

test("half-hour poll is checked before the next hourly success hides cancellation", async () => {
  const pending = run({id:61, created_at:"2026-10-01T00:47:30Z", status:"in_progress", conclusion:null});
  const first = await check([[pending], [pending]], new Date("2026-10-01T00:55:00Z"));
  assert.equal(first.alerts.length, 0);
  assert.equal(first.logs.at(-1).decision, "pending");
  const cancelled = {...pending, status:"completed", conclusion:"cancelled"};
  const followup = await check([[cancelled], [cancelled]], new Date("2026-10-01T01:10:00Z"));
  assert.match(followup.alerts[0].reason, /실패.*cancelled/);
  assert.match(followup.alerts[0].details, /run=61/);
  const laterSuccess = run({id:62, created_at:"2026-10-01T01:17:30Z"});
  const recovered = await check([[cancelled, laterSuccess]], new Date("2026-10-01T01:25:00Z"));
  assert.equal(recovered.alerts.length, 0);
});

test("half-hour checks cover the final :47 poll without adding off-hours news reads", async () => {
  const success = run({created_at:"2026-10-01T06:47:30Z"});
  assert.equal((await check([[success]], new Date("2026-10-01T06:55:00Z"))).logs.at(-1).decision, "healthy");
  assert.equal((await check([[success]], new Date("2026-10-01T07:10:00Z"))).logs.at(-1).decision, "healthy");
  for (const at of ["2026-10-01T00:10:00Z", "2026-10-01T07:55:00Z", "2026-10-01T08:10:00Z"]) {
    assert.equal((await check([], new Date(at))).calls.length, 0);
  }
});
