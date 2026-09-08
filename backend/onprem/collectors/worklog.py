"""抓取工務所 LINE 群組的出工回報，供工地看板「本日出工一覽表」使用。

資料路徑
--------
各協力商每天早上在工務所 LINE 群組發「出工回報」訊息 → 群組 webhook 把
訊息落到 Google 試算表 → 這支程式定時抓試算表的 CSV 匯出、解析訊息、
寫進本機資料庫（work_logs 表）。看板依當日日期彙整顯示，
上月總出工（人日）也由此加總。

為什麼用啟發式解析
------------------
出工回報是自由文字，每家廠商寫法都不同：日期有人寫在標題行、棟別有人
用「-」有人用「：」、人數有「鋼筋工*28」「出工數：15人」「電班：9人」
三種寫法。解析規則以實際訊息歸納，抓不出來的欄位留空、原文保留在
raw 欄位——寧可欄位空著讓人對照原文，也不要猜錯數字上牆。

同一天同一棟同一廠商常會重發更正版，以 (日期, 棟別, 廠商) 為鍵、
取最新一則覆蓋。

設定（見 .env.onprem.example）：

    WORKLOG_SHEET_URL   試算表網址（/edit 分享連結或 CSV 匯出網址皆可）
    WORKLOG_INTERVAL    每輪間隔秒數，預設 900
    BUILDING_LABELS     棟別詞彙（與看板共用），供訊息中的棟別比對

用法（在 backend/onprem 目錄下執行）：

    python -m collectors.worklog            # 跑一輪就結束
    python -m collectors.worklog --loop     # 持續執行
"""

from __future__ import annotations

import csv
import io
import re
import sys
import time
from datetime import date, datetime
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from app.db import SessionLocal, WorkLog, init_db

from .config import env, load_env, log

TIMEOUT = 30
DEFAULT_INTERVAL = 900

# 判斷「這個 token 是工項不是廠商名」用的詞。廠商行常寫成「廠商-作業」
# 或「棟別-作業-廠商」，順序不固定，只能靠內容特徵挑出廠商那一段。
WORK_WORDS = ("綁紮", "模板", "水電", "帷幕", "鋼筋", "鋼構", "放樣", "配管",
              "封模", "組立", "吊裝", "泥作", "油漆", "防水", "機電", "地改",
              "假設工程")

# 這些是施工機具不是工種——鋼構類廠商常把「760塔吊*1」列在人數
# 清單旁邊，不濾掉的話工種欄會混進機具、加總人數也會多算
MACHINE_WORDS = ("堆高機", "塔吊", "作業車", "挖掘機", "吊車", "吊卡")

ROC_DATE = re.compile(r"(1[0-9]{2})[/.．](\d{1,2})[/.．](\d{1,2})")
AD_DATE = re.compile(r"(20\d{2})[/.．-](\d{1,2})[/.．-](\d{1,2})")
STAR_COUNT = re.compile(r"([一-鿿\w]{1,8})\s*[*×xX＊]\s*(\d+)")
COLON_COUNT = re.compile(r"^([^\s：:]{1,8})\s*[：:]\s*(\d+)\s*[人名]?\s*$")
# 人數的兩層總計。[^0-9\n] 不可跨行：跨行的話「出工數：」空欄接下一行的
# 「1.鋼筋綁紮」會把編號 1 當成總人數。
# GRAND（總人數／合計）獨佔：訊息同時列「本籍出工數3＋外籍出工數11」
# 與「總人數14」時，只能取 14，全部加總會變 28。
GRAND_COUNT = re.compile(r"(?:總人數|合計)[^0-9\n]{0,6}(\d+)")
TOTAL_COUNT = re.compile(r"出工數[^0-9\n]{0,6}(\d+)")
SUPERVISOR = re.compile(r"作業主管(?:姓名)?[：:\s]\s*([^\s，,、：:]{2,10})")
TASK_LINE = re.compile(r"^\d+[.、)]\s*(.+)$")
# 「本公司移工 13人」——廠商行自帶人數的寫法
VENDOR_COUNT = re.compile(r"^(.{2,20}?)[\s　]+(\d+)\s*人$")
HAS_CJK = re.compile(r"[一-鿿]")


def sheet_csv_url() -> str:
    raw = (env("WORKLOG_SHEET_URL") or "").strip()
    if not raw:
        raise RuntimeError("未設定 WORKLOG_SHEET_URL")
    # 接受一般的 /edit 分享連結，自動轉成 CSV 匯出網址
    m = re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", raw)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
    return raw


def building_labels() -> list:
    labels = []
    for pair in (env("BUILDING_LABELS") or "").split(","):
        _, _, label = pair.partition(":")
        if label.strip():
            labels.append(label.strip())
    return list(dict.fromkeys(labels))


