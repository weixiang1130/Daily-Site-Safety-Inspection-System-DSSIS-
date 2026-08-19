// 微型氣象站排程抓取（每 15 分鐘）
//
// 實際邏輯在 ../lib/weather.ts，與 /api/admin/weather-poll 共用，
// 避免排程與手動觸發兩套實作分岔。
//
// 注意：Netlify 排程函式只在正式部署上執行，且無法以 HTTP 直接呼叫。
// 要立即驗證請改用 /api/admin/weather-poll。

import type { Config } from "@netlify/functions";
import { pollWeatherStations } from "../lib/weather.ts";

export default async (_req: Request) => {
  try {
    const msg = await pollWeatherStations();
    return new Response(msg);
  } catch (e) {
    console.error("[weather] 排程抓取失敗", e);
    return new Response(String(e), { status: 500 });
  }
};

export const config: Config = {
  schedule: "*/15 * * * *",
};
