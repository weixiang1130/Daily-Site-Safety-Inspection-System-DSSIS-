// 職安填報系統 API（Netlify Functions）
//
// 單一函式處理所有 /api/* 路由，減少冷啟動並讓 session 處理集中在一處。
// 資料層：Netlify Database（Postgres）
// 檔案層：Netlify Blobs（照片、手寫簽名、產出的 PDF）

import type { Config, Context } from "@netlify/functions";
import { getDatabase } from "@netlify/database";
import { getDeployStore, getStore } from "@netlify/blobs";

import {
  clearSessionCookie, createSession, hashPassword, readSession,
  sessionCookieHeader, verifyPassword, type SessionUser,
} from "../lib/auth.ts";
import { buildCoordinationPdf, buildInspectionPdf, type SigInput } from "../lib/pdf.ts";

const db = getDatabase();

/** 正式環境用全域儲存區；預覽部署用各自獨立的儲存區，避免污染正式資料。 */
function files() {
  const ctx = (globalThis as any).Netlify?.context?.deploy?.context;
  return ctx === "production" ? getStore("safety-files") : getDeployStore("safety-files");
}

// ---------------------------------------------------------------------------
// 設定
// ---------------------------------------------------------------------------
/** 逗號分隔的 key:value 環境變數 → 物件。INGEST_TOKENS 與 BUILDING_LABELS
    共用同一條解析規則，兩邊才不會對「值裡含冒號」這種輸入各自表述。 */
function envPairs(name: string): Record<string, string> {
  const raw = Netlify.env.get(name) || "";
  const out: Record<string, string> = {};
  for (const pair of raw.split(",")) {
    const i = pair.indexOf(":");
    if (i > 0) out[pair.slice(0, i).trim()] = pair.slice(i + 1).trim();
  }
  return out;
}

const BRANDING = {
  system_name: Netlify.env.get("SYSTEM_NAME") || "職安填報系統",
  war_room_name: Netlify.env.get("WAR_ROOM_NAME") || "職安戰情室",
  org_name: Netlify.env.get("BRAND_NAME") || "示範營造股份有限公司",
  org_short: Netlify.env.get("BRAND_SHORT_NAME") || "示範營造",
  org_name_en: Netlify.env.get("BRAND_NAME_EN") || "Demo Construction",
  group_name: Netlify.env.get("BRAND_GROUP") || "",
  // 戰情室聚焦的工地。設定後，填報頁（前端）只列這個工地——兩張建照、
  // 同一塊工地，棟別由「棟別」欄位區分；瀏覽篩選與維運工具仍拿完整清單。
  // trim：這個值會被各端拿去做字串比對，帶到尾隨空白就是四十個工地的差別。
  primary_site_code: (Netlify.env.get("PRIMARY_SITE_CODE") || "").trim(),
  // 棟別選項，來自 BUILDING_LABELS（code:標籤,…）的標籤值、依序去重。
  // 未設定時前端退回預設清單（frontend/common.js 的 BUILDINGS）。
  buildings: [...new Set(Object.values(envPairs("BUILDING_LABELS")))],
  // 雲端的戰情室是「唯讀看板」：顯示地端每 5 分鐘推上來的快照，
  // 不查資料庫、不接監視器（監視畫面用串流的方式放雲端會在兩週內
  // 燒光免費額度，算式見 docs/地端戰情室.md）。首頁入口因此打開。
  war_room: true,
  // 前端據此決定走「快照模式」：雲端沒有即時的 /api/dashboard，
  // 一律讀 /api/wallboard 的快照。地端則為 false，走即時查詢。
  wallboard: true,
};

// 過期判定改在前端做（frontend/dashboard.html 的 STALE_AFTER_MIN）：
// 它手上有快照的 generated_at，也是唯一能把警示顯示給人看的地方。
// 這裡原本留了一個 SNAPSHOT_MAX_AGE_SEC 常數但從來沒有被引用過——
// 有常數卻沒人用，比沒有更危險：審查時會誤以為這塊已經防守過了。

function ingestTokens(): Record<string, string> {
  // 刻意沒有預設值：預設權杖印在公開 repo 裡，等於任何人都能推送偽造的
  // 設備數據。未設定時清單為空，所有推送一律 401。
  return envPairs("INGEST_TOKENS");
}

// ---------------------------------------------------------------------------
// 回應工具
// ---------------------------------------------------------------------------
const json = (data: unknown, init: ResponseInit = {}) =>
  new Response(JSON.stringify(data), {
    ...init,
    headers: { "content-type": "application/json; charset=utf-8", ...(init.headers || {}) },
  });

const fail = (status: number, detail: string) => json({ detail }, { status });

const TZ = "Asia/Taipei";

/** 台北時區的今天（YYYY-MM-DD）。伺服器跑 UTC，直接用 toISOString 會差一天。 */
function todayISO(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date());
}

/** 日期欄位 → YYYY-MM-DD。pg 回傳 Date 物件，直接序列化會變完整 ISO 字串。 */
function dateOnly(v: unknown): string | null {
  if (!v) return null;
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date(v as string));
}

/** 時間欄位 → YYYY-MM-DDTHH:MM（台北時間），與前端顯示格式一致。 */
function minuteISO(v: unknown): string | null {
  if (!v) return null;
  const p = new Intl.DateTimeFormat("en-CA", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date(v as string));
  const g = (t: string) => p.find((x) => x.type === t)?.value ?? "00";
  return `${g("year")}-${g("month")}-${g("day")}T${g("hour")}:${g("minute")}`;
}

// ---------------------------------------------------------------------------
// 檔案：以 Blobs 儲存，用 /api/file/<key> 取回
// ---------------------------------------------------------------------------
const FILE_PREFIX = "/api/file/";

function newKey(folder: string, ext: string) {
  const d = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  return `${folder}/${d}-${crypto.randomUUID().slice(0, 12)}.${ext}`;
}

