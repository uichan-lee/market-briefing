import assert from "node:assert/strict";
import test from "node:test";

import { dispatch, handleScheduled } from "./scheduler.mjs";

const env = { GITHUB_DISPATCH_TOKEN: "test-token" };

function response(status = 204, body = {}) {
  return new Response(status === 204 ? null : JSON.stringify(body), { status });
}

test("dispatch sends only ref and requested inputs to the GitHub workflow endpoint", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return response();
  };

  const result = await dispatch(fetchImpl, env.GITHUB_DISPATCH_TOKEN, "report.yml", { run: "morning" });
  assert.equal(result.ok, true);
  assert.match(calls[0].url, /market-briefing\/actions\/workflows\/report.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main", inputs: { run: "morning" } });
  assert.equal(calls[0].init.headers.Authorization, "Bearer test-token");
});

test("each production cron dispatches its one intended workflow", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return response();
  };
  const now = new Date("2026-09-07T22:07:00Z");

  for (const cron of ["17,47 0-6 * * *", "17 7-23 * * *", "7 22 * * SUN-THU", "37 12 * * MON-FRI"]) {
    await handleScheduled(cron, now, env, fetchImpl);
  }

  assert.equal(calls.length, 4);
  assert.match(calls[0].url, /collect-news.yml/);
  assert.match(calls[1].url, /collect-news.yml/);
  assert.deepEqual(JSON.parse(calls[2].init.body).inputs, { run: "morning" });
  assert.deepEqual(JSON.parse(calls[3].init.body).inputs, { run: "evening" });
});

test("watchdog alerts when the latest news dispatch is stale", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (url.includes("/runs?")) {
      return response(200, {
        workflow_runs: [
          { created_at: "2026-09-07T00:00:00Z", status: "completed", conclusion: "success" },
        ],
      });
    }
    return response();
  };

  await handleScheduled("15,25,40 * * * *", new Date("2026-09-07T03:25:00Z"), env, fetchImpl);
  assert.equal(calls.length, 2);
  assert.match(calls[1].url, /scheduler-watchdog.yml\/dispatches$/);
  assert.match(JSON.parse(calls[1].init.body).inputs.reason, /뉴스 수집/);
});

test("transient GitHub failures are retried without exposing a token", async () => {
  let attempts = 0;
  const fetchImpl = async () => {
    attempts += 1;
    return attempts === 1 ? response(503) : response();
  };
  const result = await dispatch(fetchImpl, env.GITHUB_DISPATCH_TOKEN, "collect-news.yml");
  assert.equal(result.ok, true);
  assert.equal(attempts, 2);
});
