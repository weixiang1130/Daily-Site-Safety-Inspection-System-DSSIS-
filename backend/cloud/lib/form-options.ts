// 填報預設清單：把 LINE 出工回報與表單內容清理成可選的選項。
//
// 回報是人打的，原文不能直接當選項：廠商欄常帶人數或工項（「某某移工13人」「某某
// 鋼筋綁紮」）、也會混進棟別；同一工種有多種寫法；施作項目混著機具清單。規則只處理
// 寫法，不寫死任何廠商或人名（公開版控）。規則抓不到的，現場可從選單移除。

/** 欄位：vendor 廠商、trade 職種、person 人員、work_item 作業項目、content 作業內容 */
export const OPTION_FIELDS = ["vendor", "trade", "person", "work_item", "content"];

/** 回報沒寫廠商時的標記，與地端解析器（collectors/worklog.py）相同。 */
export const UNNAMED_VENDOR = "未標示廠商";

/** 同工種的不同寫法。worklog_trade_aliases（人工維護）優先於這份。 */
const TRADE_ALIASES: Record<string, string> = {
  電工班: "電班", 水工班: "水班", 電銲工: "電焊工", 電銲小工: "電焊小工", 監火工: "監火員",
};

const AREA_PREFIX = /^(住宅|商辦|辦公|[A-Za-z][棟動])(區|棟)?/;
const AREA_ONLY = /^(住宅|商辦|辦公)(區|棟)?$/;
const NOT_A_NAME = /作業主管|勞工|劳工|工人|更正|修正/;
/** 回報者把工項填在廠商欄。只比整格：「某某模板」「某某鷹架」是正常的廠商名。 */
const WORK_TERM = /^(鋼承板|續接器鎖接|續接器|輕隔間|連續壁|鋼筋綁紮|模板|鷹架|放樣|粉光|防火批覆|壓送|植釘|地改|未標示廠商)$/;
/** 廠商名後面接的工項或班別：「某某連續壁」「某某移工」。沒有這些字的是另一家
    （「某某鋼鐵」不能拆成某某）。前端 common.js 的 cleanVendor 用同一份。 */
const WORK_SUFFIX = /鋼承板|續接器|輕隔間|連續壁|電梯井|綁紮|模板|模扳|鷹架|放樣|粉光|防火|壓送|植釘|地改|水電|帷幕|配管|吊裝|泥作|油漆|防水|機電|鋼筋|鋼構|監控|移工|點工|泰工/;
/** 工項黏在廠商名前面（「放樣某某」）。 */
const WORK_PREFIX = /^(放樣|測量|泥作|油漆)(?=\S{2})/;
/** 公司全名不拆：主檔的「某某模板工程行」就是登記名稱。 */
const COMPANY_TAIL = /(公司|工程行|企業社?|實業|工業)$/;

/** 回報廠商欄 → {廠商, 順帶寫在後面的工項}。不像廠商名回 vendor=null。 */
export function cleanVendor(raw: unknown): { vendor: string | null; work: string | null } {
  const s = String(raw ?? "")
    .replace(/[（(][^）)]*[）)]/g, " ")      // 「(含工務)」
    .replace(/\s*\d+\s*[人名工].*$/, "")     // 「13人」
    .replace(/^[\s.。．、,，+＋\-]+/, "")      // 打字時多的標點
    .replace(AREA_PREFIX, "")                // 「住宅區某某」：棟別寫在廠商前面
    .trim();
  const m = /^(\S+)\s+(.+)$/.exec(s);
  let vendor = m ? m[1] : s;
  let work = m ? m[2].trim() : null;
  if (WORK_TERM.test(vendor)) {            // 「續接器鎖接 某某」：工項在前；沒寫廠商就只留工項
    if (!work) return { vendor: null, work: vendor };
    [vendor, work] = [work, vendor];
  }
  const pre = WORK_PREFIX.exec(vendor);
  if (pre && !work) { work = pre[1]; vendor = vendor.slice(pre[1].length); }
  const cut = COMPANY_TAIL.test(vendor) ? -1 : vendor.slice(2).search(WORK_SUFFIX);   // 廠商名至少兩個字
  if (cut >= 0) {
    work = [vendor.slice(cut + 2).replace("模扳", "模板"), work].filter(Boolean).join(" ");
    vendor = vendor.slice(0, cut + 2);
  }
  // 含數字或「區」的是工區與工項（1F出土、E區篩土），不是廠商
  if (vendor.length < 2 || vendor.length > 8 || /\d|[A-Za-z]區|區$/.test(vendor)
      || AREA_PREFIX.test(vendor) || NOT_A_NAME.test(s)) {
    return { vendor: null, work: null };
  }
  return { vendor, work };
}

