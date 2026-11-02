// PDF 產出（存查用）。
//
// 使用 pdf-lib 嵌入 Noto Sans TC（SIL OFL 1.1）。
// 字型檔透過 netlify.toml 的 [functions] included_files 一併打包。
//
// **不可以開啟子集化。** fontkit 的子集化對這支字型是壞的，會掉字（實測數據
// 見 tools/build_pdf_font.py 的說明）。字型已在建置階段縮到系統需要的範圍，
// 這裡整份嵌入即可；代價是 PDF 約 1.8 MB，換來的是字一定畫得出來。
//
// 也不可以改用同目錄的 .otf——那是 CID-keyed CFF，pdf-lib 連不子集化都畫不對。

import fs from "node:fs/promises";
import path from "node:path";
import { PDFDocument, PDFFont, PDFPage, rgb } from "pdf-lib";
import fontkit from "@pdf-lib/fontkit";
import { FONT_COVERAGE } from "./font-coverage.ts";

const FONT_RELATIVE = "backend/cloud/assets/fonts/NotoSansTC-Regular.ttf";

const A4 = { w: 595.28, h: 841.89 };
const M = { left: 42, right: 42, top: 50, bottom: 56 };
const CONTENT_W = A4.w - M.left - M.right;

const INK = rgb(0.09, 0.08, 0.07);          // 溫暖的深墨色
const MUTED = rgb(0.42, 0.40, 0.36);
const LINE = rgb(0.72, 0.70, 0.66);
const ACCENT = rgb(0.14, 0.22, 0.29);       // 品牌深石板藍
const FAIL_BG = rgb(0.98, 0.92, 0.91);

let fontCache: Uint8Array | null = null;

async function loadFont(): Promise<Uint8Array> {
  if (fontCache) return fontCache;
  // included_files 打包後的實際位置會隨 Netlify 的 bundler 版本而異，
  // 逐一嘗試候選路徑，避免因單一路徑猜錯就整個 PDF 功能失效。
  const basename = path.basename(FONT_RELATIVE);
  const candidates = [
    path.join(process.cwd(), FONT_RELATIVE),
    path.join(process.cwd(), "..", FONT_RELATIVE),
    path.resolve(FONT_RELATIVE),
    path.join(process.cwd(), "assets/fonts", basename),
    path.join(process.cwd(), basename),
  ];
  for (const p of candidates) {
    try {
      fontCache = new Uint8Array(await fs.readFile(p));
      return fontCache;
    } catch {
      // 換下一個候選路徑
    }
  }
  throw new Error(
    `找不到 PDF 中文字型 ${FONT_RELATIVE}。` +
    `請確認 netlify.toml 的 [functions] included_files 已包含 assets/fonts/**`,
  );
}


/**
 * 把字型收錄範圍外的字換成 □。
 *
 * 字型是建置階段縮好的，遇到沒收錄的字（罕用姓名、異體字）pdf-lib 會直接
 * 畫不出來且不報錯。安全紀錄少一個字卻沒人發現是最糟的情況，因此寧可畫成
 * □ 並留下警告，之後再依實際資料把字補進 tools/build_pdf_font.py 的收錄範圍。
 */
const missingChars = new Set<string>();

function printable(s: string): string {
  let out = "";
  // 各種 Unicode 空白換成一般空格：新版 Node 的日期字串用 U+2009 分隔日期與時間，
  // 字型沒收錄，時間會印成「2026/10/30□17:51」
  for (const ch of String(s ?? "").replace(/[  -   ]/g, " ")) {
    const cp = ch.codePointAt(0)!;
    if (cp === 0x0A || covered(cp)) { out += ch; continue; }
    out += "□";
    missingChars.add(ch);
  }
  return out;
}

function covered(cp: number): boolean {
  // 區間已排序，用二分搜尋；一份 PDF 會呼叫上萬次，線性掃描太慢
  let lo = 0, hi = FONT_COVERAGE.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const [a, b] = FONT_COVERAGE[mid];
    if (cp < a) hi = mid - 1;
    else if (cp > b) lo = mid + 1;
    else return true;
  }
  return false;
}

