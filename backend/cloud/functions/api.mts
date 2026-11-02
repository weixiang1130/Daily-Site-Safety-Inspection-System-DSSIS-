// 職安填報系統 API（Netlify Functions）
//
// 單一函式處理所有 /api/* 路由，減少冷啟動並讓 session 處理集中在一處。
// 資料層：Netlify Database（Postgres）
// 檔案層：Netlify Blobs（照片、手寫簽名、產出的 PDF）

import type { Config, Context } from "@netlify/functions";
import { createHash } from "node:crypto";
import { getDatabase } from "@netlify/database";
import { getDeployStore, getStore } from "@netlify/blobs";

import {
  clearSessionCookie, createSession, hashPassword, readSession,
  sessionCookieHeader, verifyPassword, type SessionUser,
} from "../lib/auth.ts";
import {
  buildCoordinationPdf, buildFindingReportPdf, buildInspectionPdf, imageKind, type PhotoGroup, type SigInput,
} from "../lib/pdf.ts";
import { boardPayload, validateBoard } from "../lib/site-board.ts";
import {
  OPTION_FIELDS, PERSON_SEPARATOR, UNNAMED_VENDOR, cleanPerson, cleanTaskText, cleanVendor, learnFormOptions,
  reportVendor,
  refreshFormOptions, splitTrades,
} from "../lib/form-options.ts";

const db = getDatabase();
/** 給 lib 用的查詢函式：db.sql 依賴 this，不能直接傳出去。 */
const sqlTag = (strings: TemplateStringsArray, ...values: any[]): Promise<any[]> =>
  (db.sql as any)(strings, ...values);

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
  // 出工資料庫（/api/worklog/*）只在雲端。地端與工地檢視器共用同一份 home.html，
  // 沒有這個旗標就不顯示入口，否則點進去是 404。
  worklog_db: true,
};

// 過期判定改在前端做（frontend/dashboard-detail.html 的 STALE_AFTER_MIN）：
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

/** PDF 用的照片總量。串流回應上限 20MB，字型約 1MB；函式裡沒有影像程式庫，照片是原檔放入。 */
const PDF_PHOTO_BYTES = 12 * 1024 * 1024;

/** 缺失 → PDF 附頁的照片。讀不到或格式不支援的只計數，不能讓整份 PDF 產不出來。 */
async function findingPhotos(findings: any[]): Promise<PhotoGroup[]> {
  let left = PDF_PHOTO_BYTES;
  const groups: PhotoGroup[] = [];
  for (const f of findings) {
    const g: PhotoGroup = {
      no: `F${String(f.id).padStart(6, "0")}`, text: f.description,
      sub: [f.location, [f.vendor_name ?? f.vendor, f.responsible_person].filter(Boolean).join("／")]
        .filter(Boolean).join("　"),
      before: [], after: [], broken: 0, skipped: 0,
    };
    for (const which of ["before", "after"] as const) {
      const paths = String(f[`photo_${which}`] ?? "").split("\n").map((s) => s.trim())
        .filter((s) => s.startsWith(FILE_PREFIX));
      for (const bytes of await Promise.all(paths.map(readFileBytes))) {
        if (!bytes || !imageKind(bytes)) g.broken++;
        else if (bytes.length > left) g.skipped++;
        else { left -= bytes.length; g[which].push(bytes); }
      }
    }
    groups.push(g);
  }
  return groups;
}

/** PDF 回應。一定要串流：一般回應的二進位上限約 4.5MB，放了照片的 PDF 會下載失敗。 */
function pdfResponse(body: ReadableStream | Uint8Array, name: string) {
  const stream = body instanceof Uint8Array ? new Blob([body as BlobPart]).stream() : body;
  return new Response(stream, {
    headers: {
      "content-type": "application/pdf",
      "content-disposition": `inline; filename="${name}"`,
    },
  });
}

// ---------------------------------------------------------------------------
// 工地看板（雲端自主版）
//
// 外部三塊（氣象/出工/新知/危害/每日出工總數）由 GitHub 排程每 30 分鐘抓取、
// 推到 EXTERNAL_KEY（逐區塊合併，收集失敗的區塊保留上一份與它的時間戳）；
// 要資料庫的兩塊（缺失統計、看板設定）快取在 DBSECTIONS_KEY，由寫入動作
// 觸發重算（見主處理上方的 BOARD_WRITE），另有兩個「讀取時」的失效條件：
// 換日（今日缺失／逾期數隨日期而變）與主場站代碼變更。board-data 正常情況
// 只讀兩份 Blob——一天最多因換日喚醒資料庫一次。
// ---------------------------------------------------------------------------
const EXTERNAL_KEY = "wallboard/external.json";
const DBSECTIONS_KEY = "wallboard/dbsections.json";
// 看板失更門檻（分鐘）：推送 120＋回應快取 5＋前端輪詢 15，正常最大資料年齡
// 約 140 分，GitHub 排程在尖峰常晚 10~30 分——180 分才不會在正常運作下誤報。
// 由伺服器下發，前端不必各自寫死。收集頻率改動時這個值要一起改：小於「推送
// 間隔＋40 分」會在一切正常時亮警示，大到兩倍以上則漏跑整輪也看不出來。
const WALL_STALE_AFTER_MIN = 180;
const DAILY_TOTALS_KEEP_DAYS = 100;

