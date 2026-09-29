// 工地看板外部區塊的「時鐘」：每 2 小時觸發一次 GitHub Actions 收集流程。
// （2026-09-29 由 30 分鐘放寬：看板實際使用率低，30 分鐘的新鮮度沒有人在用，
//  每輪都要花函式呼叫與 GitHub Actions 時間。）
//
// 為什麼不直接用 GitHub 的 schedule：實測設定「每 30 分鐘」，GitHub 20 小時
// 只跑了 6 次、間隔 2~5 小時（公開 repo 的免費排程會延遲甚至整輪略過），
// 看板因此長時間過期。以 API 觸發（workflow_dispatch）的執行會立刻開始，
// 不受排程壅塞影響——所以由準時的 Netlify 排程函式當時鐘，GitHub 只負責
// 跑收集程式。GitHub 自己的 schedule 仍保留，當這裡失效時的後備。
//
// 成本：每天 12 次、每次一個 HTTP 請求，不碰資料庫，遠低於 1 credit／月。
//
// 需要的環境變數（Netlify）：
//   GH_DISPATCH_TOKEN  GitHub fine-grained token，只授權本 repo 的
//                      Actions: Read and write（設為 secret；到期要換新）
//   GH_REPO            owner/repo
//
// 注意：Netlify 排程函式只在正式部署上執行，且無法以 HTTP 直接呼叫。

import type { Config } from "@netlify/functions";

const WORKFLOW = "wallboard-external.yml";

export default async (_req: Request) => {
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
  // UTC；每 30 分鐘，錯開整點與半點
  // UTC。每 2 小時的偶數小時 + 7 分：台北時間因為 +8（偶數）同樣落在偶數小時，
  // 所以 workflow 裡「台北 00 點那一輪才寫出工資料庫」與 06 點的 keepalive 都還在。
  // 改成奇數間隔（*/3 之類）會讓那兩件事靜悄悄停掉。
  schedule: "7 */2 * * *",
};
