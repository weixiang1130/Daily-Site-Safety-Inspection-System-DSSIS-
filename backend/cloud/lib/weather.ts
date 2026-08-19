// 微型氣象站抓取邏輯
//
// 登入廠商平台、取得在線測站的量測值，寫入 device_readings，
// 供戰情室的「工地環境」面板使用。
//
// 由兩處呼叫：
//   backend/cloud/functions/weather-poll.mts  排程（每 15 分鐘）
//   /api/admin/weather-poll                   管理員手動觸發，用於驗證與排錯
//
// 平台網址、帳號密碼與測站對應皆由環境變數提供，不寫進版控：
//   WEATHER_API_URL       平台位址，例如 http://host:port
//   WEATHER_API_USER      帳號
//   WEATHER_API_PASS      密碼
//   WEATHER_SITE_MAP      測站 mac 對應本系統工地代碼的 JSON，
//                         例如 {"AAAA":"BD04","BBBB":"BD08"}
//
// 未設定 WEATHER_API_URL 時本函式直接跳過，不影響其他功能。

import { getDatabase } from "@netlify/database";

const db = getDatabase();

// channel 參數的分隔字元是 SOH（U+0001），不是一般符號。
// 這是廠商規格中最容易踩錯的地方，務必保留此寫法。
const SEP = String.fromCharCode(1);

/** 廠商頻道名稱 → 本系統 metric 代碼。未列出者略過不存。 */
const METRIC_MAP: Record<string, string> = {
  "PM2.5": "pm25",
  "PM10": "pm10",
  "噪音": "noise",
  "溫度": "temperature",
  "濕度": "humidity",
  "熱指數": "heat_index",
  "危害等級": "hazard_level",
  "噪音時段日間": "noise_alarm_day",
  "噪音時段晚間": "noise_alarm_evening",
  "噪音時段夜間": "noise_alarm_night",
  "日間警報": "noise_alarm_day",
  "晚間警報": "noise_alarm_evening",
  "夜間警報": "noise_alarm_night",
};

const env = (k: string) => Netlify.env.get(k) || "";