async function readJsonBlob(key: string): Promise<any> {
  try {
    const raw = await files().get(key, { type: "text" });
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function primaryCode(): string {
  return (Netlify.env.get("PRIMARY_SITE_CODE") || "").trim();
}

/** 缺失三數（首頁概況與工地看板共用同一份口徑）。逾期條件須與地端
 *  Finding.is_overdue 一致——改這裡時兩邊要一起改。as_of 取資料庫時鐘，
 *  供快取寫入時判斷新舊。 */
async function findingStats() {
  const today = todayISO();
  const rows = await db.sql`
    SELECT
      -- 今日＝今天新填的。作業日期不行：多數的單隔天才補填，永遠算不進「今日」
      COUNT(*) FILTER (WHERE (created_at AT TIME ZONE 'Asia/Taipei')::date = ${today}::date) AS findings_today,
      COUNT(*) FILTER (WHERE status IN ('open', 'fixed'))     AS open,
      COUNT(*) FILTER (
        WHERE action_type = 'scheduled'
          AND due_date IS NOT NULL
          AND status NOT IN ('verified', 'closed')
          AND due_date < ${today}::date)                       AS overdue,
      clock_timestamp()                                         AS as_of
    FROM findings`;
  const r: any = rows[0] || {};
  return {
    findings_today: Number(r.findings_today || 0),
    open: Number(r.open || 0),
    overdue: Number(r.overdue || 0),
    as_of: new Date(r.as_of).getTime() || Date.now(),
    date: today,
  };
}

/** 缺失三數＋主場站看板設定。 */
async function computeDbSections() {
  const st = await findingStats();
  const code = primaryCode();
  let site: any = null;
  if (code) {
    [site] = await db.sql`
      SELECT id, code, name, active, board_config, board_revision
      FROM sites WHERE code = ${code} AND active = TRUE`;
  }
  if (!site) {
    [site] = await db.sql`
      SELECT id, code, name, active, board_config, board_revision
      FROM sites WHERE active = TRUE ORDER BY sort_order, id LIMIT 1`;
  }
  return {
    stats: { findings_today: st.findings_today, open: st.open, overdue: st.overdue },
    stats_date: st.date,
    as_of: st.as_of,
    primary_code: code,
    board: site ? boardPayload(site) : null,
  };
}

/** 寫入 DB 區塊快取。兩個請求同時重算時，查詢較晚者看到的資料不會更舊，
 *  因此以 as_of（資料庫時鐘）判斷：已存的比較新就不覆蓋。 */
async function writeDbSections(sec: any) {
  const cur = await readJsonBlob(DBSECTIONS_KEY);
  if (cur && cur.stats_date === sec.stats_date && cur.primary_code === sec.primary_code
      && Number(cur.as_of) > Number(sec.as_of)) return;
  await files().set(DBSECTIONS_KEY, JSON.stringify(sec),
    { metadata: { pushed_at: new Date().toISOString() } });
}

/** 寫入動作後重算快取。盡力而為——失敗不能拖累觸發它的填報動作。 */
async function refreshBoardCache() {
  try {
    await writeDbSections(await computeDbSections());
  } catch (e) {
    console.error("[board-cache] 更新失敗（換日或下次寫入會再試）", e);
  }
}

/** 無災害天數：起算日當天算第 1 天，讀取時以純日期運算。 */
function computeRecord(safety: any, lastMonth: number) {
  const base = parseInt(safety?.base_days || "0", 10) || 0;
  const startRaw = String(safety?.start_date || "").trim();
  let days: number | null = null;
  if (startRaw) {
    const start = new Date(startRaw + "T00:00:00Z");
    const today = new Date(todayISO() + "T00:00:00Z");
    if (!isNaN(start.getTime()) && start <= today) {
      days = base + Math.round((today.getTime() - start.getTime()) / 86400000) + 1;
    }
  }
  return { days, last_month_mandays: lastMonth, since: startRaw || null };
}

/** 上月總出工（人日）：由雲端逐日累積保存的每日總數加總。 */
function lastMonthMandays(ext: any): number {
  const totals = ext.daily_totals;
  if (!totals || typeof totals !== "object") return Number(ext.last_month_mandays || 0);
  const [y, m] = todayISO().split("-").map(Number);
  const prefix = m === 1 ? `${y - 1}-12-` : `${y}-${String(m - 1).padStart(2, "0")}-`;
  let sum = 0;
  for (const [d, v] of Object.entries(totals)) if (d.startsWith(prefix)) sum += Number(v) || 0;
  return sum;
}

// ---------------------------------------------------------------------------
// 門禁在場人數：雲端直接向人臉辨識設備讀（設備在公網；/admin/list 只回各門的
// 彙總人次 inCount／outCount／presenceCount，不碰任何個人資料）。
// 看板每次讀取都會經過這裡，結果存 Blob、隔 FACE_REFRESH_MIN 分鐘才重讀一次
// （多面螢幕共用一次讀取）。設備連不上時沿用上一筆（附讀數時間），超過
// 「間隔＋1 小時」就不顯示——在場人數是疏散點名用的，過時的數字比沒有更糟。
// 設定（Netlify 環境變數，不進版控）：FACE_URL＝設備位址；FACE_SITE_MAP＝
// 門名關鍵字→工地代碼的 JSON，例 {"*":"BD04"}（"*" 表示所有門加總到該工地）；
// FACE_REFRESH_MIN＝重讀間隔（分），預設 120（試辦期與其他資料一樣兩小時），
// 正式啟用改 5。改環境變數後要重新部署才生效。
// 規則與地端 collectors/face.py 相同，改一邊要跟著改另一邊。
// ---------------------------------------------------------------------------
const FACE_KEY = "face/headcount.json";
function faceRefreshMin(): number {
  const v = parseInt(Netlify.env.get("FACE_REFRESH_MIN") || "", 10);
  return Number.isFinite(v) && v >= 1 ? Math.min(v, 24 * 60) : 120;
}

async function faceHeadcount(): Promise<any> {
  const base = (Netlify.env.get("FACE_URL") || "").trim().replace(/\/+$/, "");
  const code = primaryCode();
  if (!base || !code) {
    // 改了 Netlify 環境變數要重新部署才生效——沒設好時在函式日誌留一句，免得盲查
    console.warn(`[face] 未設定${base ? "" : " FACE_URL"}${code ? "" : " PRIMARY_SITE_CODE"}，略過門禁人數`);
    return null;
  }
  const now = Date.now();
  const refreshMin = faceRefreshMin();
  const FACE_TTL_MS = refreshMin * 60000;
  const FACE_MAX_AGE_MS = FACE_TTL_MS + 60 * 60000;
  // 前端據此判斷讀數「該到了還沒到」（設備連不上）才轉紅字
  const withAge = (d: any) => (d ? { ...d, expect_within_min: refreshMin + 15 } : null);
  const cached = await readJsonBlob(FACE_KEY);
  const mine = cached && cached.code === code ? cached : null;
  if (mine && now - Number(mine.fetched_ms || 0) < FACE_TTL_MS) return withAge(mine.data);
  const save = (rec: any) => files().set(FACE_KEY, JSON.stringify(rec)).catch(() => {});
  let map: Record<string, string> = {};
  try { map = JSON.parse(Netlify.env.get("FACE_SITE_MAP") || "{}") || {}; } catch { map = {}; }
  try {
    const r = await fetch(`${base}/admin/list`, { signal: AbortSignal.timeout(4000) });
    if (!r.ok) throw Error(`HTTP ${r.status}`);
    const body: any = await r.json();
    if (![0, 200, undefined, null].includes(body?.code)) throw Error("裝置回應非正常");
    const gates: any[] = Array.isArray(body?.data) ? body.data : [];
    const n = (v: any) => { const x = Math.trunc(Number(v)); return Number.isFinite(x) ? x : 0; };
    const sum = { in: 0, out: 0, present: 0 };
    let found = false;
    for (const g of gates) {
      const pname = String(g?.pname ?? "");
      const gc = map["*"] || Object.entries(map).find(([kw]) => kw !== "*" && pname.includes(kw))?.[1];
      if (gc !== code) continue;
      found = true;
      sum.in += n(g.inCount); sum.out += n(g.outCount); sum.present += n(g.presenceCount);
    }
    // 對不到主場站的門就不顯示——不把別處的人次掛到這個工地
    const data = found ? { headcount_present: sum.present, headcount_in: sum.in, headcount_out: sum.out,
                           reading_at: minuteISO(new Date(now).toISOString()) } : null;
    await save({ code, fetched_ms: now, ok_ms: now, data });
    return withAge(data);
  } catch (e: any) {
    // 日誌只記錯誤類型與訊息，不記設備位址
    console.warn(`[face] 讀取門禁人數失敗：${e?.name || "Error"} ${String(e?.message || "").slice(0, 120)}`);
    // 連不上：記下這次嘗試（一個間隔內不再重試、不拖慢看板），上一筆在期限內照用
    const keep = mine && mine.data && now - Number(mine.ok_ms || 0) < FACE_MAX_AGE_MS ? mine.data : null;
    await save({ code, fetched_ms: now, ok_ms: mine?.ok_ms || 0, data: keep });
    return withAge(keep);
  }
}

/** 合併外部 Blob 與 DB 快取成看板整頁。 */
async function buildBoardData() {
  const headcountP = faceHeadcount();      // 與其他讀取同時進行
  const ext = (await readJsonBlob(EXTERNAL_KEY)) || {};
  let sec = await readJsonBlob(DBSECTIONS_KEY);
  // 快取失效：不存在、換日（今日缺失／逾期數依日期而變，不能沿用昨天
  // 算的）、主場站代碼變了（Blob 不隨部署清除）。一天最多喚醒一次。
  if (!sec || sec.stats_date !== todayISO() || sec.primary_code !== primaryCode()) {
    sec = await computeDbSections();
    try { await writeDbSections(sec); } catch { /* 盡力 */ }
  }

  // 資料新舊只看「會變的外部區塊」：氣象與出工各自最後成功送達的時間取
  // 較舊者。任一塊從未送達就回 null，前端顯示「尚未收到」，不拿別的時間遮過去。
  let generated_at: string | null = null;
  if (ext.section_at) {
    const { station, worklog } = ext.section_at;
    generated_at = station && worklog ? (station < worklog ? station : worklog) : null;
  } else if (ext.generated_at) {
    generated_at = ext.generated_at;           // 舊格式快照（過渡期相容）
  }

  return {
    generated_at,
    stale_after_min: WALL_STALE_AFTER_MIN,
    failed: ext.failed || [],
    station: ext.station ?? null,
    stats: sec.stats,
    worklog: ext.worklog ?? { date: todayISO(), total: 0, rows: [] },
    record: computeRecord(sec.board?.config?.safety || {}, lastMonthMandays(ext)),
    hazards: ext.hazards ?? [],
    news: ext.news ?? [],
    board: sec.board,
    headcount: await headcountP,
  };
}

// ---------------------------------------------------------------------------
// 主處理
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// 出工資料庫（worklog_*）：寫入由 GitHub 每晚推送，匯出供分析
// ---------------------------------------------------------------------------
const WORKLOG_MAX_REPORTS = 1000;   // 單次推送上限；runner 會分批
const WORKLOG_MAX_OWNED = 50000;    // 「訊息→鍵」對照的上限（全部歷史回補時一次送完）
const SITE_CODE_RE = /^[A-Za-z0-9_-]{1,32}$/;
// 單一廠商一天的人數上限。解析器會把「聯絡電話：0912345678」這類行當成人數，
// 超過 int 範圍時整批寫入失敗、之後每晚卡在同一批——寧可把離譜值存成 NULL。
const WORKLOG_MAX_HEADCOUNT = 2000;

/** YYYY-MM-DD 且是真實存在的日期。只驗格式的話 2026-02-30 會通過，
 *  到 ::date 轉型才失敗，變成 500 而不是 400。 */
function isIsoDate(v: unknown): boolean {
  const t = String(v ?? "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(t)) return false;
  const d = new Date(`${t}T00:00:00Z`);
  return !isNaN(d.getTime()) && d.toISOString().slice(0, 10) === t;
}

/** 匯出游標 → [缺失時間, id, 巡檢時間, id, 協議時間, id]。舊版地端存的是單一時間，三張表都從那裡接。 */
function exportCursor(raw: string): (string | number)[] {
  const parts = raw.split("~").map((x) => x.split("|"));
  if (parts.length === 3 && parts.every(([at, id]) => !isNaN(Date.parse(at)) && /^\d+$/.test(id ?? ""))) {
    return parts.flatMap(([at, id]) => [at, parseInt(id, 10)]);
  }
  const at = raw && !isNaN(Date.parse(raw)) ? new Date(raw).toISOString() : new Date(0).toISOString();
  return [at, 0, at, 0, at, 0];
}

function headcountOf(v: unknown): number | null {
  return Number.isInteger(v) && (v as number) >= 0 && (v as number) <= WORKLOG_MAX_HEADCOUNT
    ? (v as number) : null;
}

/** CSV 儲存格：含逗號／引號／換行時加引號；開頭是 = + - @ 時前綴單引號，
 *  避免 Excel 把 LINE 原文當公式執行（CSV injection）。 */
function csvCell(v: unknown): string {
  if (v === null || v === undefined) return "";
  let t = String(v);
  if (/^[=+\-@]/.test(t)) t = "'" + t;
  return /[",\r\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
}

/** 帶 BOM 的 UTF-8 CSV——沒有 BOM 的話 Excel 會把中文開成亂碼。
 *  檔名：HTTP 標頭只能放 Latin-1，中文檔名直接放進去 new Response() 會丟例外
 *  （每個匯出都變 500）。filename 給 ASCII 備援、filename* 給 RFC 5987 編碼的中文名。 */
function csvResponse(filename: string, asciiName: string, header: string[], rows: unknown[][]): Response {
  const body = "﻿" + [header, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n");
  return new Response(body, {
    headers: {
      "content-type": "text/csv; charset=utf-8",
      "content-disposition":
        `attachment; filename="${asciiName}"; filename*=UTF-8''${encodeURIComponent(filename)}`,
      "cache-control": "no-store",
    },
  });
}

// 會改動看板 DB 區塊（缺失、工地、看板設定）的寫入路由。在單一出口統一刷新
// 快取，而不是在各處理器逐一呼叫——逐一加的做法曾漏掉協調會新增、刪除協調
// 會／檢查、改工地名稱等路徑。刷新只在寫入成功後做，資料庫此時本來就醒著。
const BOARD_WRITE = /^\/api\/(inspections|coordinations|findings|sign\/\w+\/\d+\/(finish|void)|edit|site-board|admin\/(sites|findings|coordinations|inspections))(\/|$)/;

export default async (req: Request, ctx: Context): Promise<Response> => {
  const resp = await handle(req, ctx);
  if (req.method.toUpperCase() !== "GET" && resp.ok
      && BOARD_WRITE.test(new URL(req.url).pathname.replace(/\/+$/, ""))) {
    await refreshBoardCache();
  }
  return resp;
};

async function handle(req: Request, _ctx: Context): Promise<Response> {
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

    // 公司標誌（看板版頭）。真實 LOGO 不進公開版控，由地端主機以代理權杖
    // 上傳到 Blob（backend/tools/upload_brand_logo.py）；沒上傳就 404，
    // 看板改顯示文字公司名。讀取不需登入：牆上大螢幕沒有 session。
    const brandAsset = /^\/api\/(v1\/ingest\/)?brand\/(mark|wordmark)$/.exec(p);
    if (brandAsset && !brandAsset[1] && method === "GET") {
      const img = await files().get(`brand/${brandAsset[2]}`, { type: "arrayBuffer" });
      if (!img) return fail(404, "未設定公司標誌");
      return new Response(img, {
        headers: { "content-type": "image/png", "cache-control": "public, max-age=86400" },
      });
    }
    if (brandAsset && brandAsset[1] && method === "POST") {
      const expected = Netlify.env.get("SITE_AGENT_TOKEN") || "";
      const token = req.headers.get("x-agent-token") || "";
      if (!expected || token !== expected) return fail(401, "代理權杖驗證失敗");
      const buf = await req.arrayBuffer();
      const head = new Uint8Array(buf.slice(0, 8));
      const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
      if (buf.byteLength > 512 * 1024 || !PNG.every((b, i) => head[i] === b)) {
        return fail(400, "標誌須為 512 KB 以內的 PNG");
      }
      await files().set(`brand/${brandAsset[2]}`, buf);
      return json({ ok: true, bytes: buf.byteLength });
    }

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

    // 工地看板的「外部三塊」——由 GitHub 排程每 30 分鐘抓取後推上來。只寫 Blob、
    // 不碰資料庫、不觸發部署。權杖 WALL_INGEST_TOKEN（＝GitHub 端 CLOUD_INGEST_TOKEN）。
    // 逐區塊合併：收集失敗的區塊不會出現在推送內容裡，這裡就保留上一份好的
    // 資料與它原本的時間戳——壞掉的一塊不能把正常資料洗成空白，也不能因為
    // 其他塊成功就假裝它是新的。
    if (p === "/api/v1/ingest/external" && method === "POST") {
      const expected = Netlify.env.get("WALL_INGEST_TOKEN") || "";
      const token = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
      if (!expected || token !== expected) return fail(401, "外部快照權杖驗證失敗");
      let inc: any;
      try {
        inc = JSON.parse(await req.text());
        if (!inc || typeof inc !== "object" || Array.isArray(inc)) throw 0;
        if (!["station", "worklog", "worklog_sites", "news"].some((k) => k in inc)) throw 0;
        if ("worklog" in inc && !Array.isArray(inc.worklog?.rows)) throw 0;
        if ("news" in inc && !Array.isArray(inc.news)) throw 0;
        if ("daily_totals" in inc && (!inc.daily_totals || typeof inc.daily_totals !== "object"
            || Array.isArray(inc.daily_totals))) throw 0;
      } catch { return fail(400, "外部快照格式錯誤"); }

      const at = String(inc.generated_at || minuteISO(new Date().toISOString()));
      const prev = (await readJsonBlob(EXTERNAL_KEY)) || {};
      const out: any = { ...prev, section_at: { ...(prev.section_at || {}) } };
      delete out.generated_at;
      delete out.last_month_mandays;
      if ("station" in inc) {
        out.station = inc.station;
        out.section_at.station = at;
      }
      if ("worklog" in inc) {
        out.worklog = inc.worklog;
        out.hazards = Array.isArray(inc.hazards) ? inc.hazards : [];
        out.section_at.worklog = at;
        const merged: Record<string, number> = { ...(prev.daily_totals || {}), ...(inc.daily_totals || {}) };
        const cutoff = new Date(Date.now() - DAILY_TOTALS_KEEP_DAYS * 86400000).toISOString().slice(0, 10);
        for (const d of Object.keys(merged)) if (d < cutoff) delete merged[d];
        out.daily_totals = merged;
      }
      // 其他工地的今日出工：協議表帶入用，不進 buildBoardData（看板只顯示主場站）。
      // 逐工地合併：這輪抓不到的工地沒有出現，保留上一份
      if (inc.worklog_sites && typeof inc.worklog_sites === "object" && !Array.isArray(inc.worklog_sites)) {
        out.worklog_sites = { ...(prev.worklog_sites || {}) };
        for (const [code, v] of Object.entries<any>(inc.worklog_sites)) {
          if (SITE_CODE_RE.test(code) && Array.isArray(v?.rows)) {
            out.worklog_sites[code] = { date: v.date, rows: v.rows, at };
          }
        }
      }
      if ("news" in inc) {
        out.news = inc.news;
        out.section_at.news = at;
      }
      out.failed = Array.isArray(inc.failed) ? inc.failed : [];
      const body = JSON.stringify(out);
      await files().set(EXTERNAL_KEY, body, { metadata: { pushed_at: new Date().toISOString() } });
      return json({ ok: true, bytes: Buffer.byteLength(body, "utf8"), failed: out.failed });
    }

    // 出工資料庫：GitHub 每晚 00:07 推送最近 14 天解析結果（首次為全部歷史）。
    // 以 (工地, 日期, 棟別, 廠商) upsert；來源消失的列不刪（來源被截斷不能丟歷史）。
    // 每個工地一個來源，runner 逐工地推；site_code 未給時視為主場站（舊版 runner）。
    // 訊息因解析規則改進而改判到不同鍵時，刪掉舊鍵那筆——包括被同鍵較新訊息
    // 蓋過、沒有被送出的舊訊息（superseded_message_ids），否則舊鍵那筆會留著
    // 讓人數重複。每步都是冪等的：中途失敗時 runner 以非 0 結束，隔晚重送即可補齊。
    if (p === "/api/v1/ingest/worklog" && method === "POST") {
      const expected = Netlify.env.get("WALL_INGEST_TOKEN") || "";
      const token = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
      if (!expected || token !== expected) return fail(401, "出工資料推送權杖驗證失敗");

      let reports: any[], rejects: any[], ownedIn: any;
      const primarySite = BRANDING.primary_site_code;
      const siteOf = (x: any) => (x?.site_code ? String(x.site_code) : primarySite);
      try {
        const b = JSON.parse(await req.text());
        reports = Array.isArray(b?.reports) ? b.reports : [];
        rejects = Array.isArray(b?.rejects) ? b.rejects : [];
        ownedIn = b?.owned;
        if (reports.length > WORKLOG_MAX_REPORTS) throw Error(`單次最多 ${WORKLOG_MAX_REPORTS} 筆`);
        if (ownedIn !== undefined && (!Array.isArray(ownedIn) || ownedIn.length > WORKLOG_MAX_OWNED)) {
          throw Error("owned 格式錯誤");
        }
        for (const x of [...reports, ...rejects, ...(ownedIn || [])]) {
          if (x?.site_code !== undefined && x.site_code !== null && !SITE_CODE_RE.test(String(x.site_code))) {
            throw Error("工地代碼格式錯誤");
          }
        }
        for (const r of reports) {
          if (!isIsoDate(r?.report_date) || !String(r?.vendor || "").trim()) {
            throw Error("回報缺日期或廠商");
          }
          if (r.trades !== undefined && !Array.isArray(r.trades)) throw Error("工種明細格式錯誤");
          if (r.superseded_message_ids !== undefined && !Array.isArray(r.superseded_message_ids)) {
            throw Error("superseded_message_ids 格式錯誤");
          }
        }
      } catch (e: any) {
        return fail(400, "出工資料格式錯誤：" + (e?.message || ""));
      }

      const seen = new Set<string>();
      const rows: any[] = [];
      for (const r of reports) {
        const site_code = siteOf(r);
        const building = String(r.building || "").slice(0, 32);
        const vendor = String(r.vendor).trim().slice(0, 64);
        // 鍵用截斷後的值：兩筆只在第 64 字後不同時，同一個 INSERT 內會撞同一列
        const key = `${site_code}|${r.report_date}|${building}|${vendor}`;
        if (seen.has(key)) continue;                  // 同鍵只取第一筆（runner 已去重）
        seen.add(key);
        rows.push({
          site_code, report_date: r.report_date, building, vendor,
          headcount: headcountOf(r.headcount),
          trade_summary: r.trade_summary ?? null, supervisor: r.supervisor ?? null,
          tasks: r.tasks ?? null, reporter: r.reporter ?? null,
          reported_at: r.reported_at ?? null, message_id: r.message_id ?? null,
          raw: r.raw ?? null,
          superseded: (r.superseded_message_ids || []).map(String).filter(Boolean),
          trades: (r.trades || []).filter((t: any) =>
            String(t?.trade || "").trim() && (headcountOf(t?.headcount) ?? 0) > 0),
        });
      }

      // 「訊息 → 它擁有的鍵」對照：每個回報鍵連同它蓋過的舊訊息。資料庫裡某則訊息
      // 留在對照以外的鍵上，就是改判前的舊列（解析規則一改，同一批人會在兩個鍵各算
      // 一次），要刪。一則「施工回報」會擁有十幾個鍵——所以要拿整組對照比，不能只看
      // 單一列（只看單列的話，同一則訊息的其他列全會被當成舊列刪掉）。
      //
      // 分批推送時，同一則訊息的鍵可能分在不同批；只拿本批比就會誤刪別批的列。所以
      // runner 在最後一批附上整個來源的完整對照（owned），前面幾批送空陣列＝不刪。
      // 沒有 owned 欄位（舊版 runner）時退回用本批推出來的對照。
      const owned: any[] = ownedIn !== undefined
        ? ownedIn.filter((o: any) => o?.message_id && isIsoDate(o?.report_date)).map((o: any) => ({
            site_code: siteOf(o), report_date: o.report_date,
            building: String(o.building || "").slice(0, 32),
            vendor: String(o.vendor || "").trim().slice(0, 64), message_id: String(o.message_id) }))
        : rows.flatMap((r) => [r.message_id, ...r.superseded].filter(Boolean)
            .map((m: string) => ({ site_code: r.site_code, report_date: r.report_date,
                                   building: r.building, vendor: r.vendor, message_id: m })));

      let upserted: any[] = [];
      if (owned.length) {
        await db.sql`
          WITH owned AS (
            SELECT * FROM json_to_recordset(${JSON.stringify(owned)}::json)
              AS x(site_code text, report_date date, building text, vendor text, message_id text)
          )
          DELETE FROM worklog_reports r
          WHERE r.message_id IN (SELECT message_id FROM owned)
            AND NOT EXISTS (SELECT 1 FROM owned o
                            WHERE o.message_id = r.message_id AND o.site_code = r.site_code
                              AND o.report_date = r.report_date AND o.building = r.building
                              AND o.vendor = r.vendor)`;
      }
      if (rows.length) {
        const rj = JSON.stringify(rows);
        upserted = await db.sql`
          INSERT INTO worklog_reports
            (site_code, report_date, building, vendor, headcount, trade_summary, supervisor,
             tasks, reporter, reported_at, message_id, raw)
          SELECT site_code, report_date, building, vendor, headcount, trade_summary, supervisor,
                 tasks, reporter, reported_at, message_id, raw
          FROM json_to_recordset(${rj}::json) AS x(
            site_code text, report_date date, building text, vendor text, headcount int,
            trade_summary text, supervisor text, tasks text, reporter text,
            reported_at timestamp, message_id text, raw text)
          ON CONFLICT (site_code, report_date, building, vendor) DO UPDATE SET
            headcount = EXCLUDED.headcount, trade_summary = EXCLUDED.trade_summary,
            supervisor = EXCLUDED.supervisor, tasks = EXCLUDED.tasks,
            reporter = EXCLUDED.reporter, reported_at = EXCLUDED.reported_at,
            message_id = EXCLUDED.message_id, raw = EXCLUDED.raw, updated_at = NOW()
          RETURNING id, site_code, report_date::text AS report_date, building, vendor`;

        const idOf = new Map(upserted.map((u: any) =>
          [`${u.site_code}|${u.report_date}|${u.building}|${u.vendor}`, u.id]));
        const trades: any[] = [];
        for (const r of rows) {
          const id = idOf.get(`${r.site_code}|${r.report_date}|${r.building}|${r.vendor}`);
          if (id) for (const t of r.trades) trades.push({ report_id: id, trade: String(t.trade).trim().slice(0, 32), headcount: t.headcount });
        }
        const ids = JSON.stringify(upserted.map((u: any) => u.id));
        // 工種明細整組替換，放在同一個語句裡：分成「全刪」「再寫」兩句時，寫入
        // 那句失敗會讓這批回報只剩刪除、要等隔晚才補回來。同一語句內不能先刪
        // 再插同一個主鍵（PostgreSQL 會判重複），所以只刪「新明細裡沒有的工種」，
        // 其餘以 upsert 更新人數——兩者碰不到同一列。
        await db.sql`
          WITH fresh AS (
            SELECT report_id, trade, SUM(headcount)::int AS headcount
            FROM json_to_recordset(${JSON.stringify(trades)}::json)
              AS t(report_id int, trade text, headcount int)
            GROUP BY report_id, trade
          ), gone AS (
            DELETE FROM worklog_trades w
            WHERE w.report_id IN (SELECT value::int FROM json_array_elements_text(${ids}::json))
              AND NOT EXISTS (SELECT 1 FROM fresh f
                              WHERE f.report_id = w.report_id AND f.trade = w.trade)
          )
          INSERT INTO worklog_trades (report_id, trade, headcount)
          SELECT report_id, trade, headcount FROM fresh
          ON CONFLICT (report_id, trade) DO UPDATE SET headcount = EXCLUDED.headcount`;
      }
      if (owned.length) {
        // 這次解析成功的訊息（含被蓋過的舊訊息），從解析失敗清單移除
        const okIds = JSON.stringify(owned.map((o) => o.message_id));
        await db.sql`
          DELETE FROM worklog_rejects
          WHERE message_id IN (SELECT value FROM json_array_elements_text(${okIds}::json))`;
      }

      const rejectRows = [...new Map(rejects
        .filter((x: any) => x?.message_id)
        .map((x: any) => [String(x.message_id), {
          message_id: String(x.message_id), site_code: siteOf(x), reported_at: x.reported_at ?? null,
          reporter: x.reporter ?? null, raw: x.raw ?? null }])).values()];
      if (rejectRows.length) {
        await db.sql`
          INSERT INTO worklog_rejects (message_id, site_code, reported_at, reporter, raw)
          SELECT message_id, site_code, reported_at, reporter, raw
          FROM json_to_recordset(${JSON.stringify(rejectRows)}::json)
            AS x(message_id text, site_code text, reported_at timestamp, reporter text, raw text)
          ON CONFLICT (message_id) DO UPDATE SET site_code = EXCLUDED.site_code,
            reported_at = EXCLUDED.reported_at,
            reporter = EXCLUDED.reporter, raw = EXCLUDED.raw, updated_at = NOW()`;
      }

      // 填報預設清單跟著出工資料更新。排在每晚同一次喚醒裡，不多喚醒資料庫；
      // 只在最後一批（附完整 owned）做，一個工地重算一次就夠。失敗不影響出工資料
      if (ownedIn === undefined || ownedIn.length) {
        const sites = new Set<string>([...rows, ...owned].map((r: any) => r.site_code));
        for (const code of sites) {
          try { await refreshFormOptions(sqlTag, code); }
          catch (e: any) { console.warn(`[form-options] ${code} 重算失敗：${e?.message || e}`); }
        }
      }
      return json({ ok: true, reports: upserted.length, rejects: rejectRows.length });
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

    // 工地看板（五區塊）的雲端自主來源：合併「外部 Blob」（GitHub 排程推的
    // 氣象/出工/新知/危害）與「DB 快取 Blob」（填報時事件驅動更新的缺失/設定）。
    // 正常情況只讀兩份 Blob、不碰資料庫。驗證同上（WALL_TOKEN 或登入）。
    // site_id 只是路由佔位：看板固定顯示主場站。
    const boardData = /^\/api\/board-data\/\d+$/.exec(p);
    if (boardData && method === "GET") {
      const expected = Netlify.env.get("WALL_TOKEN") || "";
      const key = url.searchParams.get("k") || "";
      const tokenOk = expected !== "" && key === expected;
      if (!tokenOk && !me) {
        return fail(401, key
          ? "看板權杖錯誤"
          : "請先登入，或使用看板權杖網址（未設定 WALL_TOKEN 時大螢幕無法免登入）");
      }
      const data = await buildBoardData();
      if (!data.board) return fail(404, "尚未設定主場站，或沒有啟用中的工地");
      // 5 分鐘：看板資料不碰資料庫，回源便宜；快取太長會墊高資料年齡而誤報失更
      const cache = tokenOk ? "public, max-age=300" : "private, max-age=300";
      return new Response(JSON.stringify(data), {
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

      // 增量取得：只回傳游標之後異動的資料。首次同步不帶參數即可全取。
      // 三張表各記各的「更新時間|id」：遷移或批次更新會讓整批列同一時間，只用時間當游標
      // 的話，切在分頁邊界的同時間資料會被跳過，也不會因為某張表比較落後而卡住其他表。
      const since = url.searchParams.get("since") || "";
      const [fAt, fId, iAt, iId, cAt, cId] = exportCursor(since);

      // 上限保護：資料累積幾年後不設限的全量查詢會讓函式逾時，
      // 地端拿到一半的資料卻以為同步完成，之後就再也補不回來。
      const LIMIT = 2000;

      const sites = await db.sql`
        SELECT code, name, department, sort_order, active, board_config, board_revision FROM sites`;
      const vendors = await db.sql`
        SELECT code, name, active FROM vendors`;
      const findings = await db.sql`
        SELECT f.id, s.code AS site_code, f.building, f.source, f.found_at, f.location,
               f.hazard_code, f.hazard_label, f.description, v.code AS vendor_code,
               f.severity, f.action_type, f.due_date, f.fixed_at, f.verified_at,
               f.status, f.created_at, f.updated_at, f.responsible_person, f.fix_note,
               f.penalty, f.dispositions, f.review_note, f.inspection_id, f.coordination_id,
               to_char(f.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS cursor_at
        FROM findings f
        JOIN sites s ON s.id = f.site_id
        LEFT JOIN vendors v ON v.id = f.vendor_id
        WHERE (f.updated_at, f.id) > (${fAt}::timestamptz, ${fId}::int)
        ORDER BY f.updated_at, f.id LIMIT ${LIMIT}`;
      const inspections = await db.sql`
        SELECT i.id, s.code AS site_code, i.building, i.form_code, i.inspect_date,
               i.location, i.inspector_name, i.vendor_name, i.status, i.submitted_at,
               i.created_at, i.updated_at,
               to_char(i.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS cursor_at,
               (SELECT COUNT(*) FROM inspection_results r
                 WHERE r.inspection_id = i.id AND r.result = 'fail') AS fail_count
        FROM inspections i
        JOIN sites s ON s.id = i.site_id
        WHERE (i.updated_at, i.id) > (${iAt}::timestamptz, ${iId}::int)
        ORDER BY i.updated_at, i.id LIMIT ${LIMIT}`;
      const coordinations = await db.sql`
        SELECT c.id, s.code AS site_code, c.building, c.meeting_date, c.work_date,
               c.recorder_name, c.status, c.submitted_at, c.created_at, c.updated_at,
               to_char(c.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS cursor_at,
               (SELECT COUNT(*) FROM coordination_attendees a
                 WHERE a.coordination_id = c.id) AS attendee_count
        FROM coordinations c
        JOIN sites s ON s.id = c.site_id
        WHERE (c.updated_at, c.id) > (${cAt}::timestamptz, ${cId}::int)
        ORDER BY c.updated_at, c.id LIMIT ${LIMIT}`;

      // 地端拿這個當下一輪的 since。用資料本身的位置而不是「現在」——用「現在」的話，
      // 查詢期間才寫進來的資料會被永遠跳過。updated_at 由資料庫觸發器維護，退回、修改、
      // 作廢都會動到它，舊單的異動才不會被跳過。
      const tables: any[][] = [findings, inspections, coordinations];
      const cut = tables.filter((rows) => rows.length >= LIMIT);
      const nextSince = [[fAt, fId], [iAt, iId], [cAt, cId]].map(([at, id], n) => {
        const last = tables[n].at(-1);
        return last ? `${last.cursor_at}|${last.id}` : `${at}|${id}`;
      }).join("~");

      // 作廢、修改會真的刪掉缺失，增量抓不到「不見了」：附上現存 id 讓地端對帳
      const live = cut.length ? [] : await db.sql`SELECT id FROM findings`;
      return json({
        since, next_since: nextSince, limit: LIMIT, truncated: cut.length > 0,
        sites, vendors, findings, inspections, coordinations,
        live_finding_ids: cut.length ? null : live.map((r: any) => r.id),
      });
    }

    // 出工資料庫的增量匯出，供地端 SQL Server 保留一份副本（collectors/sync_forms.py
    // 設 SYNC_WORKLOG=true 時，在表單同步的同一輪呼叫——資料庫本來就醒著）。
    // 游標是「更新時間|id」：每晚寫入整批共用同一個 NOW()，只用時間當游標的話，
    // 剛好切在分頁邊界的同時間資料會被跳過。
    //
    // 這組代理權杖也打包在每個工地檢視器裡，所以只給分析需要的欄位：不含
    // LINE 原文與回報人（LINE 顯示名稱）；那些只能登入後從出工資料庫頁下載。
    // 最後一頁附上雲端現存的全部 id，地端據此刪掉雲端已刪除的列（改判的舊列）。
    if (p === "/api/v1/export/worklog" && method === "GET") {
      const expected = Netlify.env.get("SITE_AGENT_TOKEN") || "";
      const token = req.headers.get("x-agent-token") || "";
      if (!expected || token !== expected) return fail(401, "代理權杖驗證失敗");

      const [tsRaw, idRaw] = (url.searchParams.get("since") || "").split("|");
      const sinceTs = tsRaw && !isNaN(Date.parse(tsRaw)) ? tsRaw : "1970-01-01T00:00:00";
      const sinceId = Number.parseInt(idRaw || "0", 10) || 0;
      const LIMIT = 1000;

      const reports = await db.sql`
        SELECT r.id, r.site_code, r.report_date::text AS report_date, r.building, r.vendor, r.headcount,
               r.trade_summary, r.supervisor, r.tasks,
               to_char(r.reported_at, 'YYYY-MM-DD"T"HH24:MI:SS') AS reported_at,
               r.message_id,
               to_char(r.updated_at, 'YYYY-MM-DD"T"HH24:MI:SS.US') AS updated_at,
               COALESCE((SELECT json_agg(json_build_object('trade', t.trade, 'headcount', t.headcount)
                                         ORDER BY t.trade)
                         FROM worklog_trades t WHERE t.report_id = r.id), '[]'::json) AS trades
        FROM worklog_reports r
        WHERE (r.updated_at, r.id) > (${sinceTs}::timestamp, ${sinceId})
        ORDER BY r.updated_at, r.id
        LIMIT ${LIMIT}`;
      // 歸類與解析失敗清單筆數很少，每次全量給，地端整批覆蓋即可
      const aliases = await db.sql`SELECT trade, trade_group FROM worklog_trade_aliases ORDER BY trade`;
      const rejects = await db.sql`
        SELECT message_id, site_code, to_char(reported_at, 'YYYY-MM-DD"T"HH24:MI:SS') AS reported_at
        FROM worklog_rejects ORDER BY message_id`;
      const truncated = reports.length >= LIMIT;
      const liveIds = truncated ? undefined
        : (await db.sql`SELECT id FROM worklog_reports ORDER BY id`).map((x: any) => x.id);
      const last: any = reports.at(-1);
      return json({
        next_since: last ? `${last.updated_at}|${last.id}` : `${sinceTs}|${sinceId}`,
        truncated,
        reports, aliases, rejects,
        ...(liveIds ? { live_ids: liveIds } : {}),
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

    // ---- 出工資料庫：匯出（CSV）與工種歸類維護 ----
    // 資料含廠商、作業主管與回報人姓名，限管理員、主管、職安人員。
    // 查詢會喚醒資料庫（計費），這是分析時手動操作，頻率低。
    if (p === "/api/worklog/export" && method === "GET") {
      if (!["admin", "manager", "safety"].includes(me.role)) return fail(403, "無權匯出出工資料");
      const kind = url.searchParams.get("kind") || "reports";
      const from = url.searchParams.get("from") || "2000-01-01";
      const to = url.searchParams.get("to") || "2999-12-31";
      if (!isIsoDate(from) || !isIsoDate(to)) return fail(400, "日期格式應為 YYYY-MM-DD");
      const site = url.searchParams.get("site") || "";    // 空白＝全部工地
      if (site && !SITE_CODE_RE.test(site)) return fail(400, "工地代碼格式錯誤");
      const tag = `${site ? site + "_" : ""}${from === "2000-01-01" ? "all" : from}_${to === "2999-12-31" ? "now" : to}`;

      if (kind === "reports") {
        const withRaw = url.searchParams.get("raw") === "1";
        const rows = await db.sql`
          SELECT r.site_code, COALESCE(s.name, r.site_code) AS site_name,
                 r.report_date::text AS d, r.building, r.vendor, r.headcount, r.trade_summary,
                 r.supervisor, r.tasks, r.reporter,
                 to_char(r.reported_at, 'YYYY-MM-DD HH24:MI') AS at, r.message_id, r.raw
          FROM worklog_reports r LEFT JOIN sites s ON s.code = r.site_code
          WHERE r.report_date BETWEEN ${from}::date AND ${to}::date
            AND (${site}::text = '' OR r.site_code = ${site}::text)
          ORDER BY r.site_code, r.report_date, r.building, r.vendor`;
        const header = ["工地代碼", "工地", "日期", "棟別", "廠商", "工項", "總人數", "工種摘要", "作業主管", "施作項目",
                        "回報人", "回報時間", "LINE訊息ID", "廠商原文", ...(withRaw ? ["原文"] : [])];
        return csvResponse(`出工回報_${tag}.csv`, `worklog_reports_${tag}.csv`, header, rows.map((r: any) => {
          const c = reportVendor(r.vendor, r.trade_summary);
          return [r.site_code, r.site_name, r.d, r.building, c.vendor ?? r.vendor, c.work, r.headcount, c.trade,
                  r.supervisor, r.tasks, r.reporter, r.at, r.message_id, r.vendor, ...(withRaw ? [r.raw] : [])];
        }));
      }
      if (kind === "trades") {
        const rows = await db.sql`
          SELECT r.site_code, COALESCE(s.name, r.site_code) AS site_name,
                 r.report_date::text AS d, r.building, r.vendor, t.trade,
                 COALESCE(a.trade_group, t.trade) AS trade_group, t.headcount
          FROM worklog_trades t
          JOIN worklog_reports r ON r.id = t.report_id
          LEFT JOIN sites s ON s.code = r.site_code
          LEFT JOIN worklog_trade_aliases a ON a.trade = t.trade
          WHERE r.report_date BETWEEN ${from}::date AND ${to}::date
            AND (${site}::text = '' OR r.site_code = ${site}::text)
          ORDER BY r.site_code, r.report_date, r.building, r.vendor, t.trade`;
        return csvResponse(`工種明細_${tag}.csv`, `worklog_trades_${tag}.csv`,
          ["工地代碼", "工地", "日期", "棟別", "廠商", "工項", "工種（原寫法）", "工種歸類", "人數", "廠商原文"],
          rows.map((r: any) => {
            const c = cleanVendor(r.vendor);
            return [r.site_code, r.site_name, r.d, r.building, c.vendor ?? r.vendor, c.work,
                    r.trade, r.trade_group, r.headcount, r.vendor];
          }));
      }
      if (kind === "rejects") {
        const rows = await db.sql`
          SELECT x.site_code, COALESCE(s.name, x.site_code) AS site_name, x.message_id,
                 to_char(x.reported_at, 'YYYY-MM-DD HH24:MI') AS at, x.reporter, x.raw
          FROM worklog_rejects x LEFT JOIN sites s ON s.code = x.site_code
          WHERE (${site}::text = '' OR x.site_code = ${site}::text)
          ORDER BY x.site_code, x.reported_at`;
        return csvResponse("出工回報_解析失敗.csv", "worklog_rejects.csv",
          ["工地代碼", "工地", "LINE訊息ID", "回報時間", "回報人", "原文"],
          rows.map((r: any) => [r.site_code, r.site_name, r.message_id, r.at, r.reporter, r.raw]));
      }
      return fail(400, "kind 應為 reports、trades 或 rejects");
    }

    if (p === "/api/worklog/trades" && method === "GET") {
      if (!["admin", "manager", "safety"].includes(me.role)) return fail(403, "無權檢視出工資料");
      const site = url.searchParams.get("site") || "";
      if (site && !SITE_CODE_RE.test(site)) return fail(400, "工地代碼格式錯誤");
      return json(await db.sql`
        SELECT t.trade, a.trade_group, COUNT(*)::int AS reports, SUM(t.headcount)::int AS mandays,
               MIN(r.report_date)::text AS first_date, MAX(r.report_date)::text AS last_date
        FROM worklog_trades t
        JOIN worklog_reports r ON r.id = t.report_id
        LEFT JOIN worklog_trade_aliases a ON a.trade = t.trade
        WHERE (${site}::text = '' OR r.site_code = ${site}::text)
        GROUP BY t.trade, a.trade_group
        ORDER BY mandays DESC`, { headers: { "cache-control": "no-store" } });
    }

    if (p === "/api/worklog/summary" && method === "GET") {
      if (!["admin", "manager", "safety"].includes(me.role)) return fail(403, "無權檢視出工資料");
      const site = url.searchParams.get("site") || "";
      if (site && !SITE_CODE_RE.test(site)) return fail(400, "工地代碼格式錯誤");
      const [r] = await db.sql`
        SELECT COUNT(*)::int AS reports, COALESCE(SUM(headcount), 0)::int AS mandays,
               MIN(report_date)::text AS first_date, MAX(report_date)::text AS last_date,
               -- updated_at 是 NOW() 以連線時區（雲端為 UTC）存的無時區時間，先還原再轉台北
               to_char((MAX(updated_at) AT TIME ZONE current_setting('TimeZone'))
                       AT TIME ZONE 'Asia/Taipei', 'YYYY-MM-DD HH24:MI') AS updated_at,
               (SELECT COUNT(*)::int FROM worklog_rejects x
                WHERE ${site}::text = '' OR x.site_code = ${site}::text) AS rejects
        FROM worklog_reports
        WHERE ${site}::text = '' OR site_code = ${site}::text`;
      // 各工地一列，供頁面的工地選單與概況表
      const sites = await db.sql`
        SELECT r.site_code AS code, COALESCE(s.name, r.site_code) AS name,
               COUNT(*)::int AS reports, COALESCE(SUM(r.headcount), 0)::int AS mandays,
               MIN(r.report_date)::text AS first_date, MAX(r.report_date)::text AS last_date
        FROM worklog_reports r LEFT JOIN sites s ON s.code = r.site_code
        GROUP BY r.site_code, s.name ORDER BY r.site_code`;
      return json({ ...r, sites }, { headers: { "cache-control": "no-store" } });
    }

    // 工種歸類：[{trade, trade_group}]；trade_group 空白＝移除歸類（回到原寫法）
    if (p === "/api/worklog/trade-aliases" && method === "POST") {
      if (!["admin", "manager"].includes(me.role)) return fail(403, "僅管理員或主管可維護工種歸類");
      let items: any[];
      try {
        const b = await req.json();
        items = Array.isArray(b?.aliases) ? b.aliases : [];
        if (items.length > 500) throw 0;
      } catch { return fail(400, "格式錯誤"); }
      const clean = items
        .map((x: any) => ({ trade: String(x?.trade || "").trim().slice(0, 32),
                            trade_group: String(x?.trade_group || "").trim().slice(0, 32) }))
        .filter((x) => x.trade);
      const set = clean.filter((x) => x.trade_group);
      const del = clean.filter((x) => !x.trade_group).map((x) => x.trade);
      if (set.length) {
        await db.sql`
          INSERT INTO worklog_trade_aliases (trade, trade_group)
          SELECT trade, trade_group FROM json_to_recordset(${JSON.stringify(set)}::json)
            AS x(trade text, trade_group text)
          ON CONFLICT (trade) DO UPDATE SET trade_group = EXCLUDED.trade_group, updated_at = NOW()`;
      }
      if (del.length) {
        await db.sql`
          DELETE FROM worklog_trade_aliases
          WHERE trade IN (SELECT value FROM json_array_elements_text(${JSON.stringify(del)}::json))`;
      }
      return json({ ok: true, saved: set.length, removed: del.length });
    }

    if (p === '/api/board-sites' && method === 'GET') {
      // 工地清單一個月改不了幾次，短快取就能讓重複開頁不喚醒資料庫
      // （DB compute 按醒著的時數計費）
      return json(await db.sql`SELECT id, code, name FROM sites WHERE active = true ORDER BY sort_order, id`,
        {headers: {'cache-control': 'private, max-age=300'}});
    }
    const boardMatch = /^\/api\/site-board\/(\d+)$/.exec(p);
    if (boardMatch) {
      const id = Number(boardMatch[1]);
      const [site] = await db.sql`SELECT id, code, name, active, board_config, board_revision FROM sites WHERE id = ${id}`;
      if (!site || !site.active) return fail(404, '工地不存在或已停用');
      if (method === 'GET') return json(boardPayload(site), {headers: {'cache-control': 'no-store'}});
      if (method !== 'POST') return fail(405, '不支援此操作');
      const [user] = await db.sql`SELECT role, site_id, active FROM users WHERE id = ${me.id}`;
      if (!user?.active || !(user.role === 'admin' || (['safety', 'manager'].includes(user.role) && Number(user.site_id) === id))) {
        return fail(403, '僅管理員或所屬工地的職安人員、主管可維護');
      }
      let b: any, config: any;
      try {
        b = await req.json();
        if (!Number.isSafeInteger(b.revision) || b.revision < 0) throw Error('設定版本格式錯誤');
        config = validateBoard(b.config);
      } catch (e: any) { return fail(400, e.message || '設定格式錯誤'); }
      // 台北時間、無時區註記——與地端寫入的格式一致。前端直接截前 16 字
      // 顯示，寫 UTC 的話牆上的「更新於」會永遠慢 8 小時
      config.updated_at = minuteISO(new Date().toISOString());
      const [saved] = await db.sql`UPDATE sites SET board_config = ${JSON.stringify(config)}, board_revision = ${b.revision + 1}
        WHERE id = ${id} AND COALESCE(board_revision, 0) = ${b.revision}
        RETURNING id, code, name, board_config, board_revision`;
      if (!saved) return fail(409, '其他人已更新，請重新載入再編輯');
      return json(boardPayload(saved), {headers: {'cache-control': 'no-store'}});
    }

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

    // 填報預帶：這個工地近 30 天 LINE 出工回報裡各廠商最近一次的工種與作業主管。
    // 選了廠商就帶出工種／責任人，現場不必再打一次（仍可修改）。出工資料每晚
    // 才寫入，最新只到昨天——預帶用途夠了。
    if (p === "/api/vendors/hints" && method === "GET") {
      const siteId = parseInt(url.searchParams.get("site_id") || "0", 10);
      const rows = await db.sql`
        SELECT DISTINCT ON (w.vendor) w.vendor, w.trade_summary, w.report_date,
               (SELECT x.supervisor FROM worklog_reports x
                WHERE x.site_code = w.site_code AND x.vendor = w.vendor AND x.supervisor <> ''
                  AND x.report_date >= CURRENT_DATE - 30
                ORDER BY x.report_date DESC, x.reported_at DESC NULLS LAST LIMIT 1) AS supervisor
        FROM worklog_reports w JOIN sites s ON s.code = w.site_code
        WHERE s.id = ${siteId} AND w.report_date >= CURRENT_DATE - 30
        ORDER BY w.vendor, w.report_date DESC, w.reported_at DESC NULLS LAST`;
      return json(rows.map((r: any) => ({
        vendor: r.vendor,
        // 「電班8、水班1」→「電班、水班」：預帶的是職種，人數由現場當天另計
        trade: splitTrades(r.trade_summary).map((t) => t.name).join("、") || null,
        // 最近一次有寫的主管：當天沒寫不代表換人。「王大明/李小華」不預帶
        supervisor: cleanPerson(r.supervisor),
        report_date: dateOnly(r.report_date),
      })));
    }

    // 某日出工回報：協議巡視表的「參加協議人員」直接帶入，現場不必再打一次。
    // 當天的在工地看板的外部快取（GitHub 定時解析 LINE 後推上來，主場站之外在 worklog_sites）；
    // worklog_reports 每晚才寫，白天沒有今天的，但事後補填過去的單要靠它。
    if (p === "/api/worklog/day" && method === "GET") {
      const code = await siteCodeOf(url.searchParams.get("site_id"));
      const today = todayISO();
      const raw = url.searchParams.get("date") || today;
      const date = isIsoDate(raw) ? raw : today;
      if (!code || date > today) return json({ date, updated_at: null, rows: [] });
      // 回報的廠商欄常帶人數或工項；看不出是廠商的留原文，由現場判斷。
      // 明確是工項的（鋼承板）不當廠商：供應商留空讓現場選，否則會多出一個假的簽名格
      const shape = (r: any, summary: string | null) => {
        const { vendor, work: said, trade } = reportVendor(r.vendor, summary);
        const work = said === UNNAMED_VENDOR ? null : said;   // 沒寫廠商不是一種作業項目
        return {
          building: r.building ?? null, vendor: vendor ?? (said ? null : r.vendor ?? null), work,
          trade: trade ?? work ?? null, headcount: r.headcount ?? null,
          supervisor: cleanPerson(r.supervisor) ?? r.supervisor ?? null, tasks: cleanTaskText(r.tasks) || null,
        };
      };
      if (date === today) {
        const ext = (await readJsonBlob(EXTERNAL_KEY)) || {};
        const primary = code === primaryCode();
        const wl = primary ? ext.worklog : ext.worklog_sites?.[code];
        if (wl && Array.isArray(wl.rows) && (!wl.date || wl.date === today)) {
          return json({ date, updated_at: (primary ? ext.section_at?.worklog : wl.at) ?? null,
                        rows: wl.rows.map((r: any) => shape(r, r.trade ?? null)),
                        // 今天才收到、日期卻是之前的：多半沿用範本忘了改日期，提醒現場
                        late: (wl.late || []).map((x: any) => ({
                          vendor: cleanVendor(x.vendor).vendor ?? x.vendor, report_date: x.report_date })) });
        }
      }
      // 0 人的是「施工回報」改版後消失的廠商，當天不在場
      const rows = await db.sql`
        SELECT building, vendor, trade_summary, headcount, supervisor, tasks FROM worklog_reports
        WHERE site_code = ${code} AND report_date = ${date}::date
          AND (headcount IS NULL OR headcount > 0)
        ORDER BY vendor`;
      return json({ date, updated_at: null, rows: rows.map((r: any) => shape(r, r.trade_summary ?? null)) });
    }

    // 填報預設清單：協議巡視表、自主檢查表的可搜尋下拉。各工地各自一份，
    // 內容來自歷史出工回報（每晚重算）與現場新增、表單送出累積。
    if (p === "/api/form-options" && method === "GET") {
      const code = await siteCodeOf(url.searchParams.get("site_id"));
      if (!code) return fail(404, "查無此工地");
      return json(await optionLists(code));
    }

    // 下列三支都回完整清單，前端直接覆蓋本地，不自己推算。
    // 權限比照點工系統：工地使用者就能維護，等管理員建檔現場只會亂打或卡住。
    // 「＋ 新增」：逐筆合併（upsert），兩人同時新增不會互蓋；曾被刪除的由這裡恢復。
    if (p === "/api/form-options" && method === "POST") {
      const b = await req.json();
      const code = await siteCodeOf(b.site_id);
      if (!code) return fail(404, "查無此工地");
      const value = String(b.value ?? "").trim();
      const bad = optionValueError(b.field, value);
      if (bad) return fail(400, bad);
      const vendor = b.field === "vendor" ? "" : String(b.vendor ?? "").trim().slice(0, 64);
      // 只差大小寫視為同值：abc 已存在時輸入 ABC 不另存一筆
      const same = (await db.sql`
        SELECT value FROM form_options
        WHERE site_code = ${code} AND field = ${b.field} AND LOWER(value) = LOWER(${value})
        GROUP BY value HAVING bool_and(active) LIMIT 1`)[0];
      const stored = same?.value ?? value;
      await db.sql`
        INSERT INTO form_options (site_code, field, value, vendor, source, n, last_used)
        VALUES (${code}, ${b.field}, ${stored}, ${vendor}, 'manual', 1, CURRENT_DATE)
        ON CONFLICT (site_code, field, value, vendor, source) DO UPDATE SET
          n = form_options.n + 1, last_used = CURRENT_DATE, active = TRUE, updated_at = NOW()`;
      await db.sql`
        UPDATE form_options SET active = TRUE, canonical = NULL, updated_at = NOW()
        WHERE site_code = ${code} AND field = ${b.field} AND value = ${stored} AND NOT active`;
      return json({ ok: true, value: stored, options: await optionLists(code) });
    }

    // 改正錯字。單一語句＝同一交易：舊值停用並記下新值、先前改到舊值的跟著指向新值、
    // 次數併到新值（新值已存在就是合併）。已送出的單據不動：送出即已簽核存查。
    if (p === "/api/form-options/rename" && method === "POST") {
      const b = await req.json();
      const code = await siteCodeOf(b.site_id);
      if (!code) return fail(404, "查無此工地");
      const from = String(b.from ?? "");
      const to = String(b.to ?? "").trim();
      const bad = optionValueError(b.field, to);
      if (bad) return fail(400, bad);
      if (from === to) return json({ ok: true, options: await optionLists(code) });
      const moved = await db.sql`
        WITH moved AS (
          UPDATE form_options SET active = FALSE, canonical = ${to}, updated_at = NOW()
          WHERE site_code = ${code} AND field = ${b.field} AND value = ${from} AND active
          RETURNING vendor, n, last_used
        ), chain AS (
          UPDATE form_options SET canonical = ${to}, updated_at = NOW()
          WHERE site_code = ${code} AND field = ${b.field} AND canonical = ${from} AND value <> ${to}
        ), merged AS (
          SELECT vendor, SUM(n)::int AS n, MAX(last_used) AS last_used FROM moved GROUP BY vendor
        )
        INSERT INTO form_options (site_code, field, value, vendor, source, n, last_used)
        SELECT ${code}::text, ${b.field}::text, ${to}::text, vendor, 'form', n, last_used FROM merged
        ON CONFLICT (site_code, field, value, vendor, source) DO UPDATE SET
          n = form_options.n + EXCLUDED.n, updated_at = NOW()
        RETURNING id`;
      if (!moved.length) return fail(404, "這個選項可能已被改正或刪除，請重新整理");
      await db.sql`
        UPDATE form_options SET active = TRUE, canonical = NULL, updated_at = NOW()
        WHERE site_code = ${code} AND field = ${b.field} AND value = ${to}`;
      if (b.field === "vendor") {        // 人員、職種記的「這家廠商用過」跟著換名
        await db.sql`
          UPDATE form_options o SET vendor = ${to}, updated_at = NOW()
          WHERE o.site_code = ${code} AND o.vendor = ${from}
            AND NOT EXISTS (SELECT 1 FROM form_options x
                            WHERE x.site_code = o.site_code AND x.field = o.field AND x.value = o.value
                              AND x.vendor = ${to} AND x.source = o.source)`;
      }
      return json({ ok: true, options: await optionLists(code) });
    }

    // 刪除：只標記不真的刪，每晚依出工回報重算時才不會長回來；不動任何單據。
    // 冪等：已經不在清單裡也算成功。
    if (p === "/api/form-options/hide" && method === "POST") {
      const b = await req.json();
      const code = await siteCodeOf(b.site_id);
      if (!code) return fail(404, "查無此工地");
      await db.sql`
        UPDATE form_options SET active = FALSE, updated_at = NOW()
        WHERE site_code = ${code} AND field = ${String(b.field)} AND value = ${String(b.value)}`;
      return json({ ok: true, options: await optionLists(code) });
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
          AND i.status <> 'void'
          AND (${siteId}::int IS NULL OR i.site_id = ${siteId}::int)
        ORDER BY i.inspect_date DESC, i.id DESC
        LIMIT 300`;
      return json(rows.map((r: any) => ({
        ...r, inspect_date: dateOnly(r.inspect_date),
        pdf_url: r.pdf_key ? `/api/inspections/${r.id}/pdf` : null,
      })));
    }

    // 已填單據查詢：自主檢查表＋協議巡視表合併成一張清單，依日期區間、單別、表單、
    // 棟別篩選；關鍵字比對填表人、地點、表單名、廠商、參加人員、缺失內容、協議事項。
    // 現場常是「上週那張 B2 施工架的表」「某廠商被開過什麼」這種找法。
    if (p === "/api/records" && method === "GET") {
      const sp = url.searchParams;
      const iso = (v: string | null) => (isIsoDate(v) ? v : null);
      const to = iso(sp.get("to")) || todayISO();
      const from = iso(sp.get("from")) || dateOnly(new Date(Date.parse(to) - 30 * 86400000));
      const kind = ["inspection", "coordination"].includes(sp.get("kind") || "") ? sp.get("kind")! : "";
      const formCode = (sp.get("form_code") || "").trim();
      const building = (sp.get("building") || "").trim();
      const siteId = parseInt(sp.get("site_id") || "", 10) || null;
      // 沒指定狀態時不列作廢的單，要看才選
      const status = ["signing", "submitted", "void"].includes(sp.get("status") || "") ? sp.get("status")! : "";
      const q = (sp.get("q") || "").trim().slice(0, 50);
      // LIKE 的 % _ \ 要跳脫，否則輸入「100%」會變成萬用字元
      const pat = q ? `%${q.replace(/[\\%_]/g, (m) => "\\" + m)}%` : "";
      const rows = await db.sql`
        SELECT * FROM (
          SELECT 'inspection' AS kind, i.id, i.inspect_date AS d, i.building, s.name AS site,
                 ft.title, i.form_code, COALESCE(i.inspector_name, u.display_name) AS person,
                 i.location, i.pdf_key, i.status, i.revision, i.inspector_id AS owner,
                 (SELECT COUNT(*)::int FROM inspection_results r
                   WHERE r.inspection_id = i.id AND r.result = 'fail') AS fail_count,
                 (SELECT COUNT(*)::int FROM findings f WHERE f.inspection_id = i.id) AS finding_count,
                 NULL::int AS attendee_count
          FROM inspections i
          JOIN sites s ON s.id = i.site_id
          JOIN form_templates ft ON ft.form_code = i.form_code
          JOIN users u ON u.id = i.inspector_id
          WHERE ${kind}::text IN ('', 'inspection')
            AND (i.status = ${status}::text OR (${status}::text = '' AND i.status <> 'void'))
            AND i.inspect_date BETWEEN ${from}::date AND ${to}::date
            AND (${siteId}::int IS NULL OR i.site_id = ${siteId}::int)
            AND (${building}::text = '' OR i.building = ${building}::text)
            AND (${formCode}::text = '' OR i.form_code = ${formCode}::text)
            AND (${pat}::text = ''
              OR i.inspector_name ILIKE ${pat}::text OR i.location ILIKE ${pat}::text
              OR i.vendor_name ILIKE ${pat}::text OR ft.title ILIKE ${pat}::text
              OR EXISTS (SELECT 1 FROM findings f LEFT JOIN vendors v ON v.id = f.vendor_id
                         WHERE f.inspection_id = i.id
                           AND (f.description ILIKE ${pat}::text OR v.name ILIKE ${pat}::text
                                OR f.responsible_person ILIKE ${pat}::text)))
          UNION ALL
          SELECT 'coordination', c.id, c.work_date, c.building, s.name,
                 '每日協議巡視處理紀錄表', NULL, c.recorder_name, NULL, c.pdf_key, c.status,
                 c.revision, c.created_by, NULL,
                 (SELECT COUNT(*)::int FROM findings f WHERE f.coordination_id = c.id),
                 (SELECT COUNT(*)::int FROM coordination_attendees a WHERE a.coordination_id = c.id)
          FROM coordinations c
          JOIN sites s ON s.id = c.site_id
          WHERE ${kind}::text IN ('', 'coordination')
            AND (c.status = ${status}::text OR (${status}::text = '' AND c.status <> 'void'))
            AND ${formCode}::text = ''
            AND c.work_date BETWEEN ${from}::date AND ${to}::date
            AND (${siteId}::int IS NULL OR c.site_id = ${siteId}::int)
            AND (${building}::text = '' OR c.building = ${building}::text)
            AND (${pat}::text = ''
              OR c.recorder_name ILIKE ${pat}::text OR c.agreement_text ILIKE ${pat}::text
              OR c.handling_text ILIKE ${pat}::text
              OR EXISTS (SELECT 1 FROM coordination_attendees a LEFT JOIN vendors v ON v.id = a.vendor_id
                         WHERE a.coordination_id = c.id
                           AND (a.vendor_name ILIKE ${pat}::text OR v.name ILIKE ${pat}::text
                                OR a.person_name ILIKE ${pat}::text OR a.work_item ILIKE ${pat}::text
                                OR a.work_content ILIKE ${pat}::text))
              OR EXISTS (SELECT 1 FROM findings f LEFT JOIN vendors v ON v.id = f.vendor_id
                         WHERE f.coordination_id = c.id
                           AND (f.description ILIKE ${pat}::text OR v.name ILIKE ${pat}::text
                                OR f.responsible_person ILIKE ${pat}::text)))
        ) x
        ORDER BY d DESC, id DESC
        LIMIT 201`;
      return json({
        from, to, truncated: rows.length > 200,
        rows: rows.slice(0, 200).map((r: any) => ({
          kind: r.kind, id: r.id, date: dateOnly(r.d), building: r.building, site: r.site,
          title: r.title, form_code: r.form_code, person: r.person, location: r.location,
          status: r.status, revision: r.revision, can_alter: canAlter(me, r.owner),
          no: `${r.kind === "inspection" ? "INSP" : "COORD"}-${String(r.id).padStart(6, "0")}`,
          fail_count: r.fail_count, finding_count: r.finding_count, attendee_count: r.attendee_count,
          pdf_url: r.pdf_key ? `/api/${r.kind === "inspection" ? "inspections" : "coordinations"}/${r.id}/pdf` : null,
        })),
      });
    }

    // 多人分次簽核
    if (p === "/api/sign/pending" && method === "GET") return await pendingSigns();
    const signMatch = /^\/api\/sign\/(inspection|coordination)\/(\d+)(?:\/(sign|undo|finish|void))?$/.exec(p);
    if (signMatch) {
      return await handleSign(req, me, signMatch[1], parseInt(signMatch[2], 10), signMatch[3] || "", method);
    }

    // 修改已填單據
    const editMatch = /^\/api\/edit\/(inspection|coordination)\/(\d+)$/.exec(p);
    if (editMatch) return await handleEdit(req, me, editMatch[1], parseInt(editMatch[2], 10), method);
    const revPdf = /^\/api\/revisions\/(\d+)\/pdf$/.exec(p);
    if (revPdf) {
      const key = (await db.sql`SELECT pdf_key FROM form_revisions WHERE id = ${parseInt(revPdf[1], 10)}`)[0]?.pdf_key;
      return key ? await servePdfKey(key) : fail(404, "這一版沒有 PDF");
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
        WHERE c.work_date >= CURRENT_DATE - ${days}::int AND c.status <> 'void'
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
      const { findings_today, open, overdue } = await findingStats();
      return json({ findings_today, open, overdue });
    }

    if ((p === "/api/findings" || p === "/api/findings/report") && method === "GET") {
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
      if (p === "/api/findings") return json(out);
      // 條件和畫面上的清單相同：看到什麼就匯出什麼
      const site = siteId ? rows.find((r: any) => String(r.site_id) === siteId)?.site : "";
      const filter = [site || "全部工地", `近 ${days} 日`, overdueOnly ? "只看逾期" : ""].filter(Boolean).join("　");
      const bytes = await buildFindingReportPdf(filter, out, await findingPhotos(out));
      return pdfResponse(bytes, "findings-report.pdf");
    }

    if (p === "/api/findings" && method === "POST") {
      const b = await req.json();
      const bad = buildingTooLong(b.building);
      if (bad) return bad;
      const id = await insertFinding({ ...b, source: b.source || "audit" }, me);
      return json({ ok: true, finding_id: id });
    }

    // 改善回報：責任廠商或工程師補上改善後照片／說明 → 待複驗。
    // 照片上傳不等於改善通過，結案一定要再經安衛人員覆驗（下方 verify）。
    const fixMatch = /^\/api\/findings\/(\d+)\/fix$/.exec(p);
    if (fixMatch && method === "POST") {
      const b = await req.json();
      const note = String(b.fix_note ?? "").trim();
      if (!note && !b.photo_after) return fail(400, "請附改善後照片或改善說明");
      const rows = await db.sql`
        UPDATE findings SET fixed_at = NOW(), fix_note = ${note || null},
          photo_after = COALESCE(${b.photo_after ?? null}, photo_after), status = 'fixed'
        WHERE id = ${parseInt(fixMatch[1], 10)} AND status IN ('open', 'fixed')
        RETURNING id`;
      if (!rows.length) return fail(409, "這筆缺失已結案，不能再回報改善");
      return json({ ok: true });
    }

    // 覆驗通過才結案；只能對「待複驗」的缺失按——還在改善中的不能跳過改善回報
    const verifyMatch = /^\/api\/findings\/(\d+)\/verify$/.exec(p);
    if (verifyMatch && method === "POST") {
      if (!["safety", "manager", "admin"].includes(me.role)) {
        return fail(403, "僅職安人員或主管可複驗");
      }
      const rows = await db.sql`
        UPDATE findings SET verifier_id = ${me.id}, verified_at = NOW(), status = 'closed'
        WHERE id = ${parseInt(verifyMatch[1], 10)} AND status = 'fixed'
        RETURNING id`;
      if (!rows.length) return fail(409, "只能複驗「待複驗」的缺失，請先回報改善");
      return json({ ok: true });
    }

    // 覆驗不通過：退回改善中。原因、日期與前次改善照片累加記在 review_note，
    // 前次的改善照片與說明都不刪（稽核要看得到改善過幾次、為何退回）
    const rejectMatch = /^\/api\/findings\/(\d+)\/reject$/.exec(p);
    if (rejectMatch && method === "POST") {
      if (!["safety", "manager", "admin"].includes(me.role)) {
        return fail(403, "僅職安人員或主管可複驗");
      }
      const b = await req.json();
      const reason = String(b.reason ?? "").trim();
      if (!reason) return fail(400, "請填寫覆驗不通過的原因");
      if (reason.length > 200) return fail(400, "原因請在 200 字以內");
      const rows = await db.sql`
        UPDATE findings SET status = 'open', fixed_at = NULL, fix_note = NULL, photo_after = NULL,
          review_note = CONCAT_WS(E'\n', review_note,
            TO_CHAR(NOW() AT TIME ZONE 'Asia/Taipei', 'MM/DD HH24:MI') || ' 覆驗未通過（'
              || ${me.name} || '）：' || ${reason}
              || COALESCE('；前次改善說明：' || fix_note, '')
              || COALESCE('；前次改善照片：' || REPLACE(photo_after, E'\n', '、'), ''))
        WHERE id = ${parseInt(rejectMatch[1], 10)} AND status = 'fixed'
        RETURNING id`;
      if (!rows.length) return fail(409, "只能退回「待複驗」的缺失");
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
}

// ---------------------------------------------------------------------------
// 缺失
// ---------------------------------------------------------------------------

/** 工地 id → 工地代碼（出工資料與預設清單用代碼分工地）。 */
async function siteCodeOf(id: unknown): Promise<string | null> {
  const n = parseInt(String(id ?? ""), 10);
  if (!n) return null;
  return (await db.sql`SELECT code FROM sites WHERE id = ${n}`)[0]?.code ?? null;
}

const attendeeRows = (list: any[] = []) => list.map((a: any) => ({
  vendor: a.vendor_name, supervisor: a.person_name, trade_summary: a.trade,
  work_item: a.work_item, tasks: a.work_content,
}));

/** 現場實際填的內容累積進預設清單。學不成不該讓已存好的單據送出失敗。 */
async function learnFromForm(siteId: unknown, rows: any[], findings: any[] = []) {
  try {
    const code = await siteCodeOf(siteId);
    if (!code) return;
    const ids = [...new Set(findings.map((f: any) => Number(f.vendor_id)).filter(Boolean))];
    const names = new Map((ids.length ? await db.sql`
      SELECT id, name FROM vendors WHERE id = ANY(${ids}::int[])` : []).map((v: any) => [v.id, v.name]));
    await learnFormOptions(sqlTag, code, [...rows, ...findings.map((f: any) => ({
      vendor: names.get(Number(f.vendor_id)) ?? null, supervisor: f.responsible_person,
    }))]);
  } catch (e: any) { console.warn(`[form-options] 累積失敗：${e?.message || e}`); }
}

/** 某工地的完整清單＋改正對照（舊值→新值，今日名單帶入時用）。 */
async function optionLists(code: string) {
  const out: Record<string, any> = Object.fromEntries(OPTION_FIELDS.map((f) => [f, []]));
  const rows = await db.sql`
    SELECT field, value, SUM(n)::int AS n,
           COALESCE(json_agg(DISTINCT vendor) FILTER (WHERE vendor <> ''), '[]') AS vendors
    FROM form_options
    WHERE site_code = ${code}
    GROUP BY field, value
    HAVING bool_and(active)
    ORDER BY SUM(n) DESC, MAX(last_used) DESC NULLS LAST, value`;
  for (const r of rows as any[]) out[r.field]?.push({ value: r.value, n: r.n, vendors: r.vendors });
  const renames: Record<string, Record<string, string>> = {};
  const moved = await db.sql`
    SELECT DISTINCT field, value, canonical FROM form_options
    WHERE site_code = ${code} AND canonical IS NOT NULL`;
  for (const r of moved as any[]) (renames[r.field] ||= {})[r.value] = r.canonical;
  out.renames = renames;
  // 每家廠商最常出現的人員：LINE 當天沒寫主管時預帶。清單的 n 是跨廠商合計，不能拿來比
  const people = new Set(out.person.map((o: any) => o.value));
  const usual: Record<string, string> = {};
  const byVendor = await db.sql`
    SELECT vendor, value, SUM(n)::int AS n FROM form_options
    WHERE site_code = ${code} AND field = 'person' AND vendor <> '' AND active
    GROUP BY vendor, value ORDER BY vendor, n DESC, value`;
  for (const r of byVendor as any[]) if (people.has(r.value)) usual[r.vendor] ||= r.value;
  out.vendor_person = usual;
  return out;
}

/** 名單值的驗證，新增與改正共用。回錯誤訊息，沒問題回 null。 */
function optionValueError(field: unknown, value: string): string | null {
  if (!OPTION_FIELDS.includes(String(field))) return "欄位錯誤";
  if (!value || value.length > 64) return "內容請在 1～64 字以內";
  if (field === "person" && PERSON_SEPARATOR.test(value)) return "一格只能填一個人名，請分開新增";
  return null;
}

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
    dispositions: parseDispositions(f.dispositions, f.action_type),
    penalty: f.penalty == null ? null : Number(f.penalty),
    due_date: due, status: f.status, overdue: Boolean(overdue),
    fix_note: f.fix_note ?? null, fixed_at: f.fixed_at ? minuteISO(f.fixed_at) : null,
    verified_at: f.verified_at ? minuteISO(f.verified_at) : null,
    review_note: f.review_note ?? null,
    photo_before: f.photo_before, photo_after: f.photo_after,
    doc_url: f.inspection_id ? `/static/sign.html?kind=inspection&id=${f.inspection_id}`
      : f.coordination_id ? `/static/sign.html?kind=coordination&id=${f.coordination_id}` : null,
  };
}

/** 缺失處置（可複選）：onsite=立即改善 scheduled=限期改善 proxy=代工 deduct=扣款 */
const DISPOSITIONS = ["onsite", "scheduled", "proxy", "deduct"];

/** 處置清單：陣列或逗號字串皆可；舊資料或舊版表單沒有時退回 action_type。 */
function parseDispositions(v: unknown, actionType?: string): string[] {
  const raw = Array.isArray(v) ? v : String(v ?? "").split(",");
  const out = DISPOSITIONS.filter((d) => raw.map((x) => String(x).trim()).includes(d));
  return out.length ? out : [actionType === "scheduled" ? "scheduled" : "onsite"];
}

/** 處置清單 → 寫入欄位。限期改善或代工＝之後才會改善完成：開成「改善中」、帶期限，
    逾期判斷照舊讀 action_type。只有立即改善（可另加扣款）才算改善完成（待複驗）；
    只勾扣款還沒改善，一樣是改善中。 */
function dispositionOf(b: any) {
  const disp = parseDispositions(b.dispositions, b.action_type);
  const onsite = disp.includes("onsite") && !disp.includes("scheduled") && !disp.includes("proxy");
  const actionType = onsite ? "onsite" : "scheduled";
  const penalty = disp.includes("deduct") && Number(b.penalty) > 0 ? Number(b.penalty) : null;
  return { disp, onsite, actionType, penalty };
}

const ONSITE_NOTE = "當場改善完成";

async function insertFinding(b: any, me: SessionUser, ids: {
  siteId?: number; inspectionId?: number; coordinationId?: number;
} = {}): Promise<number> {
  const { disp, onsite, actionType, penalty } = dispositionOf(b);
  // found_at 可指定，用於補登既有紙本紀錄。未指定時為現在時間。
  // 沒有這個欄位的話，補登的歷史單據會全部堆在匯入當天，趨勢圖失真。
  const foundAt = b.found_at || new Date().toISOString();
  const [row] = await db.sql`
    INSERT INTO findings
      (site_id, building, inspection_id, coordination_id, item_id, source, found_at, location,
       hazard_code, hazard_label, description, vendor_id, responsible_person,
       severity, action_type, dispositions, penalty, due_date, fixed_at, fix_note, status,
       photo_before, photo_after, created_by)
    VALUES (
      ${ids.siteId ?? b.site_id}, ${String(b.building ?? "").trim() || null},
      ${ids.inspectionId ?? null}, ${ids.coordinationId ?? null},
      ${b.item_id ?? null}, ${b.source ?? "inspection"}, ${foundAt}, ${b.location ?? null},
      ${b.hazard_code ?? "OTHER"}, ${b.hazard_label ?? "其他"}, ${b.description},
      ${b.vendor_id ? Number(b.vendor_id) : null}, ${b.responsible_person ?? null},
      ${b.severity ?? "minor"}, ${actionType}, ${disp.join(",")}, ${penalty},
      ${!onsite && b.due_date ? b.due_date : null},
      ${onsite ? foundAt : null},
      ${onsite ? (b.fix_note || ONSITE_NOTE) : (b.fix_note ?? null)},
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

  // signing＝進入多人簽核：內容鎖定、完成簽核才算送出並產生 PDF
  const signing = b.signing === true;
  const inspectorName = (b.inspector_name || "").trim() || null;
  const vendorName = String(b.vendor_name ?? "").trim().slice(0, 64) || null;
  const slots = signing ? JSON.stringify(inspectionSlots(inspectorName, vendorName)) : null;
  const [insp] = await db.sql`
    INSERT INTO inspections
      (site_id, building, form_code, inspect_date, location, weather, inspector_id,
       inspector_name, vendor_name, status, submitted_at, sign_slots)
    VALUES (${Number(b.site_id)}, ${(b.building || "").trim() || null},
            ${b.form_code}, ${b.inspect_date || todayISO()},
            ${b.location ?? null}, ${b.weather ?? null}, ${me.id},
            ${inspectorName}, ${vendorName},
            ${signing ? "signing" : "submitted"},
            CASE WHEN ${signing}::boolean THEN NULL ELSE NOW() END, ${slots}::jsonb)
    RETURNING id, site_id, building, form_code, inspect_date, location, weather,
              inspector_name, submitted_at`;

  await writeResults(insp.id, b.results || []);

  const findingIds: number[] = [];
  for (const f of b.findings || []) {
    findingIds.push(await insertFinding(
      // 缺失沿用表單的棟別：缺失是在填這張表時發現的，棟別必然相同
      { ...f, source: "inspection", location: f.location ?? insp.location,
        building: insp.building },
      me, { siteId: insp.site_id, inspectionId: insp.id },
    ));
  }

  await learnFromForm(insp.site_id, [{ vendor: vendorName }], b.findings);

  if (signing) {
    return json({ ok: true, inspection_id: insp.id, finding_ids: findingIds,
                  sign_url: `/static/sign.html?kind=inspection&id=${insp.id}` });
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
    SELECT i.id, i.inspect_date, i.location, i.weather, i.submitted_at, i.building, i.sign_slots,
           i.revision, i.vendor_name,
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
    SELECT role, signer_name, signed_at, image_key, slot_key FROM signatures
    WHERE inspection_id = ${id} ORDER BY id`;
  const sigs = await hydrateSignatures(sigRows, insp.sign_slots);

  const bytes = await buildInspectionPdf(insp, results, findings, sigs, await findingPhotos(findings));
  // 每版一個檔：修改紀錄留著舊版的路徑，重新完成簽核不能蓋掉它
  const key = `pdf/INSP-${String(id).padStart(6, "0")}${insp.revision ? `-r${insp.revision}` : ""}.pdf`;
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
  const signing = b.signing === true;
  const slots = signing ? JSON.stringify(coordinationSlots(b.attendees || [])) : null;
  const [co] = await db.sql`
    INSERT INTO coordinations
      (site_id, building, meeting_date, work_date, weather, agreement_text, patrol_text,
       handling_text, recorder_name, status, submitted_at, created_by, sign_slots)
    VALUES (${Number(b.site_id)}, ${(b.building || "").trim() || null},
            ${b.meeting_date || todayISO()},
            ${b.work_date || todayISO()}, ${b.weather ?? null},
            ${b.agreement_text ?? null}, ${b.patrol_text ?? null},
            ${b.handling_text ?? null},
            ${(b.recorder_name || "").trim() || null},
            ${signing ? "signing" : "submitted"},
            CASE WHEN ${signing}::boolean THEN NULL ELSE NOW() END, ${me.id}, ${slots}::jsonb)
    RETURNING id, site_id, building`;

  await writeAttendees(co.id, b.attendees || []);

  const findingIds: number[] = [];
  for (const f of b.findings || []) {
    findingIds.push(await insertFinding(
      // 補登紙本時，缺失的發現時間預設跟隨該單的作業日期
      { found_at: b.work_date ? `${b.work_date}T09:00:00+08:00` : undefined,
        ...f, source: "coordination", building: co.building },
      me, { siteId: co.site_id, coordinationId: co.id },
    ));
  }

  await learnFromForm(co.site_id, attendeeRows(b.attendees), b.findings);

  if (signing) {
    return json({ ok: true, coordination_id: co.id, finding_ids: findingIds,
                  sign_url: `/static/sign.html?kind=coordination&id=${co.id}` });
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
    SELECT role, signer_name, signed_at, image_key, slot_key FROM signatures
    WHERE coordination_id = ${id} ORDER BY id`;
  const sigs = await hydrateSignatures(sigRows, co.sign_slots);

  const bytes = await buildCoordinationPdf(co, attendees, findings, sigs, await findingPhotos(findings));
  const key = `pdf/COORD-${String(id).padStart(6, "0")}${co.revision ? `-r${co.revision}` : ""}.pdf`;
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

/** 簽名列 → PDF 用。有簽名格的單：每格都列，沒簽的留「未簽」；舊單只列簽過的。 */
async function hydrateSignatures(rows: any[], slots?: any[] | null): Promise<SigInput[]> {
  const one = async (s: any) => ({
    role: s.role,
    signer_name: s.signer_name,
    printed_name: s.slot_key ? s.signer_name : undefined,   // 新流程的姓名是本人填的
    signed_at: new Date(s.signed_at)
      .toLocaleString("zh-TW", { timeZone: TZ, hour12: false }),
    image: await readFileBytes(s.image_key),
  });
  if (!slots) return Promise.all(rows.map(one));
  return Promise.all(slotsOf(slots).map(async (slot: any) => {
    const hit = rows.find((r) => r.slot_key === slot.key);
    if (hit) return one(hit);
    return { role: slot.role, signer_name: "", printed_name: slot.name || undefined,
             signed_at: "", image: null, unsigned: true };
  }));
}

// ---------------------------------------------------------------------------
// 多人分次簽核
// ---------------------------------------------------------------------------
const INSPECTION_SIGN_ROLES = ["檢查人員", "作業主管", "職安人員", "主辦工程師", "工程專案主管"];
const COORDINATION_SIGN_ROLES = ["工程專案主管", "職安人員", "施工組"];

/** 自主檢查表的簽名格：角色各一格，有填受檢廠商時廠商也簽一格。 */
function inspectionSlots(inspector: string | null, vendor: string | null) {
  const slots: { key: string; role: string; name: string | null }[] = INSPECTION_SIGN_ROLES.map((role) => ({
    key: role, role, name: role === "檢查人員" ? inspector : null,
  }));
  if (vendor) slots.push({ key: `供應商：${vendor}`, role: `供應商：${vendor}`, name: null });
  return slots;
}

/** 協議表的簽名格：角色各一格，參加協議的廠商各一格，帶入該廠商第一位人員當負責人。 */
function coordinationSlots(attendees: any[]) {
  const slots: { key: string; role: string; name: string | null }[] =
    COORDINATION_SIGN_ROLES.map((role) => ({ key: role, role, name: null }));
  const seen = new Set<string>();
  for (const a of attendees) {
    const vendor = String(a?.vendor_name ?? "").trim();
    if (!vendor || seen.has(vendor)) continue;
    seen.add(vendor);
    const key = `供應商：${vendor}`;
    const person = attendees.find((x) => String(x?.vendor_name ?? "").trim() === vendor
      && String(x?.person_name ?? "").trim());
    slots.push({ key, role: key, name: person ? String(person.person_name).trim() : null });
  }
  return slots;
}

/** sign_slots：驅動程式通常已解析成陣列，保險起見字串也接受。 */
const slotsOf = (v: any): any[] => (typeof v === "string" ? JSON.parse(v) : v) || [];

/** 單據的簽核資料（兩種單的欄位不同，這裡整理成一種形狀）。 */
async function loadSignDoc(kind: string, id: number): Promise<any | null> {
  if (kind === "inspection") {
    const d = (await db.sql`
      SELECT i.id, i.inspect_date AS date, i.building, i.location, i.status, i.sign_slots, i.pdf_key,
             i.revision, i.inspector_id AS owner, i.inspector_name AS person, s.name AS site, ft.title
      FROM inspections i JOIN sites s ON s.id = i.site_id
      JOIN form_templates ft ON ft.form_code = i.form_code WHERE i.id = ${id}`)[0];
    return d ? { ...d, no: `INSP-${String(id).padStart(6, "0")}` } : null;
  }
  const d = (await db.sql`
    SELECT c.id, c.work_date AS date, c.building, c.status, c.sign_slots, c.pdf_key,
           c.revision, c.created_by AS owner, c.recorder_name AS person, c.agreement_text, c.handling_text, s.name AS site,
           '每日協議巡視處理紀錄表' AS title
    FROM coordinations c JOIN sites s ON s.id = c.site_id WHERE c.id = ${id}`)[0];
  return d ? { ...d, no: `COORD-${String(id).padStart(6, "0")}` } : null;
}

async function writeResults(id: number, results: any[]) {
  for (const r of results) {
    await db.sql`
      INSERT INTO inspection_results (inspection_id, item_id, day, result, remark)
      VALUES (${id}, ${Number(r.item_id)}, ${r.day ?? null},
              ${r.result || "na"}, ${r.remark ?? null})`;
  }
}

async function writeAttendees(id: number, attendees: any[]) {
  for (const a of attendees) {
    await db.sql`
      INSERT INTO coordination_attendees
        (coordination_id, work_item, vendor_id, vendor_name, trade,
         person_name, employee_no, work_content)
      VALUES (${id}, ${a.work_item ?? null},
              ${a.vendor_id ? Number(a.vendor_id) : null}, ${a.vendor_name ?? null},
              ${a.trade ?? null}, ${a.person_name ?? null},
              ${a.employee_no ?? null}, ${a.work_content ?? null})`;
  }
}

// ---------------------------------------------------------------------------
// 修改已填單據：開新版。舊內容、舊簽名、舊 PDF 存進 form_revisions，單據回到簽核中。
// ---------------------------------------------------------------------------
/** 單據完整內容：帶回表單用，也是修改前的快照。 */
async function editData(kind: string, id: number): Promise<any | null> {
  const findings = await db.sql`
    SELECT f.id, f.item_id, f.description, f.hazard_code, f.hazard_label, f.severity, f.location,
           f.vendor_id, v.name AS vendor, f.responsible_person, f.action_type, f.dispositions,
           f.penalty, f.due_date, f.fix_note, f.photo_before, f.photo_after, f.status
    FROM findings f LEFT JOIN vendors v ON v.id = f.vendor_id
    WHERE (${kind}::text = 'inspection' AND f.inspection_id = ${id})
       OR (${kind}::text = 'coordination' AND f.coordination_id = ${id})
    ORDER BY f.id`;
  const shaped = findings.map((f: any) => ({
    ...f, due_date: dateOnly(f.due_date), penalty: f.penalty == null ? null : Number(f.penalty),
    dispositions: parseDispositions(f.dispositions, f.action_type),
  }));
  if (kind === "inspection") {
    const h = (await db.sql`
      SELECT id, site_id, building, form_code, inspect_date, location, weather, inspector_name,
             vendor_name, status, revision, pdf_key, inspector_id AS owner
      FROM inspections WHERE id = ${id}`)[0];
    if (!h) return null;
    const results = await db.sql`
      SELECT item_id, result, remark FROM inspection_results WHERE inspection_id = ${id}`;
    return { ...h, inspect_date: dateOnly(h.inspect_date), results, findings: shaped };
  }
  const h = (await db.sql`
    SELECT id, site_id, building, meeting_date, work_date, weather, agreement_text, handling_text,
           recorder_name, status, revision, pdf_key, created_by AS owner
    FROM coordinations WHERE id = ${id}`)[0];
  if (!h) return null;
  const attendees = await db.sql`
    SELECT work_item, vendor_id, vendor_name, trade, person_name, employee_no, work_content
    FROM coordination_attendees WHERE coordination_id = ${id} ORDER BY id`;
  return { ...h, meeting_date: dateOnly(h.meeting_date), work_date: dateOnly(h.work_date),
           attendees, findings: shaped };
}

/** 改內容與作廢：職安、主管、管理員，或填這張單的帳號。簽名不限：下一位簽的人可能沒有帳號權限。 */
function canAlter(me: SessionUser, owner: number | null): boolean {
  return ["safety", "manager", "admin"].includes(me.role) || me.id === owner;
}

/** 舊內容、舊簽名名單與舊 PDF 存進 form_revisions。回 false＝這一版已經有人先存了。 */
async function saveRevision(
  kind: string, id: number, data: any, rev: number, reason: string, me: SessionUser,
): Promise<boolean> {
  const sigs = await db.sql`
    SELECT role, signer_name, signed_at, image_key FROM signatures
    WHERE (${kind}::text = 'inspection' AND inspection_id = ${id})
       OR (${kind}::text = 'coordination' AND coordination_id = ${id})`;
  const rows = await db.sql`
    INSERT INTO form_revisions (doc_key, rev, reason, edited_by, edited_name, snapshot, pdf_key)
    VALUES (${`${kind}:${id}`}, ${rev}, ${reason}, ${me.id}, ${me.name},
            ${JSON.stringify({ ...data, signatures: sigs })}::jsonb, ${data.pdf_key ?? null})
    ON CONFLICT (doc_key, rev) DO NOTHING
    RETURNING id`;
  return rows.length > 0;
}

/** 缺失依 id 對回去，保留單號與改善進度。已結案的不動；表單上拿掉的刪除，新增的開新單。 */
async function syncFindings(
  kind: string, id: number, list: any[], me: SessionUser,
  base: { site_id: number; building: string | null; location?: string | null; source: string; date: string },
) {
  const old = await db.sql`
    SELECT id, status, action_type FROM findings
    WHERE (${kind}::text = 'inspection' AND inspection_id = ${id})
       OR (${kind}::text = 'coordination' AND coordination_id = ${id})`;
  const keep = new Set<number>();
  for (const f of list) {
    const prev = f.id ? old.find((o: any) => o.id === Number(f.id)) : null;
    if (!prev) {
      await insertFinding({ found_at: `${base.date}T09:00:00+08:00`,   // 補登的缺失跟著單據日期
                            ...f, id: undefined, source: base.source, building: base.building,
                            location: f.location ?? base.location },
                          me, kind === "inspection"
                            ? { siteId: base.site_id, inspectionId: id }
                            : { siteId: base.site_id, coordinationId: id });
      continue;
    }
    keep.add(prev.id);
    if (prev.status === "closed") continue;
    const { disp, onsite, actionType, penalty } = dispositionOf(f);
    // 處置從「之後改善」改成「立即改善」就算改善完成；反過來則回到改善中。沒換類型維持原狀態：
    // 被覆驗退回的缺失改個錯字，不能跳回待複驗
    const changed = (prev.action_type === "onsite") !== onsite;
    const status = !changed ? prev.status : onsite ? "fixed" : "open";
    const note = !changed ? (f.fix_note ?? null)
      : onsite ? (f.fix_note || ONSITE_NOTE)
      : (f.fix_note === ONSITE_NOTE ? null : f.fix_note ?? null);
    await db.sql`
      UPDATE findings SET description = ${f.description}, hazard_code = ${f.hazard_code ?? "OTHER"},
        hazard_label = ${f.hazard_label ?? "其他"}, severity = ${f.severity ?? "minor"},
        location = COALESCE(${f.location ?? base.location ?? null}, location),
        building = ${base.building},
        vendor_id = ${f.vendor_id ? Number(f.vendor_id) : null},
        responsible_person = ${f.responsible_person ?? null}, action_type = ${actionType},
        dispositions = ${disp.join(",")}, penalty = ${penalty},
        due_date = ${onsite ? null : (f.due_date ?? null)}, fix_note = ${note},
        photo_before = ${f.photo_before ?? null}, photo_after = ${f.photo_after ?? null},
        status = ${status},
        fixed_at = CASE WHEN ${status}::text = 'open' THEN NULL ELSE COALESCE(fixed_at, found_at) END
      WHERE id = ${prev.id}`;
  }
  for (const o of old as any[]) {
    if (!keep.has(o.id) && o.status !== "closed") {
      await db.sql`DELETE FROM findings WHERE id = ${o.id}`;
    }
  }
}

const STALE_DOC = "這張單剛被修改過，請重新整理後再操作";

async function handleEdit(
  req: Request, me: SessionUser, kind: string, id: number, method: string,
): Promise<Response> {
  const data = await editData(kind, id);
  if (!data) return fail(404, "查無此單據");
  if (data.status === "void") return fail(409, "這張單已作廢，不能修改");
  if (method === "GET") return json(data);
  if (method !== "POST") return fail(405, "方法不支援");
  if (!canAlter(me, data.owner)) return fail(403, "僅填表人、職安人員或主管可修改");

  const b = await req.json();
  if (Number(b.revision) !== data.revision) return fail(409, STALE_DOC);
  const reason = String(b.reason ?? "").trim();
  if (!reason) return fail(400, "請填寫修改原因");
  if (reason.length > 200) return fail(400, "修改原因請在 200 字以內");
  const bad = buildingTooLong(b.building);
  if (bad) return bad;

  // 先存舊版：後面任何一步失敗，舊內容都還找得回來。兩人同時修改時後者在這裡被擋下
  const rev = data.revision + 1;
  if (!await saveRevision(kind, id, data, rev, reason, me)) return fail(409, STALE_DOC);

  const building = String(b.building ?? "").trim() || null;
  if (kind === "inspection") {
    const inspector = String(b.inspector_name ?? "").trim() || null;
    const vendorName = String(b.vendor_name ?? "").trim().slice(0, 64) || null;
    const slots = JSON.stringify(inspectionSlots(inspector, vendorName));
    await db.sql`
      UPDATE inspections SET building = ${building}, inspect_date = ${b.inspect_date || data.inspect_date},
        location = ${b.location ?? null}, weather = ${b.weather ?? null}, inspector_name = ${inspector},
        vendor_name = ${vendorName},
        status = 'signing', submitted_at = NULL, pdf_key = NULL, sign_slots = ${slots}::jsonb,
        revision = ${rev}
      WHERE id = ${id}`;
    await db.sql`DELETE FROM signatures WHERE inspection_id = ${id}`;
    await db.sql`DELETE FROM inspection_results WHERE inspection_id = ${id}`;
    await writeResults(id, b.results || []);
    await syncFindings(kind, id, b.findings || [], me,
      { site_id: data.site_id, building, location: b.location ?? null, source: "inspection",
        date: b.inspect_date || data.inspect_date });
    await learnFromForm(data.site_id, [{ vendor: vendorName }], b.findings);
  } else {
    const slots = JSON.stringify(coordinationSlots(b.attendees || []));
    await db.sql`
      UPDATE coordinations SET building = ${building}, meeting_date = ${b.meeting_date || data.meeting_date},
        work_date = ${b.work_date || data.work_date}, weather = ${b.weather ?? null},
        agreement_text = ${b.agreement_text ?? null}, handling_text = ${b.handling_text ?? null},
        recorder_name = ${String(b.recorder_name ?? "").trim() || null},
        status = 'signing', submitted_at = NULL, pdf_key = NULL, sign_slots = ${slots}::jsonb,
        revision = ${rev}
      WHERE id = ${id}`;
    await db.sql`DELETE FROM signatures WHERE coordination_id = ${id}`;
    await db.sql`DELETE FROM coordination_attendees WHERE coordination_id = ${id}`;
    await writeAttendees(id, b.attendees || []);
    await syncFindings(kind, id, b.findings || [], me,
      { site_id: data.site_id, building, source: "coordination",
        date: b.work_date || data.work_date });
    await learnFromForm(data.site_id, attendeeRows(b.attendees), b.findings);
  }
  return json({ ok: true, revision: rev, sign_url: `/static/sign.html?kind=${kind}&id=${id}` });
}

async function pendingSigns(): Promise<Response> {
  const rows = await db.sql`
    SELECT * FROM (
      SELECT 'inspection' AS kind, i.id, i.inspect_date AS d, i.building, s.name AS site, ft.title,
             i.inspector_name AS person, i.sign_slots, i.created_at,
             (SELECT COUNT(*)::int FROM signatures g WHERE g.doc_key = 'inspection:' || i.id) AS signed
      FROM inspections i JOIN sites s ON s.id = i.site_id
      JOIN form_templates ft ON ft.form_code = i.form_code
      WHERE i.status = 'signing'
      UNION ALL
      SELECT 'coordination', c.id, c.work_date, c.building, s.name, '每日協議巡視處理紀錄表',
             c.recorder_name, c.sign_slots, c.created_at,
             (SELECT COUNT(*)::int FROM signatures g WHERE g.doc_key = 'coordination:' || c.id)
      FROM coordinations c JOIN sites s ON s.id = c.site_id
      WHERE c.status = 'signing'
    ) x ORDER BY created_at DESC LIMIT 100`;
  return json(rows.map((r: any) => ({
    kind: r.kind, id: r.id, date: dateOnly(r.d), building: r.building, site: r.site, title: r.title,
    person: r.person, signed: r.signed, total: slotsOf(r.sign_slots).length,
    url: `/static/sign.html?kind=${r.kind}&id=${r.id}`,
  })));
}

async function handleSign(
  req: Request, me: SessionUser, kind: string, id: number, action: string, method: string,
): Promise<Response> {
  const doc = await loadSignDoc(kind, id);
  if (!doc) return fail(404, "查無此單據");
  const docKey = `${kind}:${id}`;
  const slots = slotsOf(doc.sign_slots);

  if (!action && method === "GET") {
    const sigs = await db.sql`
      SELECT slot_key, signer_name, signed_at FROM signatures WHERE doc_key = ${docKey}`;
    const findings = await db.sql`
      SELECT f.description, f.responsible_person, v.name AS vendor
      FROM findings f LEFT JOIN vendors v ON v.id = f.vendor_id
      WHERE (${kind}::text = 'inspection' AND f.inspection_id = ${id})
         OR (${kind}::text = 'coordination' AND f.coordination_id = ${id})
      ORDER BY f.id`;
    const attendees = kind === "coordination" ? await db.sql`
      SELECT vendor_name, person_name, work_content FROM coordination_attendees
      WHERE coordination_id = ${id} ORDER BY id` : [];
    const revisions = await db.sql`
      SELECT id, rev, reason, edited_name, created_at, pdf_key FROM form_revisions
      WHERE doc_key = ${docKey} ORDER BY rev DESC`;
    return json({
      revisions: revisions.map((r: any) => ({
        rev: r.rev, reason: r.reason, edited_name: r.edited_name, at: minuteISO(r.created_at),
        pdf_url: r.pdf_key ? `/api/revisions/${r.id}/pdf` : null,
      })),
      kind, id, no: doc.no, title: doc.title, date: dateOnly(doc.date), site: doc.site,
      revision: doc.revision,
      building: doc.building, location: doc.location ?? null, person: doc.person,
      status: doc.status, agreement_text: doc.agreement_text ?? null,
      handling_text: doc.handling_text ?? null, attendees, findings,
      pdf_url: doc.pdf_key ? `/api/${kind}s/${id}/pdf` : null,
      slots: slots.map((s) => {
        const hit = sigs.find((x: any) => x.slot_key === s.key);
        return { ...s, signed: !!hit, signer_name: hit?.signer_name ?? null,
                 signed_at: hit ? minuteISO(hit.signed_at) : null };
      }),
    });
  }
  if (method !== "POST") return fail(405, "方法不支援");
  // 已完成的單填錯也要能作廢；其他動作只在簽核中
  const voidable = action === "void" && doc.status === "submitted";
  if (doc.status !== "signing" && !voidable) return fail(409, "這張單已完成簽核或已作廢，請重新整理");
  const b = await req.json();
  // 頁面開著時內容可能被改過：簽名是對看到的內容負責，舊頁面不能簽到新內容上
  if (Number(b.revision) !== doc.revision) return fail(409, STALE_DOC);

  if (action === "sign") {
    const slot = slots.find((s) => s.key === b.slot_key);
    if (!slot) return fail(400, "沒有這個簽名欄");
    const name = String(b.signer_name ?? "").trim();
    if (!name || name.length > 32) return fail(400, "請填寫簽名人姓名");
    const image = String(b.image ?? "");
    if (!image.startsWith("data:image/png;base64,")) return fail(400, "簽名格式錯誤");
    // 同一份單出現一模一樣的簽名圖，就是同一張圖被重複送出，不是另一個人簽的
    const hash = createHash("sha256").update(image).digest("hex");
    const same = (await db.sql`
      SELECT role FROM signatures WHERE doc_key = ${docKey} AND image_hash = ${hash}`)[0];
    if (same) return fail(409, `這個簽名和「${same.role}」完全相同，請本人重新簽名`);
    const key = await saveDataUrl(image, "signatures");
    const rows = await db.sql`
      INSERT INTO signatures
        (inspection_id, coordination_id, role, signer_id, signer_name, image_key,
         doc_key, slot_key, image_hash)
      VALUES (${kind === "inspection" ? id : null}, ${kind === "coordination" ? id : null},
              ${slot.role}, ${me.id}, ${name}, ${key}, ${docKey}, ${slot.key}, ${hash})
      ON CONFLICT (doc_key, slot_key) DO NOTHING
      RETURNING id`;
    if (!rows.length) return fail(409, "這一格剛被別人簽了，請重新整理");
    return json({ ok: true });
  }

  if (action === "undo") {
    const sig = (await db.sql`
      SELECT signer_id FROM signatures WHERE doc_key = ${docKey} AND slot_key = ${String(b.slot_key)}`)[0];
    if (sig && sig.signer_id !== me.id && !canAlter(me, doc.owner)) {
      return fail(403, "僅簽名的帳號、填表人、職安人員或主管可清除簽名");
    }
    await db.sql`DELETE FROM signatures WHERE doc_key = ${docKey} AND slot_key = ${String(b.slot_key)}`;
    return json({ ok: true });
  }

  if (action === "finish") {
    const n = (await db.sql`SELECT COUNT(*)::int AS n FROM signatures WHERE doc_key = ${docKey}`)[0].n;
    if (!n) return fail(400, "至少要有一個人簽名才能完成");
    // 全部簽完任何帳號都能按（最後一位可能換裝置）；留空格提前完成會讓 PDF 標未簽，要有權限
    if (n < slots.length && !canAlter(me, doc.owner)) {
      return fail(403, "還有未簽的格子，僅填表人、職安人員或主管可提前完成");
    }
    // PDF 印提交時間，要先寫；狀態等 PDF 產好才改：產檔失敗或逾時，單據還在簽核中，再按一次就好
    let done: any[];
    if (kind === "inspection") {
      await db.sql`UPDATE inspections SET submitted_at = NOW() WHERE id = ${id}`;
      await renderInspectionPdf(id);
      done = await db.sql`
        UPDATE inspections SET status = 'submitted'
        WHERE id = ${id} AND status = 'signing' AND revision = ${doc.revision} RETURNING id`;
    } else {
      await db.sql`UPDATE coordinations SET submitted_at = NOW() WHERE id = ${id}`;
      await renderCoordinationPdf(id);
      done = await db.sql`
        UPDATE coordinations SET status = 'submitted'
        WHERE id = ${id} AND status = 'signing' AND revision = ${doc.revision} RETURNING id`;
    }
    if (!done.length) return fail(409, STALE_DOC);
    return json({ ok: true, pdf_url: `/api/${kind}s/${id}/pdf` });
  }

  if (action === "void") {
    if (!canAlter(me, doc.owner)) return fail(403, "僅填表人、職安人員或主管可作廢");
    const reason = String(b.reason ?? "").trim();
    if (!reason) return fail(400, "請填寫作廢原因");
    if (reason.length > 200) return fail(400, "作廢原因請在 200 字以內");
    // 內容、簽名與缺失整份留進修改紀錄，稽核才查得到作廢了什麼、誰作廢的
    const rev = doc.revision + 1;
    const snapshot = await editData(kind, id);
    if (!await saveRevision(kind, id, snapshot, rev, `作廢：${reason}`, me)) return fail(409, STALE_DOC);
    // 這張單開出的缺失一起移除，否則重填後會重複。已結案的經過覆驗，留著不動
    if (kind === "inspection") {
      await db.sql`DELETE FROM findings WHERE inspection_id = ${id} AND status <> 'closed'`;
      await db.sql`UPDATE inspections SET status = 'void', revision = ${rev} WHERE id = ${id}`;
    } else {
      await db.sql`DELETE FROM findings WHERE coordination_id = ${id} AND status <> 'closed'`;
      await db.sql`UPDATE coordinations SET status = 'void', revision = ${rev} WHERE id = ${id}`;
    }
    return json({ ok: true });
  }
  return fail(404, "找不到簽核動作");
}

async function servePdf(table: "inspections" | "coordinations", id: number) {
  const rows = table === "inspections"
    ? await db.sql`SELECT pdf_key, status FROM inspections WHERE id = ${id}`
    : await db.sql`SELECT pdf_key, status FROM coordinations WHERE id = ${id}`;
  let key = rows[0]?.pdf_key;
  if (!key && rows[0]?.status === "submitted") {     // 已完成但 PDF 沒產成：看的時候補產
    key = await (table === "inspections" ? renderInspectionPdf(id) : renderCoordinationPdf(id));
  }
  if (!key) return fail(404, "PDF 尚未產生");
  return servePdfKey(key);
}

async function servePdfKey(key: string) {
  const data = await files().get(key, { type: "stream" });
  if (!data) return fail(404, "PDF 檔案不存在");
  return pdfResponse(data as ReadableStream, key.split("/").pop()!);
}

export const config: Config = {
  path: "/api/*",
};