/** 產出後呼叫一次，把這份文件遇到的缺字寫進記錄檔。 */
function reportMissing(title: string): void {
  if (!missingChars.size) return;
  console.warn(`[pdf] ${title}：字型未收錄 ${[...missingChars].join("")}，`
    + "已畫成 □。請將這些字補進 tools/build_pdf_font.py 的收錄範圍後重建字型。");
  missingChars.clear();
}

/** 依實際字寬折行，中英混排都適用。 */
function wrap(text: string, font: PDFFont, size: number, maxW: number): string[] {
  const out: string[] = [];
  // 先換掉缺字再量寬，否則折行位置會依「畫不出來的字」計算而跑掉
  for (const raw of printable(text).split("\n")) {
    let line = "";
    for (const ch of raw) {
      const test = line + ch;
      if (font.widthOfTextAtSize(test, size) > maxW && line) {
        out.push(line);
        line = ch;
      } else {
        line = test;
      }
    }
    out.push(line);
  }
  return out;
}

class Doc {
  doc!: PDFDocument;
  font!: PDFFont;
  page!: PDFPage;
  y = 0;
  pageNo = 0;
  title = "";

  static async create(title: string): Promise<Doc> {
    const d = new Doc();
    d.title = title;
    d.doc = await PDFDocument.create();
    d.doc.registerFontkit(fontkit);
    // subset: false 是刻意的，改成 true 會掉字，理由見檔頭
    d.font = await d.doc.embedFont(await loadFont(), { subset: false });
    d.doc.setTitle(title);
    d.doc.setProducer("職安填報系統");
    d.newPage();
    return d;
  }

  newPage() {
    this.page = this.doc.addPage([A4.w, A4.h]);
    this.pageNo += 1;
    this.y = A4.h - M.top;
  }

  need(h: number) {
    if (this.y - h < M.bottom) this.newPage();
  }

  text(s: string, opts: {
    size?: number; color?: ReturnType<typeof rgb>; x?: number; maxW?: number;
    gap?: number;
  } = {}) {
    const size = opts.size ?? 9;
    const color = opts.color ?? INK;
    const x = opts.x ?? M.left;
    const maxW = opts.maxW ?? CONTENT_W;
    for (const line of wrap(s, this.font, size, maxW)) {
      this.need(size + 4);
      this.page.drawText(printable(line), { x, y: this.y - size, size, font: this.font, color });
      this.y -= size * 1.45;
    }
    this.y -= opts.gap ?? 0;
  }

  /** ▎區段標題 */
  heading(s: string) {
    this.need(28);
    this.y -= 8;
    this.page.drawRectangle({
      x: M.left, y: this.y - 11, width: 2.5, height: 11, color: ACCENT,
    });
    this.page.drawText(printable(s), {
      x: M.left + 8, y: this.y - 10, size: 10.5, font: this.font, color: INK,
    });
    this.y -= 20;
  }

  hr() {
    this.need(8);
    this.page.drawLine({
      start: { x: M.left, y: this.y }, end: { x: A4.w - M.right, y: this.y },
      thickness: 0.6, color: LINE,
    });
    this.y -= 8;
  }