/** 人名：2～5 個中文字才收，異體字統一（黄→黃）。 */
export function cleanPerson(raw: unknown): string | null {
  const s = String(raw ?? "").trim().replace(/黄/g, "黃");
  return /^[一-鿿]{2,5}$/.test(s) ? s : null;
}

/** 工種摘要「電班8、水班1」→ [{name, counted}]。counted=false 的多半是作業類別（模板）。 */
export function splitTrades(raw: unknown): { name: string; counted: boolean }[] {
  const out: { name: string; counted: boolean }[] = [];
  for (const part of String(raw ?? "").split(/[、，,；;+＋\n]/)) {
    const p = part.trim();
    const name = p.replace(/[\s*＊xX×]*\d+\s*[人名工]?$/, "").trim();
    if (name.length >= 2 && name.length <= 10 && !/\d|\*/.test(name)) {
      out.push({ name, counted: name !== p });
    }
  }
  return out;
}

/** 回報一列 → 廠商、工項、工種。廠商欄填工項時，廠商常寫在工種欄（兩欄填反）：
    工種欄只有一個沒寫人數、也不像工種的名稱才敢認定。 */
export function reportVendor(raw: unknown, tradeSummary: unknown) {
  const { vendor, work } = cleanVendor(raw);
  const parts = splitTrades(tradeSummary);
  const swapped = !vendor && work && parts.length === 1 && !parts[0].counted
    && !WORK_SUFFIX.test(parts[0].name) && !/[工班]$/.test(parts[0].name);
  return swapped
    ? { vendor: parts[0].name, work, trade: null }
    : { vendor, work, trade: tradeSummary == null ? null : String(tradeSummary) };
}

/** 一段施作項目的整理：去掉編號、欄位標籤（「工作內容：」）、開頭的連字號、結尾的人數。 */
function tidyTask(part: string): string {
  return part.trim()
    .replace(/^[\d一二三四五六七八九十]+[.、．)）]\s*/, "")
    .replace(/^(工作內容|作業內容|作業項目|施工項目|施作項目)?[:：]\s*/, "")
    .replace(/^[-－]+\s*/, "")
    .replace(/[-－]\s*\d+\s*[人名]$/, "")
    .trim();
}

/** 是作業內容，不是機具清單（760塔吊*1）、人數行或別棟別家的回報（「商辦棟：某某/工項」）。 */
function isTask(p: string): boolean {
  if (p.length < 2) return false;
  if (/[*＊×]\s*\d/.test(p) || /[：:]\s*\d*\s*人/.test(p) || /\d+\s*[人名]$/.test(p)) return false;
  if (/點工|作業主管/.test(p) || /^\d+$/.test(p)) return false;
  return !/^(住宅|商辦|辦公)(區|棟)?[:：]/.test(p);
}

const TASK_SPLIT = /[、，,；;\n]/;

/** 施作項目原文 → 帶進表單的作業內容。 */
export function cleanTaskText(raw: unknown): string {
  return String(raw ?? "").split(TASK_SPLIT).map(tidyTask).filter(isTask).join("、");
}

/** 施作項目 → 可當選項的片段（太長的是當天敘述，不當選項）。 */
export function splitTasks(raw: unknown): string[] {
  return String(raw ?? "").split(TASK_SPLIT).map(tidyTask).filter((p) => isTask(p) && p.length <= 24);
}

export interface SourceRow {
  vendor?: string | null;      // 廠商
  supervisor?: string | null;  // 人員
  trade_summary?: string | null;
  trades?: string[] | null;    // 逐工種明細
  tasks?: string | null;       // 作業內容
  work_item?: string | null;   // 表單直接填的作業項目
  report_date?: string | null;
}

export interface OptionRow {
  field: string; value: string; vendor: string; n: number; last_used: string | null;
}

/** 來源列 → 去重計數的選項。minContent：作業內容至少出現幾次才收（單次多半是當天
    敘述）；大於 1 時廠商也篩：只出現一次又長的名稱多半是打字或工項，不是廠商。 */
