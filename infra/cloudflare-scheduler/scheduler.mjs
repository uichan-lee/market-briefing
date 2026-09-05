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

function headers(token) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

async function github(fetchImpl, token, path, init) {
  let response;
  for (let attempt = 1; attempt <= RETRIES; attempt += 1) {
    try {
      response = await fetchImpl(`${API}/${path}`, {
        ...init,
        headers: { ...headers(token), ...(init.headers || {}) },
      });
    } catch (error) {
      if (attempt === RETRIES) return { ok: false, detail: `network ${error.name}` };
      continue;
    }
    if (response.ok) return { ok: true, response };
    if (response.status < 500 && response.status !== 429) {
      return { ok: false, detail: `HTTP ${response.status}` };
    }
  }
  return { ok: false, detail: `HTTP ${response?.status ?? "unknown"}` };
}

export async function dispatch(fetchImpl, token, workflow, inputs = {}) {
  return github(fetchImpl, token, `${workflow}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: REF, inputs }),
  });
}

async function latestRun(fetchImpl, token, workflow) {
  const result = await github(fetchImpl, token, `${workflow}/runs?event=workflow_dispatch&per_page=1`, {
    method: "GET",
  });
  if (!result.ok) return result;
  const body = await result.response.json();
  return { ok: true, run: body.workflow_runs?.[0] };
}

function minutesSince(now, value) {
  return (now.getTime() - new Date(value).getTime()) / 60_000;
}

async function alert(fetchImpl, token, reason) {
  return dispatch(fetchImpl, token, WATCHDOG, { reason: reason.slice(0, 240) });
}

async function checkFresh(fetchImpl, token, workflow, now, allowedMinutes, label) {
  const latest = await latestRun(fetchImpl, token, workflow);
  if (!latest.ok) return alert(fetchImpl, token, `${label}: GitHub 상태 조회 실패 (${latest.detail})`);
  if (!latest.run) return alert(fetchImpl, token, `${label}: workflow 실행 기록 없음`);
  if (minutesSince(now, latest.run.created_at) > allowedMinutes) {
    return alert(fetchImpl, token, `${label}: 최신 실행이 ${allowedMinutes}분을 초과함`);
  }
  if (latest.run.status === "completed" && latest.run.conclusion !== "success") {
    return alert(fetchImpl, token, `${label}: 최신 실행 실패 (${latest.run.conclusion})`);
  }
  return { ok: true };
}

export async function handleScheduled(cron, now, env, fetchImpl = fetch) {
  const token = env.GITHUB_DISPATCH_TOKEN;
  if (!token) throw new Error("GITHUB_DISPATCH_TOKEN is not configured");

  if (cron === "17,47 0-6 * * *" || cron === "17 7-23 * * *") {
    return dispatch(fetchImpl, token, COLLECT);
  }
  if (cron === "7 22 * * SUN-THU") return dispatch(fetchImpl, token, REPORT, { run: "morning" });
  if (cron === "37 12 * * MON-FRI") return dispatch(fetchImpl, token, REPORT, { run: "evening" });

  if (cron !== "15,25,40 * * * *") return { ok: true };
  const minute = now.getUTCMinutes();
  const hour = now.getUTCHours();
  const weekday = now.getUTCDay();
  if (minute === 25) return checkFresh(fetchImpl, token, COLLECT, now, 120, "뉴스 수집");
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