  /** 表格。cols 為欄寬（點），rows[0] 視為表頭。 */
  table(cols: number[], rows: string[][], opts: { highlight?: number[] } = {}) {
    const size = 8;
    const padX = 4;
    const padY = 4;
    const highlight = new Set(opts.highlight ?? []);

    rows.forEach((row, ri) => {
      const wrapped = row.map((cell, ci) =>
        wrap(cell, this.font, size, cols[ci] - padX * 2));
      const lines = Math.max(...wrapped.map((w) => w.length), 1);
      const rowH = lines * size * 1.35 + padY * 2;

      if (this.y - rowH < M.bottom) {
        this.newPage();
        // 換頁後重畫表頭
        if (ri > 0) this.table(cols, [rows[0]], {});
      }

      const top = this.y;
      if (ri === 0) {
        this.page.drawRectangle({
          x: M.left, y: top - rowH, width: CONTENT_W, height: rowH,
          color: rgb(0.93, 0.92, 0.89),
        });
      } else if (highlight.has(ri)) {
        this.page.drawRectangle({
          x: M.left, y: top - rowH, width: CONTENT_W, height: rowH, color: FAIL_BG,
        });
      }

      let x = M.left;
      wrapped.forEach((cellLines, ci) => {
        cellLines.forEach((line, li) => {
          this.page.drawText(printable(line), {
            x: x + padX,
            y: top - padY - size - li * size * 1.35,
            size, font: this.font,
            color: ri === 0 ? MUTED : INK,
          });
        });
        x += cols[ci];
        this.page.drawLine({
          start: { x, y: top }, end: { x, y: top - rowH },
          thickness: 0.4, color: LINE,
        });
      });

      this.page.drawLine({
        start: { x: M.left, y: top - rowH }, end: { x: A4.w - M.right, y: top - rowH },
        thickness: 0.4, color: LINE,
      });
      this.page.drawLine({
        start: { x: M.left, y: top }, end: { x: M.left, y: top - rowH },
        thickness: 0.4, color: LINE,
      });
      this.y -= rowH;
    });
    this.y -= 6;
  }

  async signatures(sigs: SigInput[]) {
    this.heading("簽核");
    if (!sigs.length) {
      this.text("（尚未簽核）", { color: MUTED });
      return;
    }
    // 每列最多 4 格：協議表廠商一多，全擠一列會窄到簽名看不見
    const perRow = Math.min(sigs.length, 4);
    const boxW = CONTENT_W / perRow;
    const boxH = 76;

    for (let i = 0; i < sigs.length; i++) {
      if (i % perRow === 0) {
        if (i) this.y -= boxH;
        this.need(boxH + 10);
      }
      const top = this.y;
      const s = sigs[i];
      const x = M.left + (i % perRow) * boxW;
      this.page.drawRectangle({
        x, y: top - boxH, width: boxW, height: boxH,
        borderColor: LINE, borderWidth: 0.5,
      });
      this.page.drawText(printable(s.role), {
        x: x + 6, y: top - 14, size: 8, font: this.font, color: MUTED,
      });
      if (s.printed_name) {
        this.page.drawText(printable(s.printed_name), {
          x: x + 6, y: top - boxH + 18, size: 8, font: this.font, color: MUTED,
        });
      }
      if (s.unsigned) {
        this.page.drawText(printable("未簽"), {
          x: x + 8, y: top - 40, size: 10, font: this.font, color: MUTED,
        });
      }
      if (s.image) {
        try {
          const png = await this.doc.embedPng(s.image);
          const scale = Math.min((boxW - 16) / png.width, 28 / png.height, 1);
          this.page.drawImage(png, {
            x: x + 8, y: top - 47,
            width: png.width * scale, height: png.height * scale,
          });
        } catch {
          // 簽名圖毀損時略過，仍保留角色與時間
        }
      }
      // 現場共用同一組帳號，signer_name 只是帳號名、代表不了實際簽名的人，
      // 手寫簽名圖才是。印帳號名反而誤導（看似某人親簽），故留白只保簽署時間。
      this.page.drawText(printable(s.signed_at), {
        x: x + 6, y: top - boxH + 8, size: 7, font: this.font, color: MUTED,
      });
    }
    this.y -= boxH + 8;
  }

