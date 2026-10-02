/**
 * Dispatch-only scheduler for market-briefing.
 *
 * It owns no market data and has no repository-content permission. The sole
 * secret is a fine-grained GitHub token restricted to Actions: write on this
 * repository. Do not replace it with a classic `repo` token.
 */

const OWNER = "uichan-lee";
const REPOSITORY = "market-briefing";
const REF = "main";
const API = `https://api.github.com/repos/${OWNER}/${REPOSITORY}/actions/workflows`;
const COLLECT = "collect-news.yml";
const REPORT = "report.yml";
const WATCHDOG = "scheduler-watchdog.yml";
const RETRIES = 3;
// News jobs have a ten-minute execution limit. Recheck at :40 so deferral at
// :25 cannot conceal a subsequent cancellation or stuck queue.
const NEWS_PENDING_GRACE_MINUTES = 10;

function headers(token) {
  return {
    // GitHub rejects requests without an application identity, even with a valid PAT.
    "User-Agent": "market-briefing-scheduler",
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

async function githubFailureDetail(response, token) {
  if (!response) return "HTTP unknown";

  let message;
  try {
    const text = await response.clone().text();
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      // GitHub's missing-User-Agent rejection is plain text, not JSON.
      body = { message: text };
    }
    if (typeof body?.message === "string") {
      message = body.message.replaceAll(token, "[redacted]").replace(/\s+/g, " ").trim().slice(0, 200);
    }
  } catch {
    message = undefined;
  }
  return `HTTP ${response.status}${message ? `: ${message}` : ""}`;
}

async function github(fetchImpl, token, path, init) {
  let response;
  // A dispatch may already have been accepted when its response is lost.
  // Retry only reads; POST retries can duplicate scoring and delivery.
  const attempts = init.method === "GET" ? RETRIES : 1;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      response = await fetchImpl(`${API}/${path}`, {
        ...init,
        ...(init.method === "GET" ? { cache: "no-store" } : {}),
        headers: { ...headers(token), ...(init.headers || {}) },
      });
    } catch (error) {
      if (attempt === attempts) return { ok: false, detail: `network ${error.name}` };
      continue;
    }
    if (response.ok) return { ok: true, response };
    if (response.status < 500 && response.status !== 429) {
      return { ok: false, detail: await githubFailureDetail(response, token) };
    }
  }
  return { ok: false, detail: await githubFailureDetail(response, token) };
}

