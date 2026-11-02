// 工地看板外部區塊的「時鐘」：定時觸發 GitHub Actions 收集流程。
// 台北時間上午（08～12 點）每 30 分鐘、下午（13～18 點）每小時、其餘每 2 小時。
// 頻率跟著出工回報走：2026-10 主場站 367 筆回報有 94% 在 08～12 點進來，下午零星。
// （2026-09-29 曾全天放寬成每 2 小時，但早上的本日出工要等太久才看得到。）
//
// 為什麼不直接用 GitHub 的 schedule：實測設定「每 30 分鐘」，GitHub 20 小時
// 只跑了 6 次、間隔 2~5 小時（公開 repo 的免費排程會延遲甚至整輪略過），
// 看板因此長時間過期。以 API 觸發（workflow_dispatch）的執行會立刻開始，
// 不受排程壅塞影響——所以由準時的 Netlify 排程函式當時鐘，GitHub 只負責
// 跑收集程式。GitHub 自己的 schedule 仍保留，當這裡失效時的後備。
//
// 成本：每天觸發 21 次、每次一個 HTTP 請求，不碰資料庫，遠低於 1 credit／月。
//
// 需要的環境變數（Netlify）：
//   GH_DISPATCH_TOKEN  GitHub fine-grained token，只授權本 repo 的
//                      Actions: Read and write（設為 secret；到期要換新）
//   GH_REPO            owner/repo
//
// 注意：Netlify 排程函式只在正式部署上執行，且無法以 HTTP 直接呼叫。

import type { Config } from "@netlify/functions";

const WORKFLOW = "wallboard-external.yml";

/** 這一輪要不要跑（排程每 30 分鐘叫醒一次，由這裡決定）。 */
export function isDue(now: Date): boolean {
  const taipei = new Date(now.getTime() + 8 * 3600000);
  const hour = taipei.getUTCHours();
  if (hour >= 8 && hour < 12) return true;
  if (taipei.getUTCMinutes() >= 30) return false;
  return hour % 2 === 0 || (hour >= 13 && hour < 18);
}

export default async (_req: Request) => {
  if (!isDue(new Date())) return new Response("not due");
  const token = Netlify.env.get("GH_DISPATCH_TOKEN") || "";
  const repo = (Netlify.env.get("GH_REPO") || "").trim();
  if (!token || !repo) {
    console.log("[wallboard-tick] 未設定 GH_DISPATCH_TOKEN 或 GH_REPO，略過（改由 GitHub 排程後備）");
    return new Response("skipped");
  }
  const r = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${WORKFLOW}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "safety-ops-wallboard-tick",
      },
      body: JSON.stringify({ ref: "main" }),
    });
  if (r.status !== 204) {
    // 401/403 多半是 token 到期或權限不足；看板會因資料過期亮警示
    console.error("[wallboard-tick] 觸發失敗", r.status, (await r.text()).slice(0, 200));
    return new Response(`dispatch failed: ${r.status}`, { status: 502 });
  }
  return new Response("dispatched");
};

export const config: Config = {
  // 錯開整點與半點。cron 無法表達「不同時段不同頻率」，所以每 30 分鐘叫醒、由 isDue 篩。
  // isDue 一定要留台北偶數小時的 :07：workflow 裡「00 點那一輪才寫出工資料庫」與
  // 06 點的 keepalive 靠它，少了會靜悄悄停掉。
  schedule: "7,37 * * * *",
};