  /** 缺失照片附頁：有改善後照片時前後左右對照，只有改善前時一列三張。 */
  async photos(groups: PhotoGroup[]) {
    const list = groups.filter((g) => g.before.length || g.after.length || g.broken || g.skipped);
    if (!list.length) return;
    this.newPage();
    this.heading("缺失照片");
    for (const g of list) {
      const paired = g.after.length > 0;
      const cols = paired ? 2 : 3;
      const gap = 8;
      const w = (CONTENT_W - gap * (cols - 1)) / cols;
      const h = w * 0.75;
      const cells: (PhotoCell | null)[] = [];
      if (paired) {
        for (let i = 0; i < Math.max(g.before.length, g.after.length); i++) {
          cells.push(g.before[i] ? { label: `改善前 ${i + 1}`, bytes: g.before[i] } : null);
          cells.push(g.after[i] ? { label: `改善後 ${i + 1}`, bytes: g.after[i] } : null);
        }
      } else {
        g.before.forEach((bytes, i) => cells.push({ label: `改善前 ${i + 1}`, bytes }));
      }
      // 標題不和第一列照片分在兩頁
      this.need(40 + (cells.length ? h + 14 : 0));
      this.text(`${g.no}　${g.text}`, { size: 9 });
      if (g.sub) this.text(g.sub, { size: 7.5, color: MUTED, gap: 2 });
      for (let i = 0; i < cells.length; i += cols) {
        this.need(h + 14);
        for (let c = 0; c < cols; c++) {
          const cell = cells[i + c];
          if (cell) await this.photoCell(cell, M.left + c * (w + gap), this.y, w, h);
        }
        this.y -= h + 14;
      }
      if (g.broken) this.text(`另有 ${g.broken} 張照片無法顯示，請至系統查看。`, { size: 7.5, color: MUTED });
      if (g.skipped) this.text(`另有 ${g.skipped} 張照片超過這份 PDF 的容量未放入，請至系統查看。`, { size: 7.5, color: MUTED });
      this.y -= 8;
    }
  }

  private async photoCell(cell: PhotoCell, x: number, top: number, w: number, h: number) {
    this.page.drawText(printable(cell.label), { x, y: top - 8, size: 7.5, font: this.font, color: MUTED });
    const boxTop = top - 12;
    this.page.drawRectangle({ x, y: boxTop - h, width: w, height: h, borderColor: LINE, borderWidth: 0.5 });
    try {
      const img = imageKind(cell.bytes) === "png"
        ? await this.doc.embedPng(cell.bytes) : await this.doc.embedJpg(cell.bytes);
      const scale = Math.min(w / img.width, h / img.height);
      const iw = img.width * scale, ih = img.height * scale;
      this.page.drawImage(img, { x: x + (w - iw) / 2, y: boxTop - h + (h - ih) / 2, width: iw, height: ih });
    } catch {
      // 檔案毀損時留空框與標籤，不能讓整份 PDF 產不出來
      this.page.drawText(printable("照片無法顯示"), { x: x + 6, y: boxTop - 14, size: 8, font: this.font, color: MUTED });
    }
  }

  finish(note = "電子簽名紀錄存於系統資料庫"): void {
    const stamp = new Date().toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false });
    const pages = this.doc.getPages();
    pages.forEach((p, i) => {
      p.drawText(printable(`本文件由職安填報系統於 ${stamp} 產出，${note}`), {
        x: M.left, y: 28, size: 7, font: this.font, color: MUTED,
      });
      p.drawText(printable(`第 ${i + 1} / ${pages.length} 頁`), {
        x: A4.w - M.right - 60, y: 28, size: 7, font: this.font, color: MUTED,
      });
    });
    reportMissing(this.title);
  }
}

// 伺服器跑 UTC。日期時間一律明確指定台北時區，
// 否則存查文件上的提交時間與簽署時間會差 8 小時。
const TZ = "Asia/Taipei";

const fmtDate = (d: unknown) =>
  d ? new Date(d as string).toLocaleDateString("zh-TW", { timeZone: TZ }) : "－";
const fmtTime = (d: unknown) =>
  d ? new Date(d as string).toLocaleString("zh-TW", { timeZone: TZ, hour12: false }) : "－";

const RESULT_MARK: Record<string, string> = { pass: "✓", fail: "✓", na: "－" };
const STATUS_LABEL: Record<string, string> = {
  open: "改善中", fixed: "待複驗", verified: "已複驗", closed: "已結案",
};

/** 缺失處置（可複選）：立即改善／限期改善（期限）／代工／扣款（金額）。
 *  舊資料沒有處置清單時依 action_type。 */
function actionText(x: any): string {
  const d = String(x.dispositions || x.action_type || "onsite").split(",");
  const out: string[] = [];
  if (d.includes("onsite")) out.push("立即改善");
  if (d.includes("scheduled")) out.push(x.due_date ? `限期改善（${fmtDate(x.due_date)}前）` : "限期改善");
  if (d.includes("proxy")) out.push(x.due_date && !d.includes("scheduled") ? `代工（${fmtDate(x.due_date)}前）` : "代工");
  if (d.includes("deduct")) out.push(x.penalty ? `扣款 ${Number(x.penalty).toLocaleString()} 元` : "扣款");
  return out.join("、") || "立即改善";
}