def parse_building(text: str, labels: list) -> Optional[str]:
    for lb in labels:
        if lb in text:
            return lb
    # 常見同義寫法：現場偶爾把辦公棟寫成商辦棟
    if "商辦" in text and "辦公棟" in labels:
        return "辦公棟"
    m = re.search(r"([一-鿿]{1,4}棟)", text)
    return m.group(1) if m else None


def parse_report_date(text: str, fallback: Optional[date]) -> Optional[date]:
    # 先把空白全部拿掉再比對——現場有人把日期打成「115/ 09 / 0 5」
    flat = re.sub(r"\s+", "", text)
    m = ROC_DATE.search(flat)
    if m:
        try:
            return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = AD_DATE.search(flat)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return fallback


def _strip_meta(line: str, labels: list) -> str:
    """去掉行內的日期、天氣註記與棟別，看剩下什麼。"""
    s = re.sub(r"1[0-9]{2}\s*[/.．]\s*\d(?:\s*\d)?\s*[/.．]\s*\d(?:\s*\d)?", "", line)
    s = AD_DATE.sub("", s)
    s = re.sub(r"[（(][^（）()]{1,4}[）)]", "", s)      # （雨）（晴）之類
    s = s.replace("出工回報", "")
    for lb in labels:
        s = s.replace(lb, "")
    # 「A棟」「B251 住宅棟」這類分區代號不是廠商。字母後面必須跟著
    # 數字或「棟」——兩者都寫成 optional 的話，規則會退化成「刪掉任何
    # 1~2 個英文字母」，英文開頭的廠商名（如 ABC營造）就被切爛了
    s = re.sub(r"[一-鿿]{1,4}棟", "", s)
    s = re.sub(r"[A-Za-z]{1,2}(?:\d{1,4}棟?|棟)", "", s)
    return s.strip(" -－：:／/，,、。　")


def parse_message(content: str, labels: list, fallback_date: Optional[date]) -> Optional[dict]:
    """單則出工回報 → 欄位 dict；抓不出廠商時回 None（原文仍會留在 log）。"""
    lines = [ln.strip() for ln in content.splitlines()]
    lines = [ln for ln in lines if ln]

    report_date = parse_report_date(content, fallback_date)
    building = parse_building(content, labels)

    m = SUPERVISOR.search(content)
    supervisor = m.group(1) if m else None      # 有寫才顯示，不代填回報人

    def is_machine(name: str) -> bool:
        return any(w in name for w in MACHINE_WORDS)

    # 人數優先序：總人數／合計（取最後一個，且不再加總其他項）→
    # 「出工數」們加總（本籍＋外籍分列）→「工種*人數」加總 →
    # 「工種：N人」行加總（此時工程師、製圖員等也會計入——訊息沒給
    # 總數，只能全列都算）。機具不算人。
    grands = [int(n) for n in GRAND_COUNT.findall(content)]
    totals = [grands[-1]] if grands else \
        [int(n) for n in TOTAL_COUNT.findall(content)]
    star_pairs = [(name, int(n)) for name, n in STAR_COUNT.findall(content)
                  if not is_machine(name)]
    colon_pairs = []
    for ln in lines:
        cm = COLON_COUNT.match(ln)
        if not cm:
            continue
        name = cm.group(1)
        if "出工" in name or "人數" in name or is_machine(name):
            continue
        colon_pairs.append((name, int(cm.group(2))))
    if totals:
        headcount = sum(totals)
    elif star_pairs:
        headcount = sum(n for _, n in star_pairs)
    elif colon_pairs:
        headcount = sum(n for _, n in colon_pairs)
    else:
        headcount = None

    # 工種摘要：0 人的班別不上牆（鋼構廠商的回報把沒出工的班別也列出來）
    trade_parts = [f"{name}{n}" for name, n in star_pairs + colon_pairs if n]
    trade = "、".join(trade_parts)[:128] or None

    # 廠商行：跳過標題、統計、主管與日期行後的第一個含中文的內容行
    vendor = None
    vendor_line_idx = None
    for i, ln in enumerate(lines):
        if TOTAL_COUNT.search(ln) or GRAND_COUNT.search(ln) or SUPERVISOR.search(ln):
            continue
        if STAR_COUNT.search(ln) or COLON_COUNT.match(ln):
            continue
        if "工作項目" in ln or TASK_LINE.match(ln):
            continue
        if ln.rstrip().endswith(("：", ":")):
            continue                     # 「施工範圍：」這類小節標題
        rest = _strip_meta(ln, labels)
        if not rest:
            continue                     # 純日期／棟別／標題行
        vm = VENDOR_COUNT.match(rest)    # 「本公司移工 13人」自帶人數
        if vm:
            rest = vm.group(1).strip()
            if headcount is None:
                headcount = int(vm.group(2))
        tokens = [t.strip(" ，,、。　") for t in re.split(r"[-－：:／/]", rest)]
        tokens = [t for t in tokens if t and HAS_CJK.search(t)]
        if not tokens:
            continue                     # 只剩英數代號（分區、塔吊編號）
        if len(tokens) == 1:
            vendor = tokens[0]
        else:
            vendor = next((t for t in tokens
                           if not any(w in t for w in WORK_WORDS)), tokens[0])
            if not trade:                # 沒有逐工種人數時，用作業描述當工種
                works = [t for t in tokens if t != vendor]
                trade = "、".join(works)[:128] or None
        vendor_line_idx = i
        break
    if not vendor or len(vendor) > 20 or not report_date:
        # 廠商抓不到、或整段擠成一行（抓出來的「廠商」其實是整句話）——
        # 這種寧可略過留在原始表裡，也不要上牆一筆看似正常的錯資料
        return None
    vendor = vendor[:64]

    # 施作項目：編號行、「工作項目：」行，以及未被歸類的剩餘內容行
    tasks = []
    for i, ln in enumerate(lines):
        if i == vendor_line_idx:
            continue
        if ln.rstrip().endswith(("：", ":")):
            continue                     # 「施工機具：」這類小節標題
        tm = TASK_LINE.match(ln)
        if tm:
            tasks.append(tm.group(1))
            continue
        if "工作項目" in ln:
            _, _, rest = ln.partition("：") if "：" in ln else ln.partition(":")
            if rest.strip():
                tasks.append(rest.strip())
            continue
        if ("出工回報" in ln or TOTAL_COUNT.search(ln) or GRAND_COUNT.search(ln)
                or SUPERVISOR.search(ln)
                or STAR_COUNT.search(ln) or COLON_COUNT.match(ln)):
            continue
        if not _strip_meta(ln, labels):
            continue
        tasks.append(ln)
    task_text = "、".join(tasks)[:400] or None

    return {"report_date": report_date, "building": building, "vendor": vendor,
            "trade": trade, "headcount": headcount, "supervisor": supervisor,
            "tasks": task_text}


