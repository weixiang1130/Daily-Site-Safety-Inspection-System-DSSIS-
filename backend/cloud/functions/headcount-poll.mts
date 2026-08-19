// 工地人員進出人次排程抓取（每 10 分鐘）
//
// 實際邏輯在 ../lib/headcount.ts，與 /api/admin/headcount-poll 共用。
//
// 與氣象站分開排程：兩者來源不同、失效原因也不同，
// 合在一起的話任一邊出錯就會連累另一邊整輪跳過。
//
// 注意：Netlify 排程函式只在正式部署上執行，且無法以 HTTP 直接呼叫。
// 要立即驗證請改用 /api/admin/headcount-poll。

import type { Config } from "@netlify/functions";
import { pollHeadcount } from "../lib/headcount.ts";

export default async (_req: Request) => {
  try {
    const msg = await pollHeadcount();
    return new Response(msg);
  } catch (e) {
    console.error("[headcount] 排程抓取失敗", e);
    return new Response(String(e), { status: 500 });
  }
};

export const config: Config = {
  schedule: "*/10 * * * *",
};