export function buildOptions(
  rows: SourceRow[], aliases: Record<string, string> = {}, minContent = 1,
): OptionRow[] {
  const seen = new Set(rows.map((r) => cleanVendor(r.vendor).vendor));
  const map = new Map<string, OptionRow>();
  const add = (field: string, value: string, vendor: string, last: string | null) => {
    const key = `${field}\u0000${value}\u0000${vendor}`;
    const o = map.get(key);
    if (o) {
      o.n++;
      if (last && (!o.last_used || last > o.last_used)) o.last_used = last;
    } else {
      map.set(key, { field, value, vendor, n: 1, last_used: last });
    }
  };
  const trade = (t: string) => aliases[t] || TRADE_ALIASES[t] || t;

  for (const r of rows) {
    let { vendor, work } = cleanVendor(r.vendor);
    const parts = splitTrades(r.trade_summary);
    // 廠商欄填了工項時，廠商常寫在工種欄（兩欄填反）：只有一個名稱才敢認定
    if (!vendor && work && parts.length === 1 && !parts[0].counted && seen.has(parts[0].name)) {
      vendor = parts[0].name;
      parts.length = 0;
    }
    const v = vendor || "";
    const last = r.report_date ? String(r.report_date).slice(0, 10) : null;
    if (vendor) add("vendor", vendor, "", last);
    const person = cleanPerson(r.supervisor);
    if (person) add("person", person, v, last);

    const names = new Set([...(r.trades || []).map((t) => String(t).trim()).filter(Boolean),
                           ...parts.map((p) => p.name)].map(trade));
    for (const t of names) add("trade", t, v, last);
    // 沒寫人數的摘要是作業類別（模板、鋼筋綁紮）；廠商欄順帶的工項同理
    const works = new Set(parts.filter((p) => !p.counted).map((p) => p.name));
    if (work) works.add(work);
    for (const w of splitTrades(r.work_item)) works.add(w.name);
    for (const w of works) add("work_item", w, v, last);
    for (const c of new Set(splitTasks(r.tasks))) add("content", c, v, last);
  }

  const contentTotal = new Map<string, number>();
  for (const o of map.values()) {
    if (o.field === "content") contentTotal.set(o.value, (contentTotal.get(o.value) || 0) + o.n);
  }
  const vendorNames = [...map.values()].filter((o) => o.field === "vendor").map((o) => o.value);
  return [...map.values()].filter((o) => {
    if (o.field === "content") {
      // 「某某/工項」是一則回報列好幾家的格式，不是作業內容
      if (vendorNames.some((n) => o.value.startsWith(`${n}/`) || o.value.startsWith(`${n}／`))) return false;
      return (contentTotal.get(o.value) || 0) >= minContent;
    }
    if (o.field === "vendor") return minContent <= 1 || o.n >= 2 || o.value.length <= 3;
    if (o.field === "work_item") {
      return !AREA_ONLY.test(o.value) && !vendorNames.includes(o.value) && o.value !== "未標示廠商";
    }
    return true;
  });
}

/** 廠商主檔對照出的錯字：不在主檔、跟主檔某家只差一個字，而且回報的主管是同一人。
    只差一字不夠：兩個字的名字很容易只差一字卻是不同廠商，所以一定要有同一人佐證。
    現場改過的（renames 有提到）與親手新增的（confirmed）不動，以人為準。 */
export function vendorTypos(
  opts: OptionRow[], master: string[], renames: { field: string; value: string; canonical: string }[],
  confirmed: string[] = [],
): { field: string; value: string; canonical: string }[] {
  const moved = renames.filter((r) => r.field === "vendor");
  const renamedAway = new Set(moved.map((r) => r.value));
  const touched = new Set([...renamedAway, ...moved.map((r) => r.canonical), ...confirmed]);
  const peopleOf = (v: string) =>
    new Set(opts.filter((o) => o.field === "person" && o.vendor === v).map((o) => o.value));
  const oneOff = (a: string, b: string) =>
    a.length === b.length && [...a].filter((c, i) => c !== b[i]).length === 1;
  const out: { field: string; value: string; canonical: string }[] = [];
  for (const o of opts) {
    if (o.field !== "vendor" || master.includes(o.value) || touched.has(o.value)) continue;
    const people = peopleOf(o.value);
    const hits = master.filter((m) => !renamedAway.has(m) && oneOff(o.value, m)
      && [...peopleOf(m)].some((p) => people.has(p)));
    if (hits.length === 1) out.push({ field: "vendor", value: o.value, canonical: hits[0] });
  }
  return out;
}

/** 人員一格一個人：「王大明/李小華」統計時無法歸到個人。 */
export const PERSON_SEPARATOR = /[\/／、,，;；\s]/;

/** 套用改正：舊值換成新值，換完同鍵的合併。 */
export function applyRenames(
  opts: OptionRow[], renames: { field: string; value: string; canonical: string }[],
): OptionRow[] {
  const to = new Map(renames.map((r) => [`${r.field}\u0000${r.value}`, r.canonical]));
  const vendorTo = new Map(renames.filter((r) => r.field === "vendor").map((r) => [r.value, r.canonical]));
  const map = new Map<string, OptionRow>();
  for (const o of opts) {
    const value = to.get(`${o.field}\u0000${o.value}`) ?? o.value;
    const vendor = vendorTo.get(o.vendor) ?? o.vendor;
    const key = `${o.field}\u0000${value}\u0000${vendor}`;
    const hit = map.get(key);
    if (hit) {
      hit.n += o.n;
      if (o.last_used && (!hit.last_used || o.last_used > hit.last_used)) hit.last_used = o.last_used;
    } else {
      map.set(key, { ...o, value, vendor });
    }
  }
  return [...map.values()];
}

