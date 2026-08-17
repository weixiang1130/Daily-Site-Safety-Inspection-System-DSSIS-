// PDF 產出（存查用）。
//
// 使用 pdf-lib + fontkit 嵌入 Noto Sans TC（SIL OFL 1.1）。
// pdf-lib 會自動子集化，因此產出的 PDF 只含實際用到的字，檔案不會變大。
// 字型檔透過 netlify.toml 的 [functions] included_files 一併打包。

import fs from "node:fs/promises";
import path from "node:path";
import { PDFDocument, PDFFont, PDFPage, rgb } from "pdf-lib";
import fontkit from "@pdf-lib/fontkit";

const FONT_RELATIVE = "assets/fonts/NotoSansTC-Regular.otf";

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
  const candidates = [
    path.join(process.cwd(), FONT_RELATIVE),
    path.join(process.cwd(), "..", FONT_RELATIVE),
    path.resolve(FONT_RELATIVE),
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

/** 依實際字寬折行，中英混排都適用。 */
function wrap(text: string, font: PDFFont, size: number, maxW: number): string[] {
  const out: string[] = [];
  for (const raw of String(text ?? "").split("\n")) {
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
    d.font = await d.doc.embedFont(await loadFont(), { subset: true });
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
      this.page.drawText(line, { x, y: this.y - size, size, font: this.font, color });
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
    this.page.drawText(s, {
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
          this.page.drawText(line, {
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

  async signatures(sigs: Array<{
    role: string; signer_name: string; signed_at: string; image?: Uint8Array | null;
  }>) {
    this.heading("簽核");
    if (!sigs.length) {
      this.text("（尚未簽核）", { color: MUTED });
      return;
    }
    const boxW = CONTENT_W / sigs.length;
    const boxH = 76;
    this.need(boxH + 10);
    const top = this.y;

    for (let i = 0; i < sigs.length; i++) {
      const s = sigs[i];
      const x = M.left + i * boxW;
      this.page.drawRectangle({
        x, y: top - boxH, width: boxW, height: boxH,
        borderColor: LINE, borderWidth: 0.5,
      });
      this.page.drawText(s.role, {
        x: x + 6, y: top - 14, size: 8, font: this.font, color: MUTED,
      });
      if (s.image) {
        try {
          const png = await this.doc.embedPng(s.image);
          const scale = Math.min((boxW - 16) / png.width, 34 / png.height, 1);
          this.page.drawImage(png, {
            x: x + 8, y: top - 56,
            width: png.width * scale, height: png.height * scale,
          });
        } catch {
          // 簽名圖毀損時略過，仍保留姓名與時間
        }
      }
      this.page.drawText(s.signer_name, {
        x: x + 6, y: top - boxH + 18, size: 8.5, font: this.font, color: INK,
      });
      this.page.drawText(s.signed_at, {
        x: x + 6, y: top - boxH + 7, size: 7, font: this.font, color: MUTED,
      });
    }
    this.y = top - boxH - 8;
  }

  finish(): void {
    const stamp = new Date().toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false });
    const pages = this.doc.getPages();
    pages.forEach((p, i) => {
      p.drawText(`本文件由職安填報系統於 ${stamp} 產出，電子簽名紀錄存於系統資料庫`, {
        x: M.left, y: 28, size: 7, font: this.font, color: MUTED,
      });
      p.drawText(`第 ${i + 1} / ${pages.length} 頁`, {
        x: A4.w - M.right - 60, y: 28, size: 7, font: this.font, color: MUTED,
      });
    });
  }
}

const fmtDate = (d: unknown) =>
  d ? new Date(d as string).toLocaleDateString("zh-TW") : "－";
const fmtTime = (d: unknown) =>
  d ? new Date(d as string).toLocaleString("zh-TW", { hour12: false }) : "－";

const RESULT_MARK: Record<string, string> = { pass: "✓", fail: "✓", na: "－" };
const STATUS_LABEL: Record<string, string> = {
  open: "改善中", fixed: "待複驗", verified: "已複驗", closed: "已結案",
};

export interface SigInput {
  role: string; signer_name: string; signed_at: string; image?: Uint8Array | null;
}

/** 自主檢查表 PDF。 */
export async function buildInspectionPdf(
  insp: any, results: any[], findings: any[], sigs: SigInput[],
): Promise<Uint8Array> {
  const d = await Doc.create(insp.form_title);

  d.text(insp.form_title, { size: 15 });
  d.text(`單號 INSP-${String(insp.id).padStart(6, "0")}`, { size: 8, color: MUTED, gap: 6 });
  d.hr();

  d.table([70, 200, 60, CONTENT_W - 330], [
    ["工程名稱", insp.site_name, "檢查日期", fmtDate(insp.inspect_date)],
    ["檢查地點", insp.location || "－", "天氣", insp.weather || "－"],
    ["檢查人員", insp.inspector_name, "提交時間", fmtTime(insp.submitted_at)],
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
      const action = x.action_type === "onsite"
        ? "當場改善"
        : (x.due_date ? `限期改善（${fmtDate(x.due_date)}前）` : "限期改善");
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
  d.finish();
  return d.doc.save();
}

/** 每日協議、巡視及處理紀錄表 PDF。 */
export async function buildCoordinationPdf(
  co: any, attendees: any[], findings: any[], sigs: SigInput[],
): Promise<Uint8Array> {
  const d = await Doc.create("每日協議、巡視及處理紀錄表");

  d.text("每日協議、巡視及處理紀錄表", { size: 15 });
  d.text(`單號 COORD-${String(co.id).padStart(6, "0")}`, { size: 8, color: MUTED, gap: 6 });
  d.hr();

  d.table([70, 150, 70, 100, 60, CONTENT_W - 450], [
    ["工程名稱", co.site_name, "開會日期", fmtDate(co.meeting_date),
      "作業日期", fmtDate(co.work_date)],
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
      const action = x.action_type === "onsite"
        ? "當場改善"
        : (x.due_date ? `限期改善（${fmtDate(x.due_date)}前）` : "限期改善");
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
  d.finish();
  return d.doc.save();
}