export interface SigInput {
  role: string; signer_name: string; signed_at: string; image?: Uint8Array | null;
  printed_name?: string; unsigned?: boolean;
}

/** 一筆缺失的照片。broken＝讀不到或格式放不進 PDF，skipped＝超過這份 PDF 的容量。 */
export interface PhotoGroup {
  no: string; text: string; sub: string;
  before: Uint8Array[]; after: Uint8Array[]; broken: number; skipped: number;
}
interface PhotoCell { label: string; bytes: Uint8Array }

const MAX_PNG_PIXELS = 4000 * 4000;

/** pdf-lib 只能嵌入 JPEG 與 PNG（WebP 不行），看檔頭判斷，不信副檔名。 */
export function imageKind(b: Uint8Array): "jpg" | "png" | null {
  if (b[0] === 0xff && b[1] === 0xd8) return "jpg";
  if (b.length >= 24 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) {
    // PNG 要解壓才能嵌入，小檔案也可能展開成幾 GB，尺寸看檔頭擋掉。JPEG 是原樣放入，不解壓
    const head = new DataView(b.buffer, b.byteOffset, 24);
    return head.getUint32(16) * head.getUint32(20) <= MAX_PNG_PIXELS ? "png" : null;
  }
  return null;
}

/** 自主檢查表 PDF。 */
export async function buildInspectionPdf(
  insp: any, results: any[], findings: any[], sigs: SigInput[], photos: PhotoGroup[] = [],
): Promise<Uint8Array> {
  const d = await Doc.create(insp.form_title);

  d.text(insp.form_title, { size: 15 });
  d.text(`單號 INSP-${String(insp.id).padStart(6, "0")}${insp.revision ? `（修改第 ${insp.revision} 次）` : ""}`,
         { size: 8, color: MUTED, gap: 6 });
  d.hr();

  d.table([70, 200, 60, CONTENT_W - 330], [
    // 兩張建照、同一塊工地：工程名稱後標註棟別，存查文件才分得出這張表
    // 是在哪一棟做的檢查
    ["工程名稱", insp.building ? `${insp.site_name}（${insp.building}）` : insp.site_name,
      "檢查日期", fmtDate(insp.inspect_date)],
    ["檢查地點", insp.location || "－", "天氣", insp.weather || "－"],
    ["檢查人員", insp.inspector_name, "提交時間", fmtTime(insp.submitted_at)],
    ...(insp.vendor_name ? [["受檢廠商", insp.vendor_name, "", ""]] : []),
  ]);

  d.heading("檢查結果");
  const rows: string[][] = [["類別", "檢查項目", "合格", "不合格", "查驗結果／改善措施"]];
  const highlight: number[] = [];
  results.forEach((r, i) => {
    rows.push([
      r.category || "",
      `${r.seq}. ${r.text}`,
      r.result === "pass" ? RESULT_MARK.pass : (r.result === "na" ? "－" : ""),
      r.result === "fail" ? "✓" : "",
      r.remark || "",
    ]);
    if (r.result === "fail") highlight.push(i + 1);
  });
  d.table([54, 214, 30, 38, CONTENT_W - 336], rows, { highlight });

  if (findings.length) {
    d.heading("本次開立缺失");
    const f = [["缺失單號", "災害類別", "缺失內容", "責任廠商／人", "改善方式", "狀態"]];
    for (const x of findings) {
      const action = actionText(x);
      f.push([
        `F${String(x.id).padStart(6, "0")}`,
        x.hazard_label || "",
        x.description,
        `${x.vendor_name || ""}／${x.responsible_person || ""}`,
        action,
        STATUS_LABEL[x.status] || x.status,
      ]);
    }
    d.table([54, 52, 150, 90, 84, CONTENT_W - 430], f);
  }

  await d.signatures(sigs);
  await d.photos(photos);
  d.finish();
  return d.doc.save();
}