def fetch_rows() -> list:
    r = requests.get(sheet_csv_url(), timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    return list(csv.DictReader(io.StringIO(r.text)))


def poll_once() -> str:
    labels = building_labels()
    rows = fetch_rows()

    # 解析並以 (日期, 棟別, 廠商) 去重，取最新一則（廠商常重發更正版）
    parsed = {}
    skipped = 0
    for row in rows:
        content = (row.get("訊息內容") or "").strip()
        if "出工回報" not in content[:16]:
            continue
        reported_at = None
        try:
            reported_at = datetime.strptime(
                (row.get("接收時間") or "").strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
        item = parse_message(content, labels,
                             reported_at.date() if reported_at else None)
        if not item:
            skipped += 1
            continue
        item["reporter"] = (row.get("使用者名稱") or "")[:64] or None
        item["reported_at"] = reported_at
        item["message_id"] = (row.get("LINE訊息ID") or "")[:64] or None
        item["raw"] = content[:2000]
        key = (item["report_date"], item["building"] or "", item["vendor"])
        old = parsed.get(key)
        if old is None or (item["reported_at"] or datetime.min) >= \
                (old["reported_at"] or datetime.min):
            parsed[key] = item

    db = SessionLocal()
    created = updated = 0
    try:
        for (rd, bld, vendor), item in parsed.items():
            obj = (db.query(WorkLog)
                   .filter(WorkLog.report_date == rd,
                           WorkLog.building == (bld or None),
                           WorkLog.vendor == vendor).first())
            if not obj:
                obj = WorkLog(report_date=rd, building=bld or None, vendor=vendor)
                db.add(obj)
                created += 1
            elif obj.message_id != item["message_id"]:
                updated += 1
            for field in ("trade", "headcount", "supervisor", "tasks",
                          "reporter", "reported_at", "message_id", "raw"):
                setattr(obj, field, item[field])
            obj.fetched_at = datetime.now()
        db.commit()
    finally:
        db.close()

    msg = (f"讀到 {len(rows)} 列、出工回報 {len(parsed)} 組"
           f"（新增 {created}、更新 {updated}）")
    if skipped:
        msg += f"，{skipped} 則解析不出廠商或日期已略過"
    return msg


def main() -> None:
    load_env()
    init_db()
    interval = int(env("WORKLOG_INTERVAL", str(DEFAULT_INTERVAL)) or DEFAULT_INTERVAL)
    loop = "--loop" in sys.argv
    while True:
        try:
            log(poll_once())
        except Exception as e:                          # noqa: BLE001
            log(f"本輪失敗：{e}")
        if not loop:
            return
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已停止")
