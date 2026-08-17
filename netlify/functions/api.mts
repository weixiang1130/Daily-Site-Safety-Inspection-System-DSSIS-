// 職安填報系統 API（Netlify Functions）
//
// 單一函式處理所有 /api/* 路由，減少冷啟動並讓 session 處理集中在一處。
// 資料層：Netlify Database（Postgres）
// 檔案層：Netlify Blobs（照片、手寫簽名、產出的 PDF）

import type { Config, Context } from "@netlify/functions";
import { getDatabase } from "@netlify/database";
import { getDeployStore, getStore } from "@netlify/blobs";

import {
  clearSessionCookie, createSession, readSession, sessionCookieHeader,
  verifyPassword, type SessionUser,
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
const BRANDING = {
  system_name: Netlify.env.get("SYSTEM_NAME") || "職安填報系統",
  war_room_name: Netlify.env.get("WAR_ROOM_NAME") || "職安戰情室",
  org_name: Netlify.env.get("BRAND_NAME") || "示範營造股份有限公司",
  org_short: Netlify.env.get("BRAND_SHORT_NAME") || "示範營造",
  org_name_en: Netlify.env.get("BRAND_NAME_EN") || "Demo Construction",
  group_name: Netlify.env.get("BRAND_GROUP") || "",
};

/** 儀表板是否免登入。公開網際網路上務必維持 false。 */
const PUBLIC_DASHBOARD = Netlify.env.get("PUBLIC_DASHBOARD") === "true";

function ingestTokens(): Record<string, string> {
  const raw = Netlify.env.get("INGEST_TOKENS")
    || "vendor-a:demo-token-vendor-a,vendor-b:demo-token-vendor-b,vendor-c:demo-token-vendor-c";
  const out: Record<string, string> = {};
  for (const pair of raw.split(",")) {
    const i = pair.indexOf(":");
    if (i > 0) out[pair.slice(0, i).trim()] = pair.slice(i + 1).trim();
  }
  return out;
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

    // ---- 儀表板 ----
    if (p === "/api/dashboard") {
      if (!PUBLIC_DASHBOARD && !me) return fail(401, "請先登入");
      return json(await dashboard(url));
    }

    // ---- 檔案 ----
    if (p.startsWith(FILE_PREFIX)) {
      if (!me) return fail(401, "請先登入");
      const key = p.slice(FILE_PREFIX.length);
      const got = await files().getWithMetadata(key, { type: "arrayBuffer" });
      if (!got?.data) return fail(404, "檔案不存在");
      const ct = (got.metadata as any)?.contentType || "application/octet-stream";
      return new Response(got.data as ArrayBuffer, {
        headers: { "content-type": ct, "cache-control": "private, max-age=3600" },
      });
    }

    // ---- 以下皆需登入 ----
    if (!me) return fail(401, "請先登入");

    if (p === "/api/sites") {
      return json(await db.sql`
        SELECT id, code, name FROM sites WHERE active = TRUE ORDER BY id`);
    }

    if (p === "/api/vendors") {
      return json(await db.sql`
        SELECT id, code, name FROM vendors WHERE active = TRUE ORDER BY id`);
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
               s.name AS site, i.site_id, ft.title AS form_title,
               u.display_name AS inspector,
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
        SELECT c.id, c.work_date, c.status, c.pdf_key, s.name AS site,
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

    if (p === "/api/upload/photo" && method === "POST") {
      const form = await req.formData();
      const file = form.get("file");
      if (!(file instanceof File)) return fail(400, "缺少檔案");
      const ext = (file.name.split(".").pop() || "jpg").toLowerCase();
      if (!["jpg", "jpeg", "png", "webp"].includes(ext)) {
        return fail(400, "僅接受 jpg / png / webp");
      }
      const key = newKey("photos", ext === "jpeg" ? "jpg" : ext);
      await files().set(key, await file.arrayBuffer(), {
        metadata: { contentType: file.type || `image/${ext}` },
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
function shapeFinding(f: any) {
  const due = dateOnly(f.due_date);
  const overdue = f.action_type === "scheduled" && due
    && !["verified", "closed"].includes(f.status)
    && due < todayISO();
  return {
    id: f.id, no: `F${String(f.id).padStart(6, "0")}`, site: f.site, site_id: f.site_id,
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
  const [row] = await db.sql`
    INSERT INTO findings
      (site_id, inspection_id, coordination_id, item_id, source, found_at, location,
       hazard_code, hazard_label, description, vendor_id, responsible_person,
       severity, action_type, due_date, fixed_at, fix_note, status,
       photo_before, photo_after, created_by)
    VALUES (
      ${ids.siteId ?? b.site_id}, ${ids.inspectionId ?? null}, ${ids.coordinationId ?? null},
      ${b.item_id ?? null}, ${b.source ?? "inspection"}, NOW(), ${b.location ?? null},
      ${b.hazard_code ?? "OTHER"}, ${b.hazard_label ?? "其他"}, ${b.description},
      ${b.vendor_id ? Number(b.vendor_id) : null}, ${b.responsible_person ?? null},
      ${b.severity ?? "minor"}, ${b.action_type ?? "onsite"},
      ${!onsite && b.due_date ? b.due_date : null},
      ${onsite ? new Date().toISOString() : null},
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

  const [insp] = await db.sql`
    INSERT INTO inspections
      (site_id, form_code, inspect_date, location, weather, inspector_id,
       status, submitted_at)
    VALUES (${Number(b.site_id)}, ${b.form_code}, ${b.inspect_date || todayISO()},
            ${b.location ?? null}, ${b.weather ?? null}, ${me.id},
            'submitted', NOW())
    RETURNING id, site_id, form_code, inspect_date, location, weather, submitted_at`;

  for (const r of b.results || []) {
    await db.sql`
      INSERT INTO inspection_results (inspection_id, item_id, day, result, remark)
      VALUES (${insp.id}, ${Number(r.item_id)}, ${r.day ?? null},
              ${r.result || "na"}, ${r.remark ?? null})`;
  }

  const findingIds: number[] = [];
  for (const f of b.findings || []) {
    findingIds.push(await insertFinding(
      { ...f, source: "inspection", location: f.location ?? insp.location },
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
    SELECT i.id, i.inspect_date, i.location, i.weather, i.submitted_at,
           s.name AS site_name, ft.title AS form_title, u.display_name AS inspector_name
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
  const [co] = await db.sql`
    INSERT INTO coordinations
      (site_id, meeting_date, work_date, weather, agreement_text, patrol_text,
       handling_text, status, submitted_at, created_by)
    VALUES (${Number(b.site_id)}, ${b.meeting_date || todayISO()},
            ${b.work_date || todayISO()}, ${b.weather ?? null},
            ${b.agreement_text ?? null}, ${b.patrol_text ?? null},
            ${b.handling_text ?? null}, 'submitted', NOW(), ${me.id})
    RETURNING id, site_id`;

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
      { ...f, source: "coordination" }, me,
      { siteId: co.site_id, coordinationId: co.id },
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
    signed_at: new Date(s.signed_at).toLocaleString("zh-TW", { hour12: false }),
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

// ---------------------------------------------------------------------------
// 儀表板彙總
// ---------------------------------------------------------------------------
async function dashboard(url: URL) {
  const days = parseInt(url.searchParams.get("days") || "30", 10);
  const siteId = url.searchParams.get("site_id");
  const today = todayISO();

  const rows = await db.sql`
    SELECT f.*, s.name AS site, v.name AS vendor FROM findings f
    JOIN sites s ON s.id = f.site_id
    LEFT JOIN vendors v ON v.id = f.vendor_id
    WHERE f.found_at >= NOW() - (${days}::int * INTERVAL '1 day')
      AND (${siteId}::int IS NULL OR f.site_id = ${siteId}::int)`;
  const findings = rows.map((r: any) => ({ ...shapeFinding(r), raw: r }));

  const insps = await db.sql`
    SELECT id, site_id FROM inspections
    WHERE inspect_date = CURRENT_DATE
      AND (${siteId}::int IS NULL OR site_id = ${siteId}::int)`;

  const sites = await db.sql`SELECT id, name FROM sites WHERE active = TRUE ORDER BY id`;

  const count = (fn: (f: any) => boolean) => findings.filter(fn).length;
  const closed = count((f) => f.status === "closed");

  const durations = findings
    .filter((f) => f.raw.fixed_at && f.raw.found_at)
    .map((f) => (new Date(f.raw.fixed_at).getTime() - new Date(f.raw.found_at).getTime())
      / 3_600_000)
    .sort((a, b) => a - b);
  const medianFix = durations.length
    ? Math.round(durations[Math.floor(durations.length / 2)] * 10) / 10 : null;

  const tally = (key: (f: any) => string | null) => {
    const m = new Map<string, number>();
    for (const f of findings) {
      const k = key(f);
      if (k) m.set(k, (m.get(k) || 0) + 1);
    }
    return [...m.entries()].map(([label, c]) => ({ label, count: c }))
      .sort((a, b) => b.count - a.count);
  };

  const trend: Array<{ date: string; count: number }> = [];
  for (let i = 13; i >= 0; i--) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    trend.push({ date: d.toISOString().slice(0, 10), count: 0 });
  }
  const trendIdx = new Map(trend.map((t, i) => [t.date, i]));
  for (const f of findings) {
    // found_at 已是台北時區的 YYYY-MM-DDTHH:MM，直接取前十碼即為當地日期。
    const i = trendIdx.get(f.found_at!.slice(0, 10));
    if (i !== undefined) trend[i].count++;
  }

  const siteRows = sites.map((s: any) => {
    const mine = findings.filter((f) => f.site_id === s.id);
    const over = mine.filter((f) => f.overdue).length;
    const open = mine.filter((f) => ["open", "fixed"].includes(f.status)).length;
    return {
      site_id: s.id, site: s.name, findings: mine.length, open, overdue: over,
      inspections_today: insps.filter((i: any) => i.site_id === s.id).length,
      light: over ? "red" : (mine.some((f) => f.status === "open") ? "yellow" : "green"),
    };
  });

  return {
    generated_at: new Date().toISOString(),
    range_days: days,
    kpi: {
      findings_today: count((f) => f.found_at!.slice(0, 10) === today),
      findings_range: findings.length,
      onsite_fixed: count((f) => f.action_type === "onsite"),
      scheduled: count((f) => f.action_type === "scheduled"),
      open: count((f) => ["open", "fixed"].includes(f.status)),
      overdue: count((f) => f.overdue),
      closed_rate: findings.length
        ? Math.round((closed / findings.length) * 1000) / 10 : 100,
      median_fix_hours: medianFix,
      inspections_today: insps.length,
    },
    by_hazard: tally((f) => f.hazard_label || "其他"),
    by_vendor: tally((f) => f.vendor).slice(0, 10),
    trend,
    sites: siteRows,
    overdue_list: findings.filter((f) => f.overdue).map((f) => ({
      no: f.no, site: f.site, description: f.description, vendor: f.vendor || "",
      person: f.responsible_person, due_date: f.due_date,
      days_over: Math.floor(
        (Date.now() - new Date(f.due_date!).getTime()) / 86_400_000),
    })).sort((a, b) => b.days_over - a.days_over).slice(0, 20),
    recent: [...findings]
      .sort((a, b) => new Date(b.found_at).getTime() - new Date(a.found_at).getTime())
      .slice(0, 15)
      .map((f) => ({
        no: f.no, site: f.site, found_at: f.found_at!, hazard_label: f.hazard_label,
        description: f.description, vendor: f.vendor || "", status: f.status,
        action_type: f.action_type,
      })),
  };
}

export const config: Config = {
  path: "/api/*",
};
