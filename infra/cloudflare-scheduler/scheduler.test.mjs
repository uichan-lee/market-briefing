import assert from "node:assert/strict";
import test from "node:test";

import { dispatch, handleScheduled } from "./scheduler.mjs";

const env = { GITHUB_DISPATCH_TOKEN: "test-token" };

function response(status = 204, body = {}) {
  return new Response(status === 204 ? null : JSON.stringify(body), { status });
}

test("dispatch sends only ref and requested inputs to the GitHub workflow endpoint", async () => {
  const calls = [];
  const logs = [];
  const originalLog = console.log;
  console.log = (message) => logs.push(JSON.parse(message));
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return response();
  };

  let result;
  try {
    result = await dispatch(fetchImpl, env.GITHUB_DISPATCH_TOKEN, "report.yml", { run: "morning" });
  } finally {
    console.log = originalLog;
  }
  assert.equal(result.ok, true);
  assert.match(calls[0].url, /market-briefing\/actions\/workflows\/report.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main", inputs: { run: "morning" } });
  assert.equal(calls[0].init.headers.Authorization, "Bearer test-token");
  assert.deepEqual(logs, [{ event: "github_dispatch", workflow: "report.yml", http_status: 204 }]);
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
  const logs = [];
  const originalLog = console.log;
  console.log = (message) => logs.push(JSON.parse(message));
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

  try {
    await handleScheduled("15,25,40 * * * *", new Date("2026-09-07T03:25:00Z"), env, fetchImpl);
  } finally {
    console.log = originalLog;
  }
  assert.equal(calls.length, 2);
  assert.match(calls[1].url, /scheduler-watchdog.yml\/dispatches$/);
  assert.match(JSON.parse(calls[1].init.body).inputs.reason, /뉴스 수집/);
  assert.deepEqual(logs, [
    {
      event: "github_latest_run",
      workflow: "collect-news.yml",
      run_id: null,
      created_at: "2026-09-07T00:00:00Z",
      status: "completed",
      conclusion: "success",
    },
    { event: "github_dispatch", workflow: "scheduler-watchdog.yml", http_status: 204 },
  ]);
});

test("dispatch does not retry an ambiguous server failure", async () => {
  let attempts = 0;
  const fetchImpl = async () => {
    attempts += 1;
    return attempts === 1 ? response(503) : response();
  };
  const result = await dispatch(fetchImpl, env.GITHUB_DISPATCH_TOKEN, "collect-news.yml");
  assert.equal(result.ok, false);
  assert.equal(attempts, 1);
});

test("scheduled dispatch failure rejects without exposing the token", async () => {
  await assert.rejects(
    handleScheduled("17 7-23 * * *", new Date(), env, async () => response(503)),
    { message: "Scheduled GitHub request failed: HTTP 503" },
  );
});

test("scheduled dispatch failure includes GitHub's message without exposing the token", async () => {
  const requestError = "Resource not accessible by personal access token";
  await assert.rejects(
    handleScheduled("17 7-23 * * *", new Date(), env,
      async () => response(403, { message: `  ${requestError}\n`, token: env.GITHUB_DISPATCH_TOKEN })),
    { message: `Scheduled GitHub request failed: HTTP 403: ${requestError}` },
  );
});

test("dispatch does not retry a lost response", async () => {
  let attempts = 0;
  const result = await dispatch(async () => {
    attempts += 1;
    throw new TypeError("response lost");
  }, env.GITHUB_DISPATCH_TOKEN, "report.yml");
  assert.equal(result.ok, false);
  assert.equal(attempts, 1);
});

test("watchdog retries reads and alerts on unfinished runs at the check time", async () => {
  for (const status of ["queued", "in_progress", "waiting"]) {
    let reads = 0;
    const alerts = [];
    await handleScheduled("15,25,40 * * * *", new Date("2026-09-07T03:25:00Z"), env,
      async (url, init) => {
        if (init.method === "GET") {
          reads += 1;
          if (reads === 1) return response(503);
          return response(200, { workflow_runs: [
            { created_at: "2026-09-07T03:17:00Z", status },
          ] });
        }
        alerts.push(JSON.parse(init.body).inputs.reason);
        return response();
      });
    assert.equal(reads, 2);
    assert.equal(alerts.length, 1);
    assert.match(alerts[0], new RegExp(status));
  }
});

test("watchdog delivery failure rejects the scheduled task", async () => {
  await assert.rejects(handleScheduled("15,25,40 * * * *",
    new Date("2026-09-07T03:25:00Z"), env,
    async (url, init) => init.method === "GET"
      ? response(200, { workflow_runs: [] }) : response(403)),
  { message: "Scheduled GitHub request failed: HTTP 403" });
});

test("GitHub dispatches and watchdog reads identify the scheduler", async () => {
  const methods = [];
  await handleScheduled("15,25,40 * * * *", new Date("2026-09-23T08:25:00Z"), env,
    async (_url, init) => {
      assert.equal(init.headers["User-Agent"], "market-briefing-scheduler");
      methods.push(init.method);
      return init.method === "GET"
        ? response(200, { workflow_runs: [] }) : response();
    });
  assert.deepEqual(methods, ["GET", "POST"]);
});

test("plain-text GitHub rejection explains the missing header", async () => {
  const detail = "Request forbidden by administrative rules. Please make sure your request has a User-Agent header";
  await assert.rejects(handleScheduled("17 7-23 * * *", new Date(), env,
    async () => new Response(`\r\n${detail}\r\n`, { status: 403 })),
  { message: `Scheduled GitHub request failed: HTTP 403: ${detail}` });
});

test("GitHub errors redact the credential before logging or truncation", async () => {
  for (const reply of [
    response(403, { message: `Rejected ${env.GITHUB_DISPATCH_TOKEN}` }),
    new Response(`Rejected ${env.GITHUB_DISPATCH_TOKEN}`, { status: 403 }),
  ]) {
    await assert.rejects(handleScheduled("17 7-23 * * *", new Date(), env,
      async () => reply),
    { message: "Scheduled GitHub request failed: HTTP 403: Rejected [redacted]" });
  }
});
