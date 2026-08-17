# -*- coding: utf-8 -*-
"""PDF 產出（存查用）。

使用 reportlab 內建的 Adobe 繁體中文 CID 字型 MSung-Light，
不需要額外字型檔，Windows / Linux 皆可直接執行。
"""
import os
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .db import BASE_DIR

FONT = "MSung-Light"
pdfmetrics.registerFont(UnicodeCIDFont(FONT))

PDF_DIR = os.path.join(BASE_DIR, "uploads", "pdf")
os.makedirs(PDF_DIR, exist_ok=True)

RESULT_LABEL = {"pass": "合格", "fail": "不合格", "na": "不適用"}

title_style = ParagraphStyle("t", fontName=FONT, fontSize=15, leading=20,
                             alignment=1, spaceAfter=4)
sub_style = ParagraphStyle("s", fontName=FONT, fontSize=9, leading=13, alignment=1,
                           textColor=colors.HexColor("#555555"))
cell = ParagraphStyle("c", fontName=FONT, fontSize=8, leading=11)
cell_c = ParagraphStyle("cc", fontName=FONT, fontSize=8, leading=11, alignment=1)
head = ParagraphStyle("h", fontName=FONT, fontSize=8.5, leading=11, alignment=1)
body = ParagraphStyle("b", fontName=FONT, fontSize=9, leading=14)
h2 = ParagraphStyle("h2", fontName=FONT, fontSize=10.5, leading=15, spaceBefore=8,
                    spaceAfter=3)


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(FONT, 7.5)
    canvas.setFillColor(colors.HexColor("#777777"))
    canvas.drawString(15 * mm, 10 * mm,
                      f"本文件由職安填報系統於 {datetime.now():%Y-%m-%d %H:%M} 產出，"
                      f"電子簽名紀錄存於系統資料庫")
    canvas.drawRightString(195 * mm, 10 * mm, f"第 {doc.page} 頁")
    canvas.restoreState()


def _sig_table(signatures):
    """簽名區塊：每欄一個角色，含手寫簽名圖與簽署時間。"""
    if not signatures:
        return Paragraph("（尚未簽核）", body)
    roles, imgs, times = [], [], []
    for s in signatures:
        roles.append(Paragraph(s.role, head))
        path = os.path.join(BASE_DIR, s.image_path) if not os.path.isabs(s.image_path) \
            else s.image_path
        if os.path.exists(path):
            imgs.append(Image(path, width=32 * mm, height=14 * mm, kind="proportional"))
        else:
            imgs.append(Paragraph(s.signer_name, cell_c))
        times.append(Paragraph(
            f"{s.signer_name}<br/>{s.signed_at:%Y-%m-%d %H:%M}", cell_c))
    t = Table([roles, imgs, times], colWidths=[180 / max(len(roles), 1) * mm] * len(roles))
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 1), (-1, 1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 4),
    ]))
    return t


def build_inspection_pdf(insp, results, findings, signatures) -> str:
    """產出自主檢查表 PDF，回傳相對於專案根目錄的路徑。"""
    fname = f"INSP-{insp.id:06d}-{insp.form_code}-{insp.inspect_date:%Y%m%d}.pdf"
    fpath = os.path.join(PDF_DIR, fname)
    doc = SimpleDocTemplate(
        fpath, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=18 * mm,
        title=insp.form.title,
    )
    story = [
        Paragraph(insp.form.title, title_style),
        Paragraph(f"單號 INSP-{insp.id:06d}", sub_style),
        Spacer(1, 5),
    ]

    meta = [[
        Paragraph("<b>工程名稱</b>", cell), Paragraph(insp.site.name, cell),
        Paragraph("<b>檢查日期</b>", cell), Paragraph(f"{insp.inspect_date:%Y-%m-%d}", cell),
    ], [
        Paragraph("<b>檢查地點</b>", cell), Paragraph(insp.location or "－", cell),
        Paragraph("<b>天氣</b>", cell), Paragraph(insp.weather or "－", cell),
    ], [
        Paragraph("<b>檢查人員</b>", cell), Paragraph(insp.inspector.display_name, cell),
        Paragraph("<b>提交時間</b>", cell),
        Paragraph(f"{insp.submitted_at:%Y-%m-%d %H:%M}" if insp.submitted_at else "－", cell),
    ]]
    mt = Table(meta, colWidths=[24 * mm, 66 * mm, 24 * mm, 66 * mm])
    mt.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f2f2f2")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [mt, Spacer(1, 8)]

    rows = [[Paragraph("類別", head), Paragraph("檢查項目", head),
             Paragraph("合格", head), Paragraph("不合格", head),
             Paragraph("查驗結果／改善措施", head)]]
    styles = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 1), (3, -1), "CENTER"),
    ]
    for idx, r in enumerate(results, start=1):
        ok = "✓" if r.result == "pass" else ("－" if r.result == "na" else "")
        ng = "✓" if r.result == "fail" else ""
        rows.append([
            Paragraph(r.item.category or "", cell_c),
            Paragraph(f"{r.item.seq}. {r.item.text}", cell),
            Paragraph(ok, cell_c), Paragraph(ng, cell_c),
            Paragraph(r.remark or "", cell),
        ])
        if r.result == "fail":
            styles.append(("BACKGROUND", (0, idx), (-1, idx), colors.HexColor("#fdecec")))
    t = Table(rows, colWidths=[20 * mm, 78 * mm, 12 * mm, 16 * mm, 54 * mm], repeatRows=1)
    t.setStyle(TableStyle(styles))
    story += [t, Spacer(1, 10)]

    if findings:
        story.append(Paragraph("<b>本次開立缺失</b>", h2))
        frows = [[Paragraph(x, head) for x in
                  ("缺失單號", "災害類別", "缺失內容", "責任廠商／人", "改善方式", "狀態")]]
        for f in findings:
            action = "當場改善" if f.action_type == "onsite" else \
                f"限期改善（{f.due_date:%m/%d}前）" if f.due_date else "限期改善"
            frows.append([
                Paragraph(f"F{f.id:06d}", cell_c),
                Paragraph(f.hazard_label or "", cell_c),
                Paragraph(f.description, cell),
                Paragraph(f"{f.vendor.name if f.vendor else ''}／"
                          f"{f.responsible_person or ''}", cell),
                Paragraph(action, cell_c),
                Paragraph({"open": "改善中", "fixed": "待複驗",
                           "verified": "已複驗", "closed": "已結案"}
                          .get(f.status, f.status), cell_c),
            ])
        ft = Table(frows, colWidths=[20 * mm, 20 * mm, 58 * mm, 32 * mm, 30 * mm, 20 * mm],
                   repeatRows=1)
        ft.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story += [ft, Spacer(1, 10)]

    story.append(KeepTogether([Paragraph("<b>簽核</b>", h2), _sig_table(signatures)]))
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return os.path.relpath(fpath, BASE_DIR).replace("\\", "/")


