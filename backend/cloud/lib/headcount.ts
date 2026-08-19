// 工地人員進出人次抓取
//
// 來源是門禁／人臉辨識系統的看板服務，端點為：
//   GET {HEADCOUNT_API_URL}/person/list/All
//   → { code, msg, data: { inCount, outCount, presenceCount, in[], out[] } }
//
// **只取彙總數字，不取 in/out 明細。**
// 明細裡有姓名、員工編號、所屬廠商與人臉辨識紀錄，屬於個人資料。戰情室要
// 回答的是「現場有多少人」，不需要是誰，因此不落地任何個資——沒有存進來的
// 資料就不會外洩，也不必為它另外做保護措施。
//
// 注意：該服務目前沒有任何驗證，任何人知道網址就能取得上述個資。已請廠商
// 加上存取控制；在對方修正前，本系統的介接方式不受影響。
//
// 環境變數：
//   HEADCOUNT_API_URL     看板服務位址，例如 http://host
//   HEADCOUNT_SITE_CODE   對應本系統的工地代碼，例如 BD04
//
// 未設定 HEADCOUNT_API_URL 時直接跳過，不影響其他功能。

import { getDatabase } from "@netlify/database";

const db = getDatabase();

const env = (k: string) => Netlify.env.get(k) || "";

/** 本系統的指標代碼 → 來源欄位 */
const METRICS: Record<string, string> = {
  headcount_in: "inCount",
  headcount_out: "outCount",
  headcount_present: "presenceCount",
};

export async function pollHeadcount(): Promise<string> {
  const base = env("HEADCOUNT_API_URL").replace(/\/+$/, "");
  if (!base) {
    console.log("[headcount] 未設定 HEADCOUNT_API_URL，跳過");
    return "未設定 HEADCOUNT_API_URL，已跳過";
  }

  const r = await fetch(`${base}/person/list/All`, {
    headers: { Accept: "application/json" },
  });
  if (!r.ok) throw new Error(`人數服務回應 ${r.status}`);

  const body = await r.json() as {
    code?: number; msg?: string;
    data?: { inCount?: number; outCount?: number; presenceCount?: number };
  };
  if (body.code !== 200 || !body.data) {
    throw new Error(`人數服務回報失敗：${body.msg || "格式不符"}`);
  }

  const siteCode = env("HEADCOUNT_SITE_CODE") || null;
  const site = siteCode
    ? (await db.sql`SELECT id FROM sites WHERE code = ${siteCode}`)[0]
    : undefined;

  // 來源沒有提供量測時間，以抓取時間為準。輪詢間隔遠短於資料更新頻率，
  // 誤差在分鐘級，對「現場有多少人」這個用途足夠。
  const at = new Date().toISOString();

  let written = 0;
  for (const [metric, field] of Object.entries(METRICS)) {
    const value = Number((body.data as Record<string, unknown>)[field]);
    if (!Number.isFinite(value)) continue;

    await db.sql`
      INSERT INTO device_readings
        (site_id, site_code, vendor_code, device_type, device_id,
         metric, value_num, reading_at, raw_payload)
      VALUES (${site?.id ?? null}, ${siteCode}, 'access-control', 'people',
              ${siteCode || "headcount"}, ${metric}, ${value}, ${at}, ${"{}"})`;
    written += 1;
  }

  const msg = `在場 ${body.data.presenceCount}、今日進場 ${body.data.inCount}、`
    + `出場 ${body.data.outCount}，寫入 ${written} 筆`;
  console.log("[headcount] " + msg);
  return msg;
}