async function post(base: string, cmd: string, body: string): Promise<string> {
  const r = await fetch(`${base}/${cmd}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!r.ok) throw new Error(`${cmd} 回應 ${r.status}`);
  return r.text();
}

/**
 * 登入取得 session（UserIdx）。
 *
 * 帳密不是分開的欄位，而是合併後 base64：key = base64(帳號 + SOH + 密碼)。
 * 與 channel 參數用的是同一個 SOH 分隔字元。
 */
async function login(base: string): Promise<string> {
  const raw = `${env("WEATHER_API_USER")}${SEP}${env("WEATHER_API_PASS")}`;
  // btoa 只吃 Latin-1，帳密若含非 ASCII 需先轉為位元組
  const key = btoa(String.fromCharCode(...new TextEncoder().encode(raw)));
  // 平台不對 key 做 URL 解碼，base64 尾端的 "=" 一旦被編成 %3D 就會回 ErrUser，
  // 因此這裡手動組字串，不能用 URLSearchParams。
  const body = `key=${key}&val=${Math.random()}`;

  const t = await post(base, "Login", body);
  const m = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/.exec(t);
  if (!m) throw new Error(`登入失敗，未取得 UserIdx（回應：${t.slice(0, 120)}）`);
  return m[0];
}

function safeDecode(s: string): string {
  try { return decodeURIComponent(s); } catch { return s; }
}

interface Station { mac: string; name: string; lastAt: string; }

/** 取得目前「已連線」的測站。斷線者直接略過，不佔用後續請求。 */
async function onlineStations(base: string, uid: string): Promise<Station[]> {
  const t = await post(base, "ReadStateALL", `UserIdx=${uid}&val=${Math.random()}`);
  const out: Station[] = [];
  // 逐一擷取而非整份 JSON.parse —— 廠商回應偶有格式錯誤
  const re =
    /"DeviceName":"(.*?)".*?"mac":"(.*?)","ConnectState":"(.*?)".*?"ReadAILastTime":"(.*?)"/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(t)) !== null) {
    if (m[3] !== "已連線") continue;
    out.push({ mac: m[2], name: safeDecode(m[1]), lastAt: m[4] });
  }
  return out;
}

interface Reading { metric: string; value: number; at: string; }

/**
 * 取得單一測站當日的最新量測值。
 *
 * 廠商 API 有兩個地雷，因此不使用 JSON.parse：
 *   1. series 物件之間偶爾缺少逗號，整份 JSON 不合法（部分測站必現）
 *   2. 請求數超過該站實際頻道數時，尾端會重複輸出且值為空
 * 改以正規表示式逐一擷取 series，並以回應中的 Name 判斷頻道意義——
 * 各測站頻道配置不同，不可假設索引意義相同。
 */
async function readStation(base: string, uid: string, mac: string): Promise<Reading[]> {
  const today = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date());

  const channels = Array.from({ length: 8 }, (_, i) => `${mac}${SEP}${i}`).join(",");
  const body = new URLSearchParams({
    startT: today, endT: today, channel: channels,
    SampleOptions: "{}", Type: "1",
    isWriteUserHis: "0",              // 不寫入對方平台的使用者查詢紀錄
    dataSpanSec: "300",
    UserIdx: uid, val: String(Math.random()),
  }).toString();

  const text = await post(base, "TrendData", body);
  const out: Reading[] = [];
  const seen = new Set<string>();

  const re =
    /\{"Name":"(.*?)","Label".*?"Data":\[\{"Time":\[(.*?)\],"Value":\[(.*?)\]\}\]\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const chName = safeDecode(m[1]).split("-").slice(1).join("-");
    const metric = METRIC_MAP[chName];
    if (!metric || seen.has(metric)) continue;

    const times = m[2].split(",").map((s) => s.replace(/^"|"$/g, ""));
    const vals = m[3].split(",").map((s) => s.replace(/^"|"$/g, ""));

    // 尾端是補齊到區間結尾的空值，往前找最後一個有值的
    let i = vals.length - 1;
    while (i >= 0 && vals[i] === "") i--;
    if (i < 0) continue;

    const v = Number(vals[i]);
    if (!Number.isFinite(v)) continue;
    seen.add(metric);
    out.push({ metric, value: v, at: taipeiToISO(times[i]) });
  }
  return out;
}

/** "2026/08/19 10:00:00"（台北時間）→ 帶時區的 ISO 字串 */
function taipeiToISO(s: string): string {
  const m = /(\d{4})\/(\d{2})\/(\d{2}) (\d{2}):(\d{2}):(\d{2})/.exec(s);
  if (!m) return new Date().toISOString();
  return `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}+08:00`;
}

export async function pollWeatherStations(): Promise<string> {
  const base = env("WEATHER_API_URL").replace(/\/+$/, "");
  if (!base) {
    console.log("[weather] 未設定 WEATHER_API_URL，跳過");
    return "未設定 WEATHER_API_URL，已跳過";
  }

  let siteMap: Record<string, string> = {};
  try {
    siteMap = JSON.parse(env("WEATHER_SITE_MAP") || "{}");
  } catch {
    console.error("[weather] WEATHER_SITE_MAP 格式錯誤，測站將無法對應工地");
  }

  const uid = await login(base);
  const stations = await onlineStations(base, uid);
  console.log(`[weather] 在線測站 ${stations.length} 台`);

  let inserted = 0, skipped = 0;
  for (const st of stations) {
    const siteCode = siteMap[st.mac] || null;
    const site = siteCode
      ? (await db.sql`SELECT id FROM sites WHERE code = ${siteCode}`)[0]
      : undefined;

    let readings: Reading[] = [];
    try {
      readings = await readStation(base, uid, st.mac);
    } catch (e) {
      console.error(`[weather] ${st.name} 讀取失敗`, e);
      continue;
    }

    for (const r of readings) {
      // 只寫入比既有紀錄更新的資料，避免每次輪詢都塞重複值
      const last = (await db.sql`
        SELECT MAX(reading_at) AS t FROM device_readings
        WHERE device_id = ${st.mac} AND metric = ${r.metric}`)[0];
      if (last?.t && new Date(last.t) >= new Date(r.at)) { skipped++; continue; }

      await db.sql`
        INSERT INTO device_readings
          (site_id, site_code, vendor_code, device_type, device_id,
           metric, value_num, reading_at, raw_payload)
        VALUES (${site?.id ?? null}, ${siteCode}, 'weather-station', 'env',
                ${st.mac}, ${r.metric}, ${r.value}, ${r.at},
                ${JSON.stringify({ station: st.name })})`;
      inserted++;
    }
  }

  const msg = `在線測站 ${stations.length} 台，寫入 ${inserted} 筆、`
    + `略過 ${skipped} 筆（已是最新）`;
  console.log("[weather] " + msg);
  return msg;
}