export async function dispatch(fetchImpl, token, workflow, inputs = {}) {
  const result = await github(fetchImpl, token, `${workflow}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: REF, inputs }),
  });
  if (result.ok) {
    console.log(JSON.stringify({
      event: "github_dispatch",
      workflow,
      http_status: result.response.status,
    }));
  }
  return result;
}

async function latestRun(fetchImpl, token, workflow, now, since) {
  const query = new URLSearchParams({ event: "workflow_dispatch", branch: REF, per_page: "20" });
  if (since) query.set("created", `>=${since.toISOString()}`);
  const result = await github(fetchImpl, token, `${workflow}/runs?${query}`, {
    method: "GET",
  });
  if (!result.ok) return result;
  let body;
  try {
    body = await result.response.json();
  } catch {
    return { ok: false, detail: "invalid JSON" };
  }
  if (!Array.isArray(body?.workflow_runs)) return { ok: false, detail: "invalid run list" };
  const statuses = new Set(["completed", "queued", "in_progress", "waiting", "requested", "pending"]);
  const conclusions = new Set(["success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale", "startup_failure"]);
  for (const item of body.workflow_runs) {
    const time = typeof item?.created_at === "string" && /Z$|[+-]\d\d:\d\d$/.test(item.created_at)
      ? Date.parse(item.created_at) : NaN;
    if (!Number.isSafeInteger(item?.id) || item.id <= 0 || !Number.isFinite(time)
      || time > now.getTime() || (since && time < since.getTime())
      || item.head_branch !== REF || item.event !== "workflow_dispatch"
      || !statuses.has(item.status)
      || (item.status === "completed" && !conclusions.has(item.conclusion))) {
      return { ok: false, detail: "invalid run metadata" };
    }
  }
  const run = body.workflow_runs.reduce((latest, item) =>
    !latest || Date.parse(item.created_at) > Date.parse(latest.created_at)
      || (Date.parse(item.created_at) === Date.parse(latest.created_at) && item.id > latest.id)
      ? item : latest, null);
  return { ok: true, run };
}

function minutesSince(now, value) {
  return (now.getTime() - new Date(value).getTime()) / 60_000;
}

async function alert(fetchImpl, token, reason, details) {
  return dispatch(fetchImpl, token, WATCHDOG, {
    reason: reason.replaceAll(token, "[redacted]").slice(0, 240),
    details: details.replaceAll(token, "[redacted]").slice(0, 4000),
  });
}

function classify(latest, now, allowedMinutes, pendingGraceMinutes = 0) {
  if (!latest.ok) return "unknown";
  if (!latest.run) return "missing";
  if (minutesSince(now, latest.run.created_at) > allowedMinutes) {
    return "stale";
  }
  if (latest.run.status === "completed" && latest.run.conclusion !== "success") {
    return "failed";
  }
  if (latest.run.status === "completed") return "healthy";
  return minutesSince(now, latest.run.created_at) <= pendingGraceMinutes
    ? "pending" : "unfinished";
}

async function checkFresh(fetchImpl, token, workflow, now, allowedMinutes, label,
  pendingGraceMinutes = 0) {
  const observations = [];
  async function read(stage, since) {
    const latest = await latestRun(fetchImpl, token, workflow, now, since);
    const run = latest.run;
    const observation = {
      stage, run_id: run?.id ?? null, created_at: run?.created_at ?? null,
      age_minutes: run ? Math.round(minutesSince(now, run.created_at) * 100) / 100 : null,
      status: run?.status ?? null, conclusion: run?.conclusion ?? null,
      error: latest.ok ? null : latest.detail.replaceAll(token, "[redacted]"),
    };
    observations.push(observation);
    console.log(JSON.stringify({ event: "github_latest_run", workflow,
      checked_at: now.toISOString(), allowed_minutes: allowedMinutes, ...observation }));
    return latest;
  }
  let latest = await read("initial");
  const initialDecision = classify(latest, now, allowedMinutes, pendingGraceMinutes);
  if (initialDecision !== "healthy") {
    const since = ["stale", "missing"].includes(initialDecision)
      ? new Date(now.getTime() - allowedMinutes * 60_000) : undefined;
    latest = await read("confirmation", since);
  }
  const decision = classify(latest, now, allowedMinutes, pendingGraceMinutes);
  const shouldAlert = !["healthy", "pending"].includes(decision);
  console.log(JSON.stringify({ event: "watchdog_decision", workflow,
    checked_at: now.toISOString(), allowed_minutes: allowedMinutes,
    pending_grace_minutes: pendingGraceMinutes,
    initial_decision: initialDecision, decision, alert: shouldAlert, observations }));
  if (!shouldAlert) return { ok: true };
  const reasons = {
    unknown: "GitHub 상태 확인 실패 (실행 지연 여부 미확인)",
    missing: `최근 ${allowedMinutes}분 내 workflow 실행 기록 없음 (재조회 확인)`,
    stale: `최신 실행이 ${allowedMinutes}분을 초과함 (재조회 확인)`,
    failed: `최신 실행 실패 (${latest.run?.conclusion})`,
    unfinished: `점검 시각까지 실행 미완료 (${latest.run?.status})`,
  };
  const evidence = observations.map(item =>
    `${item.stage}: run=${item.run_id ?? "미확인"}, created_at=${item.created_at ?? "미확인"}, `
    + `age_minutes=${item.age_minutes ?? "미확인"}, status=${item.status ?? "미확인"}, `
    + `conclusion=${item.conclusion ?? "미확인"}, error=${item.error ?? "없음"}`
    + (item.run_id ? `\nhttps://github.com/${OWNER}/${REPOSITORY}/actions/runs/${item.run_id}` : ""));
  return alert(fetchImpl, token, `${label}: ${reasons[decision]}`,
    `workflow=${workflow}\nchecked_at_utc=${now.toISOString()}\nthreshold_minutes=${allowedMinutes}\n`
    + `pending_grace_minutes=${pendingGraceMinutes}\n`
    + evidence.join("\n"));
}

export async function handleScheduled(cron, now, env, fetchImpl = fetch) {
  const result = await routeScheduled(cron, now, env, fetchImpl);
  if (!result.ok) throw new Error(`Scheduled GitHub request failed: ${result.detail}`);
  return result;
}

async function routeScheduled(cron, now, env, fetchImpl) {
  const token = env.GITHUB_DISPATCH_TOKEN;
  if (!token) throw new Error("GITHUB_DISPATCH_TOKEN is not configured");

  if (cron === "17,47 0-6 * * *" || cron === "17 7-23 * * *") {
    return dispatch(fetchImpl, token, COLLECT);
  }
  if (cron === "7 22 * * SUN-THU") return dispatch(fetchImpl, token, REPORT, { run: "morning" });
  if (cron === "37 12 * * MON-FRI") return dispatch(fetchImpl, token, REPORT, { run: "evening" });

  const watchdogCron = "10,15,25,40,55 * * * *";
  // Natural events still carried the former trigger after the control API
  // reported the new cron. Preserve checks during that overlap, never silently
  // treating a recognized historical watchdog event as a successful no-op.
  const legacyWatchdog = cron === "15,25,40 * * * *";
  if (cron !== watchdogCron && !legacyWatchdog) return { ok: true };
  if (legacyWatchdog) {
    console.log(JSON.stringify({ event: "scheduler_cron_compatibility", cron,
      expected_cron: watchdogCron, scheduled_at_utc: now.toISOString() }));
  }
  const minute = now.getUTCMinutes();
  const hour = now.getUTCHours();
  const weekday = now.getUTCDay();
  // Check the :47 poll before a later :17 success can hide its failure.
  const halfHourNewsCheck = (minute === 55 && hour <= 6)
    || (minute === 10 && hour >= 1 && hour <= 7);
  if (minute === 25 || minute === 40 || halfHourNewsCheck) {
    const news = await checkFresh(fetchImpl, token, COLLECT, now, 120, "뉴스 수집",
      NEWS_PENDING_GRACE_MINUTES);
    if (!news.ok || minute !== 40) return news;
  }
  if (minute === 15 && hour === 13 && weekday >= 1 && weekday <= 5) {
    return checkFresh(fetchImpl, token, REPORT, now, 40, "저녁 리포트");
  }
  if (minute === 40 && hour === 23 && weekday >= 0 && weekday <= 4) {
    return checkFresh(fetchImpl, token, REPORT, now, 120, "아침 리포트");
  }
  return { ok: true };
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(handleScheduled(controller.cron, new Date(controller.scheduledTime), env));
  },
};
