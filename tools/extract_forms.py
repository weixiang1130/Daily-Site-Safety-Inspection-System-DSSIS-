# -*- coding: utf-8 -*-
"""
從「K02-3-HS07-02-自主檢查表(範本)」docx 抽出所有檢查表定義，輸出 data/forms.json。

規則：
  - 依文件順序走訪段落與表格，段落若是「XXX自主檢查表 / XXX檢查表」即開啟一張新表單。
  - 表單開啟後遇到的第一個資料表格即為該表單的檢查項目表。
  - 5 欄型 = 單次檢查表（大類 | 項目 | 合格 | 不合格 | 改善措施）
  - 32 欄型 = 月曆型檢查表（項目 x 1..31 日）
  - 1x4 的簽名表格略過（由程式統一產生簽核欄位）。
"""
import json
import os
import re
import sys

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

# 來源 docx 路徑由參數指定；亦可用環境變數 FORMS_DOCX 設定。
# 原始範本屬公司內部文件，不隨程式碼進版控。
SRC = (sys.argv[1] if len(sys.argv) > 1
       else os.environ.get("FORMS_DOCX", "自主檢查表範本.docx"))
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "forms.json"
)

TITLE_RE = re.compile(r"^(.+?)(安全自主檢查表|自主檢查表|檢查表)$")
SKIP_TITLES = {"每日協議、巡視及處理紀錄表"}

# 檢查項目大類 -> 統一災害代碼。用於儀表板的災害類別統計。
HAZARD_MAP = [
    (("墜落", "開口", "人體墜落"), "FALL", "墜落"),
    (("感電", "防止感電", "電"), "ELEC", "感電"),
    (("倒塌", "崩塌", "結構"), "COLLAPSE", "倒塌崩塌"),
    (("飛落", "物體飛落", "落下"), "FALLING_OBJ", "物體飛落"),
    (("衝撞",), "COLLISION", "衝撞"),
    (("被夾", "被捲"), "CAUGHT", "被夾被捲"),
    (("穿刺",), "PUNCTURE", "穿刺"),
    (("火災", "動火", "滅火"), "FIRE", "火災"),
    (("局限空間", "缺氧"), "CONFINED", "局限空間"),
    (("危險機械", "危險機具", "機械管理", "機具", "起重", "吊掛"), "MACHINE", "危險機械吊掛"),
    (("門禁", "防護具", "個人防護具", "PPE"), "PPE", "門禁與防護具"),
    (("環境", "整潔", "圍籬"), "ENV", "環境整潔"),
    (("作業主管", "一般", "一般規定", "一般作業", "作業前", "設備檢點", "災害防止"), "GENERAL", "一般管理"),
]


# 檢查項目沒有大類欄（如局限空間表、月曆型表）時，改用表單名稱推定災害類別。
FORM_HAZARD = [
    (("局限空間",), "CONFINED", "局限空間"),
    (("滅火器",), "FIRE", "火災"),
    (("捲揚機", "起重機", "高空工作車", "施工電梯", "堆高機", "車輛系營建機械"),
     "MACHINE", "危險機械吊掛"),
]


def hazard_of(category: str, form_title: str = ""):
    c = re.sub(r"\s+", "", category or "")
    for keys, code, label in HAZARD_MAP:
        for k in keys:
            if k in c:
                return code, label
    t = re.sub(r"\s+", "", form_title or "")
    for keys, code, label in FORM_HAZARD:
        for k in keys:
            if k in t:
                return code, label
    return "OTHER", "其他"


def iter_block_items(parent):
    """依文件順序產出 Paragraph / Table。"""
    body = parent.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield Paragraph(child, parent)
        elif tag == "tbl":
            yield Table(child, parent)


def row_cells(row):
    """回傳該列去除合併重複後的文字。"""
    out, prev = [], None
    for c in row.cells:
        if c._tc is prev:
            continue
        prev = c._tc
        out.append(re.sub(r"\s+", " ", c.text).strip())
    return out


def norm_cat(text):
    """『門 禁 管 制』-> 『門禁管制』"""
    return re.sub(r"\s+", "", text or "")


def parse_single_table(tbl, form_title=""):
    """5 欄型：回傳 items[]"""
    items, seq = [], 0
    for row in tbl.rows:
        cells = row_cells(row)
        if len(cells) < 2:
            continue
        head = norm_cat(cells[0])
        if head in ("檢查項目", "序號") or "檢查項目" in head and len(cells) <= 3:
            continue
        if head in ("合格", "不合格"):
            continue
        cat_raw, text = cells[0], cells[1]
        # 局限空間表第一欄是序號
        if re.fullmatch(r"\d+\.?", norm_cat(cat_raw)) or norm_cat(cat_raw) == "":
            cat_raw = ""
        if not text or text.startswith("其他檢核事項"):
            continue
        seq += 1
        cat = norm_cat(cat_raw)
        code, label = hazard_of(cat, form_title)
        items.append({
            "seq": seq,
            "category": cat or "檢查項目",
            "hazard_code": code,
            "hazard_label": label,
            "text": text,
        })
    return items


def parse_monthly_table(tbl, form_title=""):
    """32 欄型：第一欄是項目文字，其餘為 1..31 日。"""
    items, seq = [], 0
    for row in tbl.rows:
        cells = row_cells(row)
        if not cells:
            continue
        text = cells[0].strip()
        if text in ("檢查日期", "查驗結果", "檢查人員簽名", ""):
            continue
        seq += 1
        code, label = hazard_of(text, form_title)
        items.append({
            "seq": seq,
            "category": "每日檢點",
            "hazard_code": code,
            "hazard_label": label,
            "text": text,
        })
    return items


def slugify(title, used):
    """產生穩定的英數 form_code。"""
    base = re.sub(r"(安全自主檢查表|自主檢查表|檢查表)$", "", title)
    base = re.sub(r"[^\w\u4e00-\u9fff]+", "", base)
    code = "F" + str(len(used) + 1).zfill(2)
    while code in used:
        code = "F" + str(int(code[1:]) + 1).zfill(2)
    used.add(code)
    return code, base


def main():
    doc = docx.Document(SRC)
    forms, used = [], set()
    pending = None  # 等待配對表格的表單標題

    for block in iter_block_items(doc):
        if isinstance(block, Paragraph):
            t = re.sub(r"\s+", "", block.text)
            if not t or t in SKIP_TITLES:
                continue
            m = TITLE_RE.match(t)
            if m and len(t) <= 30:
                pending = t
        else:
            ncols = len(block.columns)
            nrows = len(block.rows)
            if pending is None:
                continue
            if nrows <= 1 and ncols == 4:      # 簽名列
                continue
            if ncols >= 30:
                items = parse_monthly_table(block, pending)
                ftype = "monthly"
            elif ncols == 5:
                items = parse_single_table(block, pending)
                ftype = "single"
            else:
                continue
            if not items:
                continue
            code, short = slugify(pending, used)
            forms.append({
                "form_code": code,
                "title": pending,
                "short_name": short,
                "form_type": ftype,
                "item_count": len(items),
                "items": items,
            })
            pending = None

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"source": os.path.basename(SRC), "forms": forms}, f,
                  ensure_ascii=False, indent=2)

    print(f"forms: {len(forms)}  items: {sum(f['item_count'] for f in forms)}")
    for f in forms:
        print(f"  {f['form_code']}  {f['form_type']:8s} {f['item_count']:3d}  {f['title']}")


if __name__ == "__main__":
    main()