/** 把前端 canvas 的 data:image/png;base64,... 存進 Blobs，回傳可直接使用的路徑。 */
async function saveDataUrl(dataUrl: string, folder: string): Promise<string> {
  const m = /^data:image\/(png|jpeg);base64,(.+)$/s.exec(dataUrl || "");
  if (!m) throw new Error("簽名格式錯誤");
  const bytes = Uint8Array.from(atob(m[2]), (c) => c.charCodeAt(0));
  const key = newKey(folder, m[1] === "png" ? "png" : "jpg");
  await files().set(key, bytes.buffer as ArrayBuffer, {
    metadata: { contentType: `image/${m[1]}` },
  });
  return FILE_PREFIX + key;
}

/** 由 /api/file/<key> 這種路徑取回原始位元組，供 PDF 嵌入簽名圖使用。 */
async function readFileBytes(pathOrKey: string | null): Promise<Uint8Array | null> {
  if (!pathOrKey) return null;
  const key = pathOrKey.startsWith(FILE_PREFIX)
    ? pathOrKey.slice(FILE_PREFIX.length) : pathOrKey;
  try {
    const buf = await files().get(key, { type: "arrayBuffer" });
    return buf ? new Uint8Array(buf as ArrayBuffer) : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// 主處理
// ---------------------------------------------------------------------------
export default async (req: Request, _ctx: Context): Promise<Response> => {
  const url = new URL(req.url);
  const p = url.pathname.replace(/\/+$/, "") || "/api";
  const method = req.method.toUpperCase();

  try {
    // ---- 不需登入 ----
    if (p === "/api/branding") return json(BRANDING);

    if (p === "/api/health") {
      const [{ n: templates }] = await db.sql`SELECT COUNT(*)::int AS n FROM form_templates`;
      const [{ n: items }] = await db.sql`SELECT COUNT(*)::int AS n FROM form_items`;
      const [{ n: findings }] = await db.sql`SELECT COUNT(*)::int AS n FROM findings`;
      return json({
        ok: true,
        database: "Netlify Database (PostgreSQL)",
        form_templates: templates, form_items: items, findings,
        server_time: new Date().toISOString(),
      });
    }

    if (p === "/api/login" && method === "POST") {
      const form = await req.formData();
      const username = String(form.get("username") || "");
      const password = String(form.get("password") || "");
      const rows = await db.sql`
        SELECT id, username, password_hash, display_name, role, site_id, employee_no
        FROM users WHERE username = ${username} AND active = TRUE`;
      const u = rows[0];
      if (!u || !(await verifyPassword(password, u.password_hash))) {
        return fail(401, "帳號或密碼錯誤");
      }
      const user = {
        id: u.id, username: u.username, name: u.display_name,
        role: u.role, site_id: u.site_id, employee_no: u.employee_no,
      };
      return json({ ok: true, user }, {
        headers: { "set-cookie": sessionCookieHeader(await createSession(user)) },
      });
    }

    if (p === "/api/logout" && method === "POST") {
      return json({ ok: true }, { headers: { "set-cookie": clearSessionCookie() } });
    }

    const me = await readSession(req);

    if (p === "/api/me") return json({ user: me });

    // ---- 設備廠商推送（以權杖驗證，不用 session）----
    if (p === "/api/v1/ingest/device" && method === "POST") {
      const body = await req.json();
      const token = req.headers.get("x-vendor-token") || "";
      const vendorCode = String(body.vendor_code || "");
      if (ingestTokens()[vendorCode] !== token) return fail(401, "廠商權杖驗證失敗");

      const site = (await db.sql`
        SELECT id FROM sites WHERE code = ${body.site_code || null}`)[0];
      let accepted = 0;
      for (const r of body.readings || []) {
        await db.sql`
          INSERT INTO device_readings
            (site_id, site_code, vendor_code, device_type, device_id,
             metric, value_num, value_text, reading_at, raw_payload)
          VALUES (${site?.id ?? null}, ${body.site_code ?? null}, ${vendorCode},
                  ${body.device_type ?? null}, ${body.device_id ?? null},
                  ${r.metric ?? null}, ${r.value_num ?? null}, ${r.value_text ?? null},
                  ${r.reading_at}, ${JSON.stringify(r)})`;
        accepted++;
      }
      return json({ ok: true, accepted });
    }

    // ---- 工地看板（地端推快照 → 工地電腦唯讀顯示）----
    //
    // 工地辦公室的電腦什麼都不能裝、也連不到公司內網，只有一個瀏覽器。
    // 因此由公司內網的地端主機定時把整份儀表板資料 POST 上來存成 Blob，
    // 工地電腦開網址讀這一份。
    //
    // **只寫 Blob，絕不觸發重新部署。** 每 5 分鐘 deploy 一次會在三天內
    // 燒光免費的 300 分鐘建置額度；寫 Blob 則只算一次函式呼叫。
    //
    // 快照是「一份固定的視圖」——地端產生時就決定了天數與工地範圍。
    // 工地電腦上的天數／工地下拉因此不會作用，前端在看板模式會隱藏它們。
    const WALL_KEY = "wallboard/snapshot.json";

    if (p === "/api/v1/ingest/wallboard" && method === "POST") {
      const expected = Netlify.env.get("SITE_AGENT_TOKEN") || "";
      const token = req.headers.get("x-agent-token") || "";
      if (!expected || token !== expected) return fail(401, "代理權杖驗證失敗");

      const body = await req.text();
      // 存原始字串而非重新序列化：這份資料只會被原樣讀出去，
      // 中間多一次 parse/stringify 只是多一個出錯的地方。
      await files().set(WALL_KEY, body, {
        metadata: { pushed_at: new Date().toISOString() },
      });
      // Buffer.byteLength：內容以中文為主，.length 是字元數，會少報三倍
      return json({ ok: true, bytes: Buffer.byteLength(body, "utf8") });
    }

    // 這個看板會在公開網際網路上，內容含缺失描述與廠商名稱，
    // 因此以 WALL_TOKEN 驗證。權杖外流時改一次環境變數即可撤換。
    // 兩種身分都可以看板：
    //   1. 已登入的使用者——從填報站首頁點「戰情儀表板」進來的人。
    //   2. 帶 ?k=<WALL_TOKEN> 的牆上大螢幕——整天開著、沒有人登入。
    // 刻意不讓首頁的按鈕帶權杖：那等於把權杖攤在每個登入者眼前，
    // 而他們本來就有 session，不需要它。
    if (p === "/api/wallboard" && method === "GET") {
      const expected = Netlify.env.get("WALL_TOKEN") || "";
      const key = url.searchParams.get("k") || "";
      const tokenOk = expected !== "" && key === expected;
      if (!tokenOk && !me) {
        return fail(401, key
          ? "看板權杖錯誤"
          : "請先登入，或使用看板權杖網址（未設定 WALL_TOKEN 時大螢幕無法免登入）");
      }

      const body = await files().get(WALL_KEY, { type: "text" });
      if (!body) return fail(404, "尚無快照，地端還沒推送過");
      // 快取分兩種身分處理：
      //   憑權杖：同一組 ?k= 網址對所有大螢幕都一樣，可讓 CDN 共用，
      //           多面牆就只回源一次——這才讓快取真的省到呼叫次數。
      //   憑 session：內容因人而異，只能存在自己的瀏覽器裡。
      // max-age 對齊推送間隔（900 秒）：設得比它短，快取在被讀到之前
      // 就過期了，等於白寫；設得比它長則會顯示更舊的資料。
      // 對齊之後，多面牆的讀取會落在 CDN 上，回源次數不隨螢幕數增加。
      const cache = tokenOk ? "public, max-age=900" : "private, max-age=900";
      return new Response(body, {
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": cache,
        },
      });
    }

    if (p === "/api/v1/device/latest") {
      const siteCode = url.searchParams.get("site_code");
      const deviceType = url.searchParams.get("device_type");
      const limit = Math.min(parseInt(url.searchParams.get("limit") || "50", 10), 500);
      const rows = await db.sql`
        SELECT site_code, vendor_code, device_type, device_id, metric,
               value_num, value_text, reading_at
        FROM device_readings
        WHERE (${siteCode}::text IS NULL OR site_code = ${siteCode})
          AND (${deviceType}::text IS NULL OR device_type = ${deviceType})
        ORDER BY reading_at DESC
        LIMIT ${limit}`;
      return json(rows.map((r: any) => ({
        ...r, value_num: r.value_num === null ? null : Number(r.value_num),
      })));
    }

    // 地端戰情室拉取表單資料。
    //
    // 戰情室已搬到公司內網（見 docs/地端戰情室.md），但表單仍留在雲端讓工地
    // 用手機填報。這個端點讓地端定時把新增與異動的資料抓回去，儀表板才有
    // 東西可以顯示。
    //
    // 只讀、只回傳統計需要的欄位。照片、簽名與 PDF 不在此列——那些是大檔，
    // 每輪都傳會把雲端流量吃掉，而這正是搬回地端要解決的問題。
    if (p === "/api/v1/export" && method === "GET") {
      const expected = Netlify.env.get("SITE_AGENT_TOKEN") || "";
      const token = req.headers.get("x-agent-token") || "";
      if (!expected || token !== expected) return fail(401, "代理權杖驗證失敗");

      // 增量取得：只回傳這個時間點之後異動的資料。首次同步不帶參數即可全取。
      const sinceRaw = url.searchParams.get("since") || "";
      const since = sinceRaw && !isNaN(Date.parse(sinceRaw))
        ? new Date(sinceRaw).toISOString()
        : new Date(0).toISOString();

      // 上限保護：資料累積幾年後不設限的全量查詢會讓函式逾時，
      // 地端拿到一半的資料卻以為同步完成，之後就再也補不回來。
      const LIMIT = 2000;

      const sites = await db.sql`
        SELECT code, name, department, sort_order, active FROM sites`;
      const vendors = await db.sql`
        SELECT code, name, active FROM vendors`;
      const findings = await db.sql`
        SELECT f.id, s.code AS site_code, f.building, f.source, f.found_at, f.location,
               f.hazard_code, f.hazard_label, f.description, v.code AS vendor_code,
               f.severity, f.action_type, f.due_date, f.fixed_at, f.verified_at,
               f.status, f.created_at
        FROM findings f
        JOIN sites s ON s.id = f.site_id
        LEFT JOIN vendors v ON v.id = f.vendor_id
        WHERE GREATEST(f.created_at, COALESCE(f.fixed_at, f.created_at),
                       COALESCE(f.verified_at, f.created_at)) > ${since}
        ORDER BY f.id LIMIT ${LIMIT}`;
      const inspections = await db.sql`
        SELECT i.id, s.code AS site_code, i.building, i.form_code, i.inspect_date,
               i.location, i.inspector_name, i.status, i.submitted_at, i.created_at,
               (SELECT COUNT(*) FROM inspection_results r
                 WHERE r.inspection_id = i.id AND r.result = 'fail') AS fail_count
        FROM inspections i
        JOIN sites s ON s.id = i.site_id
        WHERE i.created_at > ${since} OR i.submitted_at > ${since}
        ORDER BY i.id LIMIT ${LIMIT}`;
      const coordinations = await db.sql`
        SELECT c.id, s.code AS site_code, c.building, c.meeting_date, c.work_date,
               c.recorder_name, c.status, c.submitted_at, c.created_at,
               (SELECT COUNT(*) FROM coordination_attendees a
                 WHERE a.coordination_id = c.id) AS attendee_count
        FROM coordinations c
        JOIN sites s ON s.id = c.site_id
        WHERE c.created_at > ${since} OR c.submitted_at > ${since}
        ORDER BY c.id LIMIT ${LIMIT}`;

      // 地端拿這個時間當下一輪的 since。用資料本身的最大時間而不是「現在」——
      // 用「現在」的話，查詢期間才寫進來的資料會被永遠跳過。
      const stamps = [...findings, ...inspections, ...coordinations]
        .map((r: any) => r.created_at).filter(Boolean)
        .map((t: any) => new Date(t).toISOString());
      const nextSince = stamps.length ? stamps.sort().at(-1) : since;

      return json({
        since, next_since: nextSince, limit: LIMIT,
        truncated: findings.length >= LIMIT || inspections.length >= LIMIT
          || coordinations.length >= LIMIT,
        sites, vendors, findings, inspections, coordinations,
      });
    }

    // ---- 檔案 ----
    if (p.startsWith(FILE_PREFIX)) {
      if (!me) return fail(401, "請先登入");
      const key = p.slice(FILE_PREFIX.length);
      const got = await files().getWithMetadata(key, { type: "arrayBuffer" });
      if (!got?.data) return fail(404, "檔案不存在");
      const ct = (got.metadata as any)?.contentType || "application/octet-stream";
      return new Response(got.data as ArrayBuffer, {
        headers: {
          "content-type": ct,
          "cache-control": "private, max-age=3600",
          "x-content-type-options": "nosniff",
        },
      });
    }

    // ---- 以下皆需登入 ----
    if (!me) return fail(401, "請先登入");

    if (p === "/api/sites") {
      // 一律回傳完整清單。「填報只列主場站」是填報頁自己的事
      // （frontend/common.js 的 fillableSites）——這個端點同時供
      // 缺失清單／儀表板的瀏覽篩選與匯入工具查工地主檔，在這裡過濾
      // 會讓那些消費者拿到殘缺的主檔（實測害補登工具跟 e2e 測試直接掛）。
      return json(await db.sql`
        SELECT id, code, name, department FROM sites WHERE active = TRUE
        ORDER BY department NULLS FIRST, sort_order, id`);
    }

    if (p === "/api/vendors") {
      return json(await db.sql`
        SELECT id, code, name FROM vendors WHERE active = TRUE ORDER BY name`);
    }

    // 依名稱取得廠商，不存在就建立。
    // 協力商在各工地差異很大且會隨工程階段更換，不可能由管理員預先維護齊全，
    // 因此填報時允許現場直接輸入新廠商名稱。這裡必須建立正式的廠商資料
    // （而非存成自由文字），否則儀表板的「廠商缺失排行」會漏統計。
    if (p === "/api/vendors/resolve" && method === "POST") {
      const b = await req.json();
      const name = String(b.name || "").trim();
      if (!name) return fail(400, "廠商名稱不可為空");
      if (name.length > 128) return fail(400, "廠商名稱過長");

      // 比對時忽略大小寫與前後空白，避免同一家廠商因輸入差異被建成兩筆
      const found = await db.sql`
        SELECT id, code, name FROM vendors WHERE LOWER(TRIM(name)) = LOWER(${name})`;
      if (found.length) {
        if (!found[0].active) {
          await db.sql`UPDATE vendors SET active = TRUE WHERE id = ${found[0].id}`;
        }
        return json({ ...found[0], created: false });
      }

      const [row] = await db.sql`
        INSERT INTO vendors (code, name, active)
        VALUES ('TMP-' || gen_random_uuid()::text, ${name}, TRUE)
        RETURNING id`;
      const [vendor] = await db.sql`
        UPDATE vendors SET code = 'V' || LPAD(id::text, 3, '0')
        WHERE id = ${row.id} RETURNING id, code, name`;
      return json({ ...vendor, created: true });
    }

    if (p === "/api/forms") {
      return json(await db.sql`
        SELECT form_code, title, short_name, form_type, item_count
        FROM form_templates WHERE active = TRUE ORDER BY form_code`);
    }

    const formMatch = /^\/api\/forms\/([A-Za-z0-9]+)$/.exec(p);
    if (formMatch) {
      const code = formMatch[1];
      const tpl = (await db.sql`
        SELECT form_code, title, form_type FROM form_templates
        WHERE form_code = ${code}`)[0];
      if (!tpl) return fail(404, "查無此表單");
      const items = await db.sql`
        SELECT id, seq, category, hazard_code, hazard_label, text
        FROM form_items WHERE form_code = ${code} ORDER BY seq`;
      return json({ ...tpl, items });
    }

    if (p === "/api/inspections" && method === "POST") {
      return await createInspection(req, me);
    }

    if (p === "/api/inspections" && method === "GET") {
      const days = parseInt(url.searchParams.get("days") || "30", 10);
      const siteId = url.searchParams.get("site_id");
      const rows = await db.sql`
        SELECT i.id, i.form_code, i.inspect_date, i.location, i.status, i.pdf_key,
               s.name AS site, i.site_id, i.building, ft.title AS form_title,
               COALESCE(i.inspector_name, u.display_name) AS inspector,
               (SELECT COUNT(*)::int FROM inspection_results r
                 WHERE r.inspection_id = i.id) AS item_count,
               (SELECT COUNT(*)::int FROM inspection_results r
                 WHERE r.inspection_id = i.id AND r.result = 'fail') AS fail_count
        FROM inspections i
        JOIN sites s ON s.id = i.site_id
        JOIN form_templates ft ON ft.form_code = i.form_code
        JOIN users u ON u.id = i.inspector_id
        WHERE i.inspect_date >= CURRENT_DATE - ${days}::int
          AND (${siteId}::int IS NULL OR i.site_id = ${siteId}::int)
        ORDER BY i.inspect_date DESC, i.id DESC
        LIMIT 300`;
      return json(rows.map((r: any) => ({
        ...r, inspect_date: dateOnly(r.inspect_date),
        pdf_url: r.pdf_key ? `/api/inspections/${r.id}/pdf` : null,
      })));
    }

    const inspPdf = /^\/api\/inspections\/(\d+)\/pdf$/.exec(p);
    if (inspPdf) return await servePdf("inspections", parseInt(inspPdf[1], 10));

    if (p === "/api/coordinations" && method === "POST") {
      return await createCoordination(req, me);
    }

    if (p === "/api/coordinations" && method === "GET") {
      const days = parseInt(url.searchParams.get("days") || "30", 10);
      const rows = await db.sql`
        SELECT c.id, c.work_date, c.status, c.pdf_key, c.site_id, c.building,
               s.name AS site,
               (SELECT COUNT(*)::int FROM coordination_attendees a
                 WHERE a.coordination_id = c.id) AS attendee_count
        FROM coordinations c JOIN sites s ON s.id = c.site_id
        WHERE c.work_date >= CURRENT_DATE - ${days}::int
        ORDER BY c.work_date DESC, c.id DESC LIMIT 200`;
      return json(rows.map((r: any) => ({
        ...r, work_date: dateOnly(r.work_date),
        pdf_url: r.pdf_key ? `/api/coordinations/${r.id}/pdf` : null,
      })));
    }

    const coordPdf = /^\/api\/coordinations\/(\d+)\/pdf$/.exec(p);
    if (coordPdf) return await servePdf("coordinations", parseInt(coordPdf[1], 10));

    // 首頁的「缺失概況」只要三個數字。
    //
    // 原本是叫 /api/dashboard，但戰情室搬到地端後那個端點已移除。改成三個
    // COUNT 而不是把整份缺失撈回前端自己算——首頁每次載入都會呼叫，
    // 資料量會隨案件累積無止境成長。
    if (p === "/api/findings/summary" && method === "GET") {
      const today = todayISO();
      const rows = await db.sql`
        SELECT
          COUNT(*) FILTER (WHERE found_at::date = ${today}::date) AS findings_today,
          COUNT(*) FILTER (WHERE status IN ('open', 'fixed'))     AS open,
          COUNT(*) FILTER (
            WHERE action_type = 'scheduled'
              AND due_date IS NOT NULL
              AND status NOT IN ('verified', 'closed')
              AND due_date < ${today}::date)                       AS overdue
        FROM findings`;
      const r: any = rows[0] || {};
      return json({
        findings_today: Number(r.findings_today || 0),
        open: Number(r.open || 0),
        overdue: Number(r.overdue || 0),
      });
    }

    if (p === "/api/findings" && method === "GET") {
      const days = parseInt(url.searchParams.get("days") || "30", 10);
      const siteId = url.searchParams.get("site_id");
      const status = url.searchParams.get("status");
      const overdueOnly = url.searchParams.get("overdue") === "true";
      const rows = await db.sql`
        SELECT f.*, s.name AS site, v.name AS vendor
        FROM findings f
        JOIN sites s ON s.id = f.site_id
        LEFT JOIN vendors v ON v.id = f.vendor_id
        WHERE f.found_at >= NOW() - (${days}::int * INTERVAL '1 day')
          AND (${siteId}::int IS NULL OR f.site_id = ${siteId}::int)
          AND (${status}::text IS NULL OR f.status = ${status})
        ORDER BY f.found_at DESC LIMIT 500`;
      const out = rows.map(shapeFinding).filter((f) => !overdueOnly || f.overdue);
      return json(out);
    }

    if (p === "/api/findings" && method === "POST") {
      const b = await req.json();
      const bad = buildingTooLong(b.building);
      if (bad) return bad;
      const id = await insertFinding({ ...b, source: b.source || "audit" }, me);
      return json({ ok: true, finding_id: id });
    }

    const fixMatch = /^\/api\/findings\/(\d+)\/fix$/.exec(p);
    if (fixMatch && method === "POST") {
      const b = await req.json();
      await db.sql`
        UPDATE findings SET fixed_at = NOW(), fix_note = ${b.fix_note ?? null},
          photo_after = COALESCE(${b.photo_after ?? null}, photo_after), status = 'fixed'
        WHERE id = ${parseInt(fixMatch[1], 10)}`;
      return json({ ok: true });
    }

    const verifyMatch = /^\/api\/findings\/(\d+)\/verify$/.exec(p);
    if (verifyMatch && method === "POST") {
      if (!["safety", "manager", "admin"].includes(me.role)) {
        return fail(403, "僅職安人員或主管可複驗");
      }
      await db.sql`
        UPDATE findings SET verifier_id = ${me.id}, verified_at = NOW(), status = 'closed'
        WHERE id = ${parseInt(verifyMatch[1], 10)}`;
      return json({ ok: true });
    }

    // ---- 管理功能（限管理員）----
    // 工地、廠商與帳號的實際名稱都屬公司資料，一律在此以 API 維護，
    // 不寫進 migration，避免真實資料進入公開版控。
    if (p.startsWith("/api/admin/")) {
      if (me.role !== "admin") return fail(403, "僅系統管理員可使用");

      if (p === "/api/admin/sites" && method === "GET") {
        return json(await db.sql`
          SELECT id, code, name, department, sort_order, active
          FROM sites ORDER BY department NULLS FIRST, sort_order, id`);
      }

      if (p === "/api/admin/sites" && method === "POST") {
        const b = await req.json();
        const [row] = await db.sql`
          INSERT INTO sites (code, name, department, sort_order, active)
          VALUES (${b.code}, ${b.name}, ${b.department ?? null},
                  ${b.sort_order ?? 0}, TRUE)
          ON CONFLICT (code) DO UPDATE SET
            name = EXCLUDED.name, department = EXCLUDED.department,
            sort_order = EXCLUDED.sort_order, active = TRUE
          RETURNING id, code, name, department`;
        return json({ ok: true, site: row });
      }

      const siteMatch = /^\/api\/admin\/sites\/(\d+)$/.exec(p);
      if (siteMatch && (method === "PATCH" || method === "POST")) {
        const b = await req.json();
        const id = parseInt(siteMatch[1], 10);
        await db.sql`
          UPDATE sites SET
            name = COALESCE(${b.name ?? null}, name),
            department = COALESCE(${b.department ?? null}, department),
            sort_order = COALESCE(${b.sort_order ?? null}, sort_order),
            active = COALESCE(${b.active ?? null}, active)
          WHERE id = ${id}`;
        return json({ ok: true });
      }

      if (p === "/api/admin/vendors" && method === "POST") {
        const b = await req.json();
        const [row] = await db.sql`
          INSERT INTO vendors (code, name, active) VALUES (${b.code}, ${b.name}, TRUE)
          ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, active = TRUE
          RETURNING id, code, name`;
        return json({ ok: true, vendor: row });
      }

      const vendorMatch = /^\/api\/admin\/vendors\/(\d+)$/.exec(p);
      if (vendorMatch && (method === "PATCH" || method === "POST")) {
        const b = await req.json();
        await db.sql`
          UPDATE vendors SET
            name = COALESCE(${b.name ?? null}, name),
            active = COALESCE(${b.active ?? null}, active)
          WHERE id = ${parseInt(vendorMatch[1], 10)}`;
        return json({ ok: true });
      }

      // 變更密碼。repo 的 migration 含預設密碼，上線前必須逐一更換。
      if (p === "/api/admin/password" && method === "POST") {
        const b = await req.json();
        if (!b.username || !b.password || String(b.password).length < 8) {
          return fail(400, "密碼至少 8 碼");
        }
        const hash = await hashPassword(String(b.password));
        const rows = await db.sql`
          UPDATE users SET password_hash = ${hash}
          WHERE username = ${b.username} RETURNING id`;
        if (!rows.length) return fail(404, "查無此帳號");
        return json({ ok: true });
      }

      // 刪除單據與缺失。用於清除測試資料；
      // 正式缺失請走複驗結案而非刪除，以保留稽核軌跡。
      // findings 對 inspections / coordinations 的外鍵沒有 ON DELETE CASCADE
      // （正式資料不應被連帶刪除），因此這裡必須先手動清掉子紀錄。
      const delFinding = /^\/api\/admin\/findings\/(\d+)$/.exec(p);
      if (delFinding && (method === "DELETE" || method === "POST")) {
        await db.sql`DELETE FROM findings WHERE id = ${parseInt(delFinding[1], 10)}`;
        return json({ ok: true });
      }

      const delCoord = /^\/api\/admin\/coordinations\/(\d+)$/.exec(p);
      if (delCoord && (method === "DELETE" || method === "POST")) {
        const id = parseInt(delCoord[1], 10);
        await db.sql`DELETE FROM findings WHERE coordination_id = ${id}`;
        await db.sql`DELETE FROM coordinations WHERE id = ${id}`;
        return json({ ok: true });
      }

      const delInsp = /^\/api\/admin\/inspections\/(\d+)$/.exec(p);
      if (delInsp && (method === "DELETE" || method === "POST")) {
        const id = parseInt(delInsp[1], 10);
        await db.sql`DELETE FROM findings WHERE inspection_id = ${id}`;
        await db.sql`DELETE FROM inspections WHERE id = ${id}`;
        return json({ ok: true });
      }

      return fail(404, `找不到管理路由 ${p}`);
    }

    if (p === "/api/upload/photo" && method === "POST") {
      const form = await req.formData();
      const file = form.get("file");
      if (!(file instanceof File)) return fail(400, "缺少檔案");
      const ext = (file.name.split(".").pop() || "jpg").toLowerCase();
      if (!["jpg", "jpeg", "png", "webp"].includes(ext)) {
        return fail(400, "僅接受 jpg / png / webp");
      }
      // content-type 由副檔名決定，不採用呼叫端送來的 file.type。
      // /api/file 會把存下來的值原樣回傳，若信任呼叫端，上傳 x.png 卻標成
      // text/html 就會變成本站網域下的 HTML，對已登入的使用者形成儲存型 XSS。
      const safeExt = ext === "jpeg" ? "jpg" : ext;
      const key = newKey("photos", safeExt);
      await files().set(key, await file.arrayBuffer(), {
        metadata: { contentType: safeExt === "jpg" ? "image/jpeg" : `image/${safeExt}` },
      });
      return json({ ok: true, path: FILE_PREFIX + key });
    }

    return fail(404, `找不到路由 ${p}`);
  } catch (err: any) {
    console.error("[api] 未處理錯誤", p, err);
    return fail(500, err?.message || "伺服器錯誤");
  }
};

// ---------------------------------------------------------------------------
// 缺失
// ---------------------------------------------------------------------------

/** 棟別長度防線。雲端此欄是無上限的 TEXT，但地端是 NVARCHAR(32)：
    一筆超長值會讓地端同步整批 rollback、進度不推進、每輪重抓同一批——
    牆面從此停止更新。表單送不出超長值，這裡擋的是直接打 API 的寫入。 */
function buildingTooLong(v: unknown): Response | null {
  return String(v ?? "").trim().length > 32
    ? fail(400, "棟別長度不可超過 32 字") : null;
}

function shapeFinding(f: any) {
  const due = dateOnly(f.due_date);
  const overdue = f.action_type === "scheduled" && due
    && !["verified", "closed"].includes(f.status)
    && due < todayISO();
  return {
    id: f.id, no: `F${String(f.id).padStart(6, "0")}`, site: f.site, site_id: f.site_id,
    building: f.building ?? null,
    source: f.source, found_at: minuteISO(f.found_at), location: f.location,
    hazard_code: f.hazard_code, hazard_label: f.hazard_label, description: f.description,
    vendor: f.vendor ?? null, responsible_person: f.responsible_person,
    severity: f.severity, action_type: f.action_type,
    due_date: due, status: f.status, overdue: Boolean(overdue),
    photo_before: f.photo_before, photo_after: f.photo_after,
  };
}

async function insertFinding(b: any, me: SessionUser, ids: {
  siteId?: number; inspectionId?: number; coordinationId?: number;
} = {}): Promise<number> {
  const onsite = (b.action_type || "onsite") === "onsite";
  // found_at 可指定，用於補登既有紙本紀錄。未指定時為現在時間。
  // 沒有這個欄位的話，補登的歷史單據會全部堆在匯入當天，趨勢圖失真。
  const foundAt = b.found_at || new Date().toISOString();
  const [row] = await db.sql`
    INSERT INTO findings
      (site_id, building, inspection_id, coordination_id, item_id, source, found_at, location,
       hazard_code, hazard_label, description, vendor_id, responsible_person,
       severity, action_type, due_date, fixed_at, fix_note, status,
       photo_before, photo_after, created_by)
    VALUES (
      ${ids.siteId ?? b.site_id}, ${String(b.building ?? "").trim() || null},
      ${ids.inspectionId ?? null}, ${ids.coordinationId ?? null},
      ${b.item_id ?? null}, ${b.source ?? "inspection"}, ${foundAt}, ${b.location ?? null},
      ${b.hazard_code ?? "OTHER"}, ${b.hazard_label ?? "其他"}, ${b.description},
      ${b.vendor_id ? Number(b.vendor_id) : null}, ${b.responsible_person ?? null},
      ${b.severity ?? "minor"}, ${b.action_type ?? "onsite"},
      ${!onsite && b.due_date ? b.due_date : null},
      ${onsite ? foundAt : null},
      ${onsite ? (b.fix_note || "當場改善完成") : (b.fix_note ?? null)},
      ${onsite ? "fixed" : "open"},
      ${b.photo_before ?? null}, ${b.photo_after ?? null}, ${me.id})
    RETURNING id`;
  return row.id;
}

// ---------------------------------------------------------------------------
// 巡檢單
// ---------------------------------------------------------------------------
async function createInspection(req: Request, me: SessionUser): Promise<Response> {
  const b = await req.json();
  const tpl = (await db.sql`
    SELECT form_code, title FROM form_templates WHERE form_code = ${b.form_code}`)[0];
  if (!tpl) return fail(400, "表單代碼錯誤");
  const badBuilding = buildingTooLong(b.building);
  if (badBuilding) return badBuilding;

  const [insp] = await db.sql`
    INSERT INTO inspections
      (site_id, building, form_code, inspect_date, location, weather, inspector_id,
       inspector_name, status, submitted_at)
    VALUES (${Number(b.site_id)}, ${(b.building || "").trim() || null},
            ${b.form_code}, ${b.inspect_date || todayISO()},
            ${b.location ?? null}, ${b.weather ?? null}, ${me.id},
            ${(b.inspector_name || "").trim() || null},
            'submitted', NOW())
    RETURNING id, site_id, building, form_code, inspect_date, location, weather,
              inspector_name, submitted_at`;

  for (const r of b.results || []) {
    await db.sql`
      INSERT INTO inspection_results (inspection_id, item_id, day, result, remark)
      VALUES (${insp.id}, ${Number(r.item_id)}, ${r.day ?? null},
              ${r.result || "na"}, ${r.remark ?? null})`;
  }

  const findingIds: number[] = [];
  for (const f of b.findings || []) {
    findingIds.push(await insertFinding(
      // 缺失沿用表單的棟別：缺失是在填這張表時發現的，棟別必然相同
      { ...f, source: "inspection", location: f.location ?? insp.location,
        building: insp.building },
      me, { siteId: insp.site_id, inspectionId: insp.id },
    ));
  }

  const sigs = await saveSignatures(b.signatures || [], me, { inspectionId: insp.id });
  const pdfKey = await renderInspectionPdf(insp.id);

  return json({
    ok: true, inspection_id: insp.id,
    pdf_url: pdfKey ? `/api/inspections/${insp.id}/pdf` : null,
    finding_ids: findingIds, signatures: sigs,
  });
}

async function renderInspectionPdf(id: number): Promise<string | null> {
  const insp = (await db.sql`
    SELECT i.id, i.inspect_date, i.location, i.weather, i.submitted_at, i.building,
           s.name AS site_name, ft.title AS form_title,
           -- 共用帳號的情況下，帳號名稱代表不了實際檢查人，
           -- 因此以填報時填寫的姓名優先
           COALESCE(i.inspector_name, u.display_name) AS inspector_name
    FROM inspections i
    JOIN sites s ON s.id = i.site_id
    JOIN form_templates ft ON ft.form_code = i.form_code
    JOIN users u ON u.id = i.inspector_id
    WHERE i.id = ${id}`)[0];
  if (!insp) return null;

  const results = await db.sql`
    SELECT fi.seq, fi.category, fi.text, r.result, r.remark
    FROM inspection_results r JOIN form_items fi ON fi.id = r.item_id
    WHERE r.inspection_id = ${id} ORDER BY fi.seq`;

  const findings = await db.sql`
    SELECT f.*, v.name AS vendor_name FROM findings f
    LEFT JOIN vendors v ON v.id = f.vendor_id
    WHERE f.inspection_id = ${id} ORDER BY f.id`;

  const sigRows = await db.sql`
    SELECT role, signer_name, signed_at, image_key FROM signatures
    WHERE inspection_id = ${id} ORDER BY id`;
  const sigs = await hydrateSignatures(sigRows);

  const bytes = await buildInspectionPdf(insp, results, findings, sigs);
  const key = `pdf/INSP-${String(id).padStart(6, "0")}.pdf`;
  await files().set(key, bytes.buffer as ArrayBuffer, {
    metadata: { contentType: "application/pdf" },
  });
  await db.sql`UPDATE inspections SET pdf_key = ${key} WHERE id = ${id}`;
  return key;
}

// ---------------------------------------------------------------------------
// 每日協議巡視表
// ---------------------------------------------------------------------------
async function createCoordination(req: Request, me: SessionUser): Promise<Response> {
  const b = await req.json();
  const badBuilding = buildingTooLong(b.building);
  if (badBuilding) return badBuilding;
  const [co] = await db.sql`
    INSERT INTO coordinations
      (site_id, building, meeting_date, work_date, weather, agreement_text, patrol_text,
       handling_text, recorder_name, status, submitted_at, created_by)
    VALUES (${Number(b.site_id)}, ${(b.building || "").trim() || null},
            ${b.meeting_date || todayISO()},
            ${b.work_date || todayISO()}, ${b.weather ?? null},
            ${b.agreement_text ?? null}, ${b.patrol_text ?? null},
            ${b.handling_text ?? null},
            ${(b.recorder_name || "").trim() || null},
            'submitted', NOW(), ${me.id})
    RETURNING id, site_id, building`;

  for (const a of b.attendees || []) {
    await db.sql`
      INSERT INTO coordination_attendees
        (coordination_id, work_item, vendor_id, vendor_name, trade,
         person_name, employee_no, work_content)
      VALUES (${co.id}, ${a.work_item ?? null},
              ${a.vendor_id ? Number(a.vendor_id) : null}, ${a.vendor_name ?? null},
              ${a.trade ?? null}, ${a.person_name ?? null},
              ${a.employee_no ?? null}, ${a.work_content ?? null})`;
  }

  const findingIds: number[] = [];
  for (const f of b.findings || []) {
    findingIds.push(await insertFinding(
      // 補登紙本時，缺失的發現時間預設跟隨該單的作業日期
      { found_at: b.work_date ? `${b.work_date}T09:00:00+08:00` : undefined,
        ...f, source: "coordination", building: co.building },
      me, { siteId: co.site_id, coordinationId: co.id },
    ));
  }

  await saveSignatures(b.signatures || [], me, { coordinationId: co.id });
  await renderCoordinationPdf(co.id);

  return json({
    ok: true, coordination_id: co.id,
    pdf_url: `/api/coordinations/${co.id}/pdf`, finding_ids: findingIds,
  });
}

async function renderCoordinationPdf(id: number): Promise<string | null> {
  const co = (await db.sql`
    SELECT c.*, s.name AS site_name FROM coordinations c
    JOIN sites s ON s.id = c.site_id WHERE c.id = ${id}`)[0];
  if (!co) return null;

  const attendees = await db.sql`
    SELECT a.*, COALESCE(a.vendor_name, v.name) AS vendor_name
    FROM coordination_attendees a LEFT JOIN vendors v ON v.id = a.vendor_id
    WHERE a.coordination_id = ${id} ORDER BY a.id`;

  const findings = await db.sql`
    SELECT f.*, v.name AS vendor_name FROM findings f
    LEFT JOIN vendors v ON v.id = f.vendor_id
    WHERE f.coordination_id = ${id} ORDER BY f.id`;

  const sigRows = await db.sql`
    SELECT role, signer_name, signed_at, image_key FROM signatures
    WHERE coordination_id = ${id} ORDER BY id`;
  const sigs = await hydrateSignatures(sigRows);

  const bytes = await buildCoordinationPdf(co, attendees, findings, sigs);
  const key = `pdf/COORD-${String(id).padStart(6, "0")}.pdf`;
  await files().set(key, bytes.buffer as ArrayBuffer, {
    metadata: { contentType: "application/pdf" },
  });
  await db.sql`UPDATE coordinations SET pdf_key = ${key} WHERE id = ${id}`;
  return key;
}

// ---------------------------------------------------------------------------
// 簽名與 PDF 取回
// ---------------------------------------------------------------------------
async function saveSignatures(
  list: any[], me: SessionUser, ids: { inspectionId?: number; coordinationId?: number },
): Promise<number> {
  let n = 0;
  for (const s of list) {
    const key = await saveDataUrl(s.image, "signatures");
    await db.sql`
      INSERT INTO signatures
        (inspection_id, coordination_id, role, signer_id, signer_name, image_key)
      VALUES (${ids.inspectionId ?? null}, ${ids.coordinationId ?? null},
              ${s.role || "檢查人員"}, ${me.id}, ${s.signer_name || me.name}, ${key})`;
    n++;
  }
  return n;
}

async function hydrateSignatures(rows: any[]): Promise<SigInput[]> {
  return Promise.all(rows.map(async (s) => ({
    role: s.role,
    signer_name: s.signer_name,
    signed_at: new Date(s.signed_at)
      .toLocaleString("zh-TW", { timeZone: TZ, hour12: false }),
    image: await readFileBytes(s.image_key),
  })));
}

async function servePdf(table: "inspections" | "coordinations", id: number) {
  const rows = table === "inspections"
    ? await db.sql`SELECT pdf_key FROM inspections WHERE id = ${id}`
    : await db.sql`SELECT pdf_key FROM coordinations WHERE id = ${id}`;
  const key = rows[0]?.pdf_key;
  if (!key) return fail(404, "PDF 尚未產生");
  const data = await files().get(key, { type: "arrayBuffer" });
  if (!data) return fail(404, "PDF 檔案不存在");
  return new Response(data as ArrayBuffer, {
    headers: {
      "content-type": "application/pdf",
      "content-disposition": `inline; filename="${key.split("/").pop()}"`,
    },
  });
}

export const config: Config = {
  path: "/api/*",
};