def build_coordination_pdf(co, attendees, findings, signatures) -> str:
    """產出每日協議、巡視及處理紀錄表 PDF。"""
    fname = f"COORD-{co.id:06d}-{co.work_date:%Y%m%d}.pdf"
    fpath = os.path.join(PDF_DIR, fname)
    doc = SimpleDocTemplate(
        fpath, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=18 * mm,
        title="每日協議、巡視及處理紀錄表",
    )
    story = [
        Paragraph("每日協議、巡視及處理紀錄表", title_style),
        Paragraph(f"單號 COORD-{co.id:06d}", sub_style),
        Spacer(1, 5),
    ]

    meta = [[
        Paragraph("<b>工程名稱</b>", cell), Paragraph(co.site.name, cell),
        Paragraph("<b>開會日期</b>", cell), Paragraph(f"{co.meeting_date:%Y-%m-%d}", cell),
        Paragraph("<b>作業日期</b>", cell), Paragraph(f"{co.work_date:%Y-%m-%d}", cell),
    ]]
    mt = Table(meta, colWidths=[22 * mm, 44 * mm, 22 * mm, 30 * mm, 22 * mm, 40 * mm])
    mt.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f2f2f2")),
        ("BACKGROUND", (4, 0), (4, -1), colors.HexColor("#f2f2f2")),
    ]))
    story += [mt, Spacer(1, 8)]

    rows = [[Paragraph(x, head) for x in ("作業項目", "供應商", "職種", "參加協議人員", "作業內容")]]
    for a in attendees:
        rows.append([
            Paragraph(a.work_item or "", cell),
            Paragraph(a.vendor_name or (a.vendor.name if a.vendor else ""), cell),
            Paragraph(a.trade or "", cell),
            Paragraph(f"{a.person_name or ''}"
                      f"{'（' + a.employee_no + '）' if a.employee_no else ''}", cell),
            Paragraph(a.work_content or "", cell),
        ])
    at = Table(rows, colWidths=[34 * mm, 30 * mm, 22 * mm, 40 * mm, 54 * mm], repeatRows=1)
    at.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [at, Spacer(1, 8)]

    story.append(Paragraph("<b>一、協議事項</b>（應具體指明何處、何事、何人）", h2))
    story.append(Paragraph((co.agreement_text or "－").replace("\n", "<br/>"), body))

    story.append(Paragraph("<b>二、巡視結果</b>", h2))
    if findings:
        frows = [[Paragraph(x, head) for x in
                  ("項次", "災害類別", "缺失內容", "責任廠商／人", "改善方式")]]
        for i, f in enumerate(findings, 1):
            action = "當場改善" if f.action_type == "onsite" else \
                f"限期改善（{f.due_date:%m/%d}前）" if f.due_date else "限期改善"
            frows.append([
                Paragraph(str(i), cell_c), Paragraph(f.hazard_label or "", cell_c),
                Paragraph(f.description, cell),
                Paragraph(f"{f.vendor.name if f.vendor else ''}／"
                          f"{f.responsible_person or ''}", cell),
                Paragraph(action, cell_c),
            ])
        ft = Table(frows, colWidths=[12 * mm, 22 * mm, 68 * mm, 40 * mm, 38 * mm],
                   repeatRows=1)
        ft.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(ft)
    else:
        story.append(Paragraph(co.patrol_text or "本日巡視未發現缺失。", body))

    story.append(Paragraph("<b>三、處理情形</b>（說明停止作業、扣款及要求改善情形）", h2))
    story.append(Paragraph((co.handling_text or "－").replace("\n", "<br/>"), body))

    story += [Spacer(1, 10), KeepTogether([Paragraph("<b>簽核</b>", h2),
                                           _sig_table(signatures)])]
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return os.path.relpath(fpath, BASE_DIR).replace("\\", "/")