type Sql = (strings: TemplateStringsArray, ...values: any[]) => Promise<any[]>;

/** 依近一年出工回報重算某工地的 worklog 選項。已移除的（active=FALSE）留著，
    每晚重算才不會把它加回來；清理規則改變後不再產生的舊列刪掉。 */
export async function refreshFormOptions(sql: Sql, siteCode: string): Promise<number> {
  const rows = await sql`
    SELECT r.vendor, r.supervisor, r.trade_summary, r.tasks, r.report_date::text AS report_date,
           (SELECT json_agg(t.trade) FROM worklog_trades t WHERE t.report_id = r.id) AS trades
    FROM worklog_reports r
    WHERE r.site_code = ${siteCode} AND r.report_date >= CURRENT_DATE - 365`;
  const aliasRows = await sql`SELECT trade, trade_group FROM worklog_trade_aliases`;
  const aliases = Object.fromEntries(aliasRows.map((a: any) => [a.trade, a.trade_group]));
  const renames = await sql`
    SELECT field, value, canonical FROM form_options
    WHERE site_code = ${siteCode} AND canonical IS NOT NULL
    GROUP BY field, value, canonical`;
  const master = (await sql`SELECT name FROM vendors WHERE active`).map((m: any) => m.name);
  // 表單累積的舊寫法（「某某模板」）已拆成廠商＋工項，不再是選項；現場親手加的不動
  const learned = await sql`
    SELECT DISTINCT value FROM form_options
    WHERE site_code = ${siteCode} AND field = 'vendor' AND source = 'form' AND canonical IS NULL`;
  for (const { value } of learned as any[]) {
    const { vendor } = cleanVendor(value);
    if (vendor && vendor !== value) {
      await sql`DELETE FROM form_options
                WHERE site_code = ${siteCode} AND field = 'vendor' AND source = 'form'
                  AND value = ${value} AND canonical IS NULL`;
    }
  }
  const built = buildOptions(rows, aliases, 2);
  // 被併掉後現場又親手加回來的不是錯字，不能每晚再併一次
  const confirmed = await sql`
    SELECT DISTINCT value FROM form_options
    WHERE site_code = ${siteCode} AND field = 'vendor' AND source = 'manual' AND active`;
  const typos = vendorTypos(built, master, renames as any, confirmed.map((c: any) => c.value));
  const fresh = JSON.stringify(applyRenames(built, [...(renames as any), ...typos]));
  // 同一語句內不能先刪再插同一主鍵，所以只刪「新清單沒有的」，其餘 upsert
  await sql`
    WITH fresh AS (
      SELECT * FROM json_to_recordset(${fresh}::json)
        AS x(field text, value text, vendor text, n int, last_used date)
    ), gone AS (
      DELETE FROM form_options o
      WHERE o.site_code = ${siteCode} AND o.source = 'worklog' AND o.active
        AND NOT EXISTS (SELECT 1 FROM fresh f
                        WHERE f.field = o.field AND f.value = o.value AND f.vendor = o.vendor)
    )
    INSERT INTO form_options (site_code, field, value, vendor, source, n, last_used)
    SELECT ${siteCode}::text, field, value, vendor, 'worklog', n, last_used FROM fresh
    ON CONFLICT (site_code, field, value, vendor, source) DO UPDATE SET
      n = EXCLUDED.n, last_used = EXCLUDED.last_used, updated_at = NOW()`;
  // 錯字記成「已改正」，跟現場按 ✎ 同一種紀錄：LINE 帶入也會換名，現場可再改回來
  for (const t of typos) {
    await sql`
      INSERT INTO form_options (site_code, field, value, vendor, source, n, active, canonical)
      VALUES (${siteCode}, 'vendor', ${t.value}, '', 'worklog', 0, FALSE, ${t.canonical})
      ON CONFLICT (site_code, field, value, vendor, source) DO UPDATE SET
        active = FALSE, canonical = EXCLUDED.canonical, updated_at = NOW()`;
  }
  return JSON.parse(fresh).length;
}

/** 表單送出時累加使用次數：清單會跟著現場實際填的內容長。 */
export async function learnFormOptions(
  sql: Sql, siteCode: string, rows: SourceRow[],
): Promise<void> {
  const opts = JSON.stringify(buildOptions(rows));
  await sql`
    INSERT INTO form_options (site_code, field, value, vendor, source, n, last_used)
    SELECT ${siteCode}::text, field, value, vendor, 'form', n, CURRENT_DATE
    FROM json_to_recordset(${opts}::json) AS x(field text, value text, vendor text, n int)
    ON CONFLICT (site_code, field, value, vendor, source) DO UPDATE SET
      n = form_options.n + EXCLUDED.n, last_used = CURRENT_DATE, updated_at = NOW()`;
}