/** 每日協議、巡視及處理紀錄表 PDF。 */
export async function buildCoordinationPdf(
  co: any, attendees: any[], findings: any[], sigs: SigInput[], photos: PhotoGroup[] = [],
): Promise<Uint8Array> {
  const d = await Doc.create("每日協議、巡視及處理紀錄表");

  d.text("每日協議、巡視及處理紀錄表", { size: 15 });
  d.text(`單號 COORD-${String(co.id).padStart(6, "0")}${co.revision ? `（修改第 ${co.revision} 次）` : ""}`,
         { size: 8, color: MUTED, gap: 6 });
  d.hr();

  // 紀錄人員必須進 PDF：現場共用同一組帳號，這份簽核後存查的文件是稽核
  // 與事故調查實際會調閱的東西，少了填表人就答不出「這張表是誰填的」。
  d.table([70, 150, 70, 100, 60, CONTENT_W - 450], [
    ["工程名稱", co.building ? `${co.site_name}（${co.building}）` : co.site_name,
      "開會日期", fmtDate(co.meeting_date),
      "作業日期", fmtDate(co.work_date)],
    ["紀錄人員", co.recorder_name || "—", "提交時間", fmtTime(co.submitted_at),
      "", ""],
  ]);

  d.heading("參加協議人員");
  const a = [["作業項目", "供應商", "職種", "參加協議人員", "作業內容"]];
  for (const x of attendees) {
    a.push([
      x.work_item || "", x.vendor_name || "", x.trade || "",
      `${x.person_name || ""}${x.employee_no ? `（${x.employee_no}）` : ""}`,
      x.work_content || "",
    ]);
  }
  d.table([90, 80, 60, 110, CONTENT_W - 340], a);

  d.heading("一、協議事項（應具體指明何處、何事、何人）");
  d.text(co.agreement_text || "－");

  d.heading("二、巡視結果");
  if (findings.length) {
    const f = [["項次", "災害類別", "缺失內容", "責任廠商／人", "改善方式"]];
    findings.forEach((x, i) => {
      const action = actionText(x);
      f.push([
        String(i + 1), x.hazard_label || "", x.description,
        `${x.vendor_name || ""}／${x.responsible_person || ""}`, action,
      ]);
    });
    d.table([32, 58, 180, 110, CONTENT_W - 380], f);
  } else {
    d.text(co.patrol_text || "本日巡視未發現缺失。");
  }

  d.heading("三、處理情形（說明停止作業、扣款及要求改善情形）");
  d.text(co.handling_text || "－");

  await d.signatures(sigs);
  await d.photos(photos);
  d.finish();
  return d.doc.save();
}

/** 缺失改善紀錄：簽核 PDF 完成後內容固定，之後補的改善照片在這份看。按需產生，不存檔。 */
export async function buildFindingReportPdf(
  filter: string, findings: any[], photos: PhotoGroup[],
): Promise<Uint8Array> {
  const d = await Doc.create("缺失改善紀錄");
  d.text("缺失改善紀錄", { size: 15 });
  d.text(`${filter}　共 ${findings.length} 筆`, { size: 8, color: MUTED, gap: 6 });
  d.hr();
  if (findings.length) {
    const rows = [["缺失單號", "發現時間", "棟別", "缺失內容", "責任廠商／人", "改善方式", "改善說明", "狀態"]];
    for (const x of findings) {
      rows.push([
        `F${String(x.id).padStart(6, "0")}`, fmtDate(x.found_at), x.building || x.site || "", x.description,
        [x.vendor, x.responsible_person].filter(Boolean).join("／"), actionText(x),
        // 覆驗紀錄裡的舊照片是系統路徑，印出來沒有意義
        [x.fix_note, x.review_note?.replace(/；?前次改善照片：\S+/g, "")].filter(Boolean).join("\n"),
        STATUS_LABEL[x.status] || x.status,
      ]);
    }
    d.table([50, 50, 42, 130, 70, 60, 62, CONTENT_W - 464], rows);
  } else {
    d.text("這個條件沒有缺失。", { color: MUTED });
  }
  await d.photos(photos);
  d.finish("僅供查閱，簽核版本以各單據 PDF 為準");
  return d.doc.save();
}
