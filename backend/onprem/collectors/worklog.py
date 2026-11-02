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
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
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

# 單行人數上限。「聯絡電話：0912345678」「編號X3000000000」也長得像
# 「名稱：數字」，不擋的話一則訊息就是幾十億人，還會讓雲端寫入整批失敗。
MAX_COUNT = 999

# 回報只寫工班、沒寫廠商（「工務*1、司機*6」）：人數不能丟，廠商留給現場選
UNNAMED_VENDOR = "未標示廠商"

ROC_DATE = re.compile(r"(1[0-9]{2})[/.．](\d{1,2})[/.．](\d{1,2})")
# 手機打字把 0 打成英文字母：「115/1O/2O」「外藉勞工1O人」
ROC_DATE_TYPO = re.compile(r"(1\d{2}\s*[/.．]\s*)([0-9Oo]{1,2})(\s*[/.．]\s*)([0-9Oo]{1,2})")
COUNT_TYPO = re.compile(r"(\d)[Oo](?=\s*[人名])")
# 沒寫年：「10/30商辦棟」「10/1（四）」，只認行首。「3/5F」「7/8樓」「3/5區」是樓層與工區
_MD_END = r"(?![\dA-Za-zＦｆ樓層區棟/])"
MONTH_DAY = re.compile(r"^\s*(\d{1,2})/(\d{1,2})" + _MD_END, re.M)
# 無年份日期離接收日多遠以內才信：更遠的多半是工區編號，不是回報日
MONTH_DAY_RANGE = 31
# 內文提到的日期不是回報日：「預計115/11/05完成」
NOT_REPORT_DATE = re.compile(r"預計|預定|完成|截止|之前|以前")
AD_DATE = re.compile(r"(20\d{2})[/.．-](\d{1,2})[/.．-](\d{1,2})")
STAR_COUNT = re.compile(r"([一-鿿\w]{1,8})\s*[*×xX＊]\s*(\d+)")
COLON_COUNT = re.compile(r"^([^\s：:]{1,8})\s*[：:]\s*(\d+)\s*[人名]?\s*$")
# 人數的兩層總計。[^0-9\n] 不可跨行：跨行的話「出工數：」空欄接下一行的
# 「1.鋼筋綁紮」會把編號 1 當成總人數。
# GRAND（總人數／合計）獨佔：訊息同時列「本籍出工數3＋外籍出工數11」
# 與「總人數14」時，只能取 14，全部加總會變 28。
GRAND_COUNT = re.compile(r"(?:總人數|合計)[^0-9\n]{0,6}(\d+)")
ALL_COUNT = re.compile(r"^\s*共\s*(\d+)\s*[人名]\s*$", re.M)   # 「共5人」獨立一行
TOTAL_COUNT = re.compile(r"出工數[^0-9\n]{0,6}(\d+)")
SUPERVISOR = re.compile(r"作業主管(?:姓名)?[：:\s]\s*([^\s，,、：:]{2,10})")
# 沒有分隔符號的寫法：「作業主管某某某外藉勞工10人」。只收三個字，否則會吃到後面的字
SUPERVISOR_TIGHT = re.compile(r"作業主管([一-鿿]{3})(?=[外本\s，,、]|$)", re.M)
TASK_LINE = re.compile(r"^\d+[.、)]\s*(.+)$")
# 「本公司移工 13人」——廠商行自帶人數的寫法
VENDOR_COUNT = re.compile(r"^(.{2,20}?)[\s　]+(\d+)\s*人$")
# 「某某-鋼筋綁紮*15」：廠商、工種、總人數寫在同一行，後面的行是各區細項（人數已含在內）
VENDOR_STAR = re.compile(r"^([^\s*×xX＊\d]{2,12})[-－]\s*([一-鿿\w]{1,10})\s*[*×xX＊]\s*(\d+)\s*$")
# 「鋼承板（某某）」：工項在前，廠商在括號裡
VENDOR_PAREN = re.compile(r"^([一-鿿]{2,10})[（(]([一-鿿]{2,6})[）)]\s*$")
# 括號裡不是廠商：「出工回報（更正）」「住宅棟（多雲）」「（星期五）」。整段比對，
# 「鋼承板（順風）」的風是名字的一部分
PAREN_NOT_VENDOR = re.compile(r"(?:[多晴陰雨雲風時轉偶陣有]|天氣)+|(?:更正|修正|更新|補報|重發)[版後]?"
                              r"|(?:星期|禮拜|週|周).")
PAREN_TITLE = re.compile(r"出工|回報|更正|修正|星期|禮拜|週|天氣")
# 去掉標題後只剩更正字樣的行：「更正」「出工回報更正」
NOT_VENDOR_LINE = re.compile(r"(?:更正|修正)[版後]?(?:回報)?")
# 「外藉勞工10人」「本籍1人」
WORKER_COUNT = re.compile(r"(?:外[藉籍]|本[藉籍])[^0-9\n人]{0,5}(\d+)\s*[人名]")
# 細項行尾的人數：「A棟2F站窗-1人」（只有一行細項、沒有其他人數時才用）
TAIL_COUNT = re.compile(r"[-－]\s*(\d+)\s*人\s*$", re.M)
# 「商辦棟某某移工12人」：整則都是這種行，每行一組（棟別、名稱、人數）
GROUP_LINE = re.compile(r"^(住宅棟|商辦棟|辦公棟|住宅|商辦)?\s*([一-鿿]{2,10}?)\s*(\d+)\s*人\s*$")
# 統計列不是一組：「合計14人」「出工數5人」
GROUP_TOTAL = re.compile(r"合計|總計|小計|共計|人數|出工")
LINE_COUNT = re.compile(r"\d+\s*[人名]\s*$")
HAS_CJK = re.compile(r"[一-鿿]")

# 「施工回報」：一天一則、逐行列出各廠商——另一種回報寫法（例如 BD10）。
#   施工回報 115/10/03（六）
#   1. 〇〇-9工-D區 #6〇〇〇
#   2. 〇〇-0工-
#   〇〇合計出工：88工
# 每行「名稱-N工-施作內容」，名稱是廠商簡稱。0 工的行是當天沒出工，不計。
# 分隔號容許半形、全形與長破折號（手機輸入法常混用）；人數容許小數（0.5 工＝
# 半天，仍算一個人到場，見 parse_daily_list）。
_DASH = "[-－—–]"
DAILY_LINE = re.compile(r"^\s*\d+\s*[.、]\s*(.+?)\s*" + _DASH
                        + r"\s*(\d+(?:\.\d+)?)\s*工\s*" + _DASH + r"?\s*(.*)$")
NUMBERED_LINE = re.compile(r"^\s*\d+\s*[.、]")

# 來源回應必須有的欄位。Apps Script 出口在通行碼不符時回 HTTP 200 的「forbidden」、
# 存取權設錯時回 HTTP 200 的 Google 登入頁——不檢查的話會被當成「沒有任何回報的
# 試算表」：出工資料庫靜默停更、看板的本日出工被蓋成 0 人。
REQUIRED_COLUMNS = ("接收時間", "訊息內容")


def _csv_url(raw: str) -> str:
    # 接受一般的 /edit 分享連結，自動轉成 CSV 匯出網址；其他網址（例如
    # Apps Script 唯讀出口 /macros/s/…/exec?token=…）原樣使用
    m = re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", raw)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
    return raw


def sheet_csv_url() -> str:
    raw = (env("WORKLOG_SHEET_URL") or "").strip()
    if not raw:
        raise RuntimeError("未設定 WORKLOG_SHEET_URL")
    return _csv_url(raw)


def worklog_sources() -> dict:
    """出工資料庫要收的來源：{工地代碼: 網址}。

    主場站（PRIMARY_SITE_CODE）沿用 WORKLOG_SHEET_URL——看板、地端牆面與工地
    檢視器都只讀這一個。其他工地兩種設法擇一（可並用）：
      - 各設一個 WORKLOG_URL_<工地代碼>（例如 WORKLOG_URL_BD10）
      - 一個 WORKLOG_SOURCES，內容是 JSON：{"BD10": "網址", "BD12": "網址"}——
        GitHub 上只要改這一個 Secret，新增工地不必改 workflow
    讀多來源的只有 cloud-runner：出工資料庫（push_worklog.py）與協議表帶入用的
    今日出工（run_external.py 的 _site_worklogs）。
    """
    out = {}
    primary = (env("PRIMARY_SITE_CODE") or "").strip()
    main_url = (env("WORKLOG_SHEET_URL") or "").strip()
    if main_url:
        out[primary] = main_url
    raw = (env("WORKLOG_SOURCES") or "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if not isinstance(extra, dict):
                raise ValueError
        except ValueError:
            # 不印內容：裡面是通行碼
            raise RuntimeError("WORKLOG_SOURCES 格式錯誤，應為 JSON 物件 {\"工地代碼\": \"網址\"}")
        for code, url in extra.items():
            code, url = str(code).strip(), str(url or "").strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code) and url and code != primary:
                out[code] = url
    for key in sorted(os.environ):
        m = re.fullmatch(r"WORKLOG_URL_([A-Za-z0-9_-]{1,32})", key)
        if m and m.group(1) != primary and os.environ[key].strip():
            out[m.group(1)] = os.environ[key].strip()
    return out


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
    # 常見同義寫法：現場偶爾把辦公棟寫成商辦棟、住宅棟寫成住宅區
    if "商辦" in text and "辦公棟" in labels:
        return "辦公棟"
    if "住宅" in text and "住宅棟" in labels:
        return "住宅棟"
    m = re.search(r"([一-鿿]{1,4}棟)", text)
    return m.group(1) if m else None


def parse_report_date(text: str, fallback: Optional[date]) -> Optional[date]:
    # 先把空白全部拿掉再比對——現場有人把日期打成「115/ 09 / 0 5」
    flat = re.sub(r"\s+", "", "\n".join(
        ln for ln in text.splitlines() if not NOT_REPORT_DATE.search(ln)))
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
    return _month_day(text, fallback) or fallback


def _month_day(text: str, fallback: Optional[date]) -> Optional[date]:
    """沒寫年的日期取最靠近接收日的那一年（1/1 收到「12/31」是去年）。"""
    if not fallback:
        return None
    for m in MONTH_DAY.finditer(text):
        for year in (fallback.year, fallback.year - 1, fallback.year + 1):
            try:
                d = date(year, int(m.group(1)), int(m.group(2)))
            except ValueError:
                continue
            if abs((d - fallback).days) <= MONTH_DAY_RANGE:
                return d
    return None


def _fix_date_typo(text: str) -> str:
    def zero(t: str) -> str:
        return t.replace("O", "0").replace("o", "0")
    text = ROC_DATE_TYPO.sub(lambda m: m.group(1) + zero(m.group(2)) + m.group(3) + zero(m.group(4)), text)
    return COUNT_TYPO.sub(r"\g<1>0", text)


def _supervisor(text: str) -> Optional[str]:
    m = SUPERVISOR.search(text) or SUPERVISOR_TIGHT.search(text)
    return m.group(1) if m else None      # 有寫才顯示，不代填回報人


_AREA = r"(?:住宅|商辦|辦公)"
_AREA_SEP = r"[\s\-－：:／/，,、+＋]"
_AREA_TOKEN = re.compile(f"(?:^|(?<={_AREA_SEP})){_AREA}區?(?:{_AREA_SEP}*{_AREA}區?)*(?=$|{_AREA_SEP})")


def _strip_meta(line: str, labels: list) -> str:
    """去掉行內的日期、天氣註記與棟別，看剩下什麼。"""
    s = re.sub(r"1[0-9]{2}\s*[/.．]\s*\d(?:\s*\d)?\s*[/.．]\s*\d(?:\s*\d)?", "", line)
    s = AD_DATE.sub("", s)
    s = re.sub(r"^\s*\d{1,2}/\d{1,2}" + _MD_END, "", s)
    s = re.sub(r"[（(][^（）()]{1,4}[）)]", "", s)      # （雨）（晴）之類
    s = s.replace("出工回報", "")
    for lb in labels:
        s = s.replace(lb, "")
    # 「A棟」「B251 住宅棟」這類分區代號不是廠商。字母後面必須跟著
    # 數字或「棟」——兩者都寫成 optional 的話，規則會退化成「刪掉任何
    # 1~2 個英文字母」，英文開頭的廠商名（如 ABC營造）就被切爛了
    s = re.sub(r"[一-鿿]{1,4}棟", "", s)
    s = re.sub(r"[A-Za-z]{1,2}(?:\d{1,4}棟?|棟)", "", s)
    # 「住宅區」「商辦/住宅」是棟別的別寫，不是廠商。只認行首或獨立一段：
    # 「某某住宅工程」「辦公家具行」裡的字是廠商名的一部分
    s = _AREA_TOKEN.sub("", s.strip())
    s = re.sub(r"^" + _AREA + "區", "", s)
    return s.strip(" -－：:／/，,、。　+＋")


def parse_message(content: str, labels: list, fallback_date: Optional[date]) -> Optional[dict]:
    """單則出工回報 → 欄位 dict；抓不出廠商時回 None（原文仍會留在 log）。"""
    lines = [ln.strip() for ln in content.splitlines()]
    lines = [ln for ln in lines if ln]

    report_date = parse_report_date(content, fallback_date)
    building = parse_building(content, labels)
    supervisor = _supervisor(content)

    def is_machine(name: str) -> bool:
        return any(w in name for w in MACHINE_WORDS)

    def is_trade(name: str, n: int) -> bool:
        # 名稱不含中文的是機具型號或分區代號，不是工種：「PC*120*2」是挖土機
        # PC120 × 2 台（曾讓工種明細兩週冒出 600 人日）、「B1F*2」是樓層。
        # 用「有沒有中文」判斷而非列舉型號——「PC吊裝*6」（預鑄班）要留著，
        # 小寫 pc、全形ＰＣ、ZX200 也都要擋。
        return bool(HAS_CJK.search(name)) and not is_machine(name) and n <= MAX_COUNT

    # 人數優先序：總人數／合計（取最後一個，且不再加總其他項）→
    # 「出工數」們加總（本籍＋外籍分列）→「工種*人數」加總 →
    # 「工種：N人」行加總（此時工程師、製圖員等也會計入——訊息沒給
    # 總數，只能全列都算）。機具不算人。
    grands = [int(n) for n in GRAND_COUNT.findall(content) if int(n) <= MAX_COUNT] \
        or [int(n) for n in ALL_COUNT.findall(content) if int(n) <= MAX_COUNT]
    totals = [grands[-1]] if grands else \
        [int(n) for n in TOTAL_COUNT.findall(content) if int(n) <= MAX_COUNT]
    # 編號行是各區細項：「鋼筋工*22」底下「1. A區*1、2. B區*21」，人數已含在上面，
    # 一起加會變兩倍。細項加起來比上面還多就不是細項（「工務*1」底下「1.鋼筋工*5」）
    def stars_in(text: str) -> list:
        return [(name, int(n)) for name, n in STAR_COUNT.findall(text) if is_trade(name, int(n))]
    plain = [p for ln in lines if not TASK_LINE.match(ln) for p in stars_in(ln)]
    numbered = [p for ln in lines if TASK_LINE.match(ln) for p in stars_in(ln)]
    star_pairs = plain + numbered if plain and sum(n for _, n in numbered) > sum(n for _, n in plain) \
        else plain or stars_in(content)

    def is_stat(ln: str) -> bool:
        return bool(TOTAL_COUNT.search(ln) or GRAND_COUNT.search(ln) or ALL_COUNT.search(ln)
                    or SUPERVISOR.search(ln) or SUPERVISOR_TIGHT.fullmatch(ln))

    # 「某某-鋼筋綁紮*15」要是第一行內容才代表廠商：上面已經有廠商行、或各行前段
    # 不同時，前段是工區（「甲工程／地下室-鋼筋綁紮*5／外牆-模板組立*3」）
    vstars = [(i, m) for i, m in enumerate(map(VENDOR_STAR.match, lines))
              if m and HAS_CJK.search(m.group(1)) and int(m.group(3)) <= MAX_COUNT
              and not re.search(r"[區棟樓]$", m.group(1))]
    if vstars and (len({m.group(1) for _, m in vstars}) > 1
                   or any(_strip_meta(ln, labels) and not is_stat(ln)
                          for ln in lines[:vstars[0][0]])):
        vstars = []
    vendor_star = vstars[0][1] if vstars else None
    vstar_lines = {i for i, _ in vstars}
    if vstars:
        own = [(m.group(2), int(m.group(3))) for _, m in vstars]
        rest_lines = [ln for i, ln in enumerate(lines) if i not in vstar_lines]
        others = [p for ln in rest_lines for p in stars_in(ln)]
        # 其他行是細項，人數已含在總數裡；比總數還多就不可能是細項
        star_pairs = own + others if sum(n for _, n in others) > sum(n for _, n in own) else own
    colon_pairs = []
    for ln in lines:
        cm = COLON_COUNT.match(ln)
        if not cm:
            continue
        name = cm.group(1)
        if "出工" in name or "人數" in name or not is_trade(name, int(cm.group(2))):
            continue
        colon_pairs.append((name, int(cm.group(2))))
    if totals:
        headcount = sum(totals)
    elif star_pairs:
        headcount = sum(n for _, n in star_pairs)
    elif colon_pairs:
        headcount = sum(n for _, n in colon_pairs)
    else:
        workers = [int(n) for n in WORKER_COUNT.findall(content) if int(n) <= MAX_COUNT] \
            or [int(n) for n in TAIL_COUNT.findall(content) if int(n) <= MAX_COUNT]
        headcount = sum(workers) if workers else None

    # 工種摘要：0 人的班別不上牆（鋼構廠商的回報把沒出工的班別也列出來）
    trade_parts = [f"{name}{n}" for name, n in star_pairs + colon_pairs if n]
    trade = "、".join(trade_parts)[:128] or None

    # 廠商行：跳過標題、統計、主管與日期行後的第一個含中文的內容行
    vendor = vendor_star.group(1).strip() if vendor_star else None
    vendor_line_idx = lines.index(vendor_star.group(0).strip()) if vendor_star else None
    for i, ln in enumerate(lines):
        if vendor:
            break
        pm = VENDOR_PAREN.match(ln)
        if pm and not PAREN_NOT_VENDOR.fullmatch(pm.group(2)) and not PAREN_TITLE.search(pm.group(1)):
            vendor = pm.group(2)
            trade = trade or pm.group(1)
            vendor_line_idx = i
            break
        if is_stat(ln):
            continue
        if STAR_COUNT.search(ln) or COLON_COUNT.match(ln):
            continue
        if "工作項目" in ln or TASK_LINE.match(ln):
            continue
        if ln.rstrip().endswith(("：", ":")):
            continue                     # 「施工範圍：」這類小節標題
        # 「某某模板作業主管某某某外藉勞工15人」整段擠在一行：廠商在「作業主管」前面
        rest = _strip_meta(ln, labels)
        if "作業主管" in rest:
            rest = re.sub(r"^\d+[A-Za-z]+", "", rest.split("作業主管")[0].strip())   # 「6FL某某模板」
        if not rest or NOT_VENDOR_LINE.fullmatch(rest):
            continue                     # 純日期／棟別／標題行
        vm = VENDOR_COUNT.match(rest)    # 「本公司移工 13人」自帶人數
        if vm:
            rest = vm.group(1).strip()
            if headcount is None and int(vm.group(2)) <= MAX_COUNT:
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
    # 找不到廠商、或找到的是區域工項（「1F出土破碎咬碎」），但有寫工班人數：
    # 人數留著、廠商標成未標示。沒有人數的才算解析失敗
    section_titles = []
    if (not vendor or re.match(r"\d", vendor)) and star_pairs:
        vendor = UNNAMED_VENDOR
        vendor_line_idx = None
        section_titles = [ln.rstrip("：: ") for ln in lines if ln.rstrip().endswith(("：", ":"))]
    if not vendor or len(vendor) > 20 or not report_date:
        # 廠商抓不到、或整段擠成一行（抓出來的「廠商」其實是整句話）——
        # 這種寧可略過留在原始表裡，也不要上牆一筆看似正常的錯資料
        return None
    vendor = vendor[:64]

    # 施作項目：編號行、「工作項目：」行，以及未被歸類的剩餘內容行
    tasks = list(section_titles)
    for i, ln in enumerate(lines):
        if i == vendor_line_idx or i in vstar_lines:
            continue
        if ln.rstrip().endswith(("：", ":")):
            continue                     # 「施工機具：」這類小節標題
        tm = TASK_LINE.match(ln)
        if tm:
            tasks.append(re.sub(r"\s*[*×xX＊]\s*\d+\s*$", "", tm.group(1)) if vendor_star
                         else tm.group(1))
            continue
        if vendor_star and STAR_COUNT.search(ln):
            tasks.append(re.sub(r"\s*[*×xX＊]\s*\d+\s*$", "", ln))   # 細項行，人數已算在總數
            continue
        if "工作項目" in ln:
            _, _, rest = ln.partition("：") if "：" in ln else ln.partition(":")
            if rest.strip():
                tasks.append(rest.strip())
            continue
        if ("出工回報" in ln or TOTAL_COUNT.search(ln) or GRAND_COUNT.search(ln)
                or ALL_COUNT.search(ln) or SUPERVISOR.search(ln) or SUPERVISOR_TIGHT.search(ln)
                or WORKER_COUNT.search(ln)
                or STAR_COUNT.search(ln) or COLON_COUNT.match(ln)):
            continue
        left = _strip_meta(ln, labels)
        if not left or NOT_VENDOR_LINE.fullmatch(left):
            continue
        tasks.append(ln)
    task_text = "、".join(tasks)[:400] or None

    # 工種明細（供出工資料庫分析）：與工種摘要同一來源，0 人的班別不計。
    # 同名工種重複列出時合併加總。訊息只寫總人數、沒有逐工種人數時為空。
    trades: dict = {}
    for name, n in star_pairs + colon_pairs:
        if n:
            trades[name] = trades.get(name, 0) + n

    return {"report_date": report_date, "building": building, "vendor": vendor,
            "trade": trade, "headcount": headcount, "supervisor": supervisor,
            "tasks": task_text,
            "trades": [{"trade": k, "headcount": v} for k, v in trades.items()]}


def _parse_groups(content: str, labels: list, fallback_date: Optional[date]) -> list:
    """整則都是「[棟別]名稱N人」的行（一家分棟、分工別列人數）→ 每組一筆；否則回空。
    「點工2人」接在「某某移工12人」後面時補上同一個名稱開頭。"""
    report_date = parse_report_date(content, fallback_date)
    body = []
    for ln in (x.strip() for x in content.splitlines()):
        left = _strip_meta(ln, labels) if ln else ""
        if not left or NOT_VENDOR_LINE.fullmatch(left):
            continue
        if "出工回報" in ln and not LINE_COUNT.search(ln):
            continue                     # 標題行；「某某出工回報12人」是廠商行
        body.append(ln.replace("出工回報", ""))
    matches = [GROUP_LINE.match(ln) for ln in body]
    if not report_date or not body or not all(matches):
        return []
    # 有統計列時整則的總數才是答案，交給 parse_message 取總數，不當成一組組相加
    if any(GROUP_TOTAL.search(m.group(2)) or int(m.group(3)) > MAX_COUNT for m in matches):
        return []
    items: dict = {}
    prev = ""
    for m in matches:
        name = m.group(2)
        if name in ("點工", "移工") and prev[-2:] in ("點工", "移工"):
            name = prev[:-2] + name
        prev = name
        building = parse_building(m.group(1), labels) if m.group(1) \
            else parse_building(content, labels)
        key = (building, name)
        if key in items:
            items[key]["headcount"] += int(m.group(3))
            continue
        items[key] = {"report_date": report_date, "building": building, "vendor": name,
                      "trade": None, "headcount": int(m.group(3)), "supervisor": None,
                      "tasks": None, "trades": []}
    return list(items.values())


def parse_report(content: str, labels: list, fallback_date: Optional[date]) -> list:
    """一則出工回報 → 一到多筆。一則可能列好幾組（_parse_groups），也可能兩家廠商
    貼在同一則、各自帶日期；只有整則解析不出東西才回空（呼叫端記成解析失敗）。"""
    content = _fix_date_typo(content)
    groups = _parse_groups(content, labels, fallback_date)
    if groups:
        return groups
    items = _parse_blocks(content, labels, fallback_date)
    if items:
        return items
    one = parse_message(content, labels, fallback_date)
    return [one] if one else []


def _parse_blocks(content: str, labels: list, fallback_date: Optional[date]) -> list:
    """兩家貼同一則、各帶日期 → 各一筆。每一塊都要是完整回報（有廠商、有人數、日期不在
    未來）才算，否則是同一則裡提到別的日期，整則當一筆。"""
    lines = content.splitlines()
    starts = [i for i, ln in enumerate(lines) if ROC_DATE.search(re.sub(r"\s+", "", ln))
              and not NOT_REPORT_DATE.search(ln)]
    if len(starts) < 2:
        return []
    # 第一個日期上面就有內容：廠商寫在日期上面，每塊要往上多帶一行，否則廠商和人數會錯開
    if any(_strip_meta(ln, labels) for ln in lines[:starts[0]] if ln.strip()):
        def above(i: int) -> int:
            i -= 1
            while i > 0 and not lines[i].strip():
                i -= 1
            return i
        starts = [0] + [above(i) for i in starts[1:]]
        if any(a >= b for a, b in zip(starts, starts[1:])):
            return []
    blocks = ["\n".join(lines[a:b]) for a, b in zip(starts, starts[1:] + [len(lines)])]
    items = [parse_message(b, labels, fallback_date) for b in blocks]
    latest = fallback_date + timedelta(days=1) if fallback_date else None
    if not all(one and one["headcount"] is not None and (not latest or one["report_date"] <= latest)
               for one in items):
        return []
    # 同一則裡其他家都是今天，只有一家是舊日期：沿用範本忘了改，跟著今天
    if sum(one["report_date"] == fallback_date for one in items) >= 2:
        for one in items:
            one["report_date"] = fallback_date
    return items


def parse_daily_list(content: str, fallback_date: Optional[date]) -> dict:
    """「施工回報」一則 → {report_date, items, lines, unparsed}。

    items：每個有出工的廠商一筆（0 工的行不成為一筆）。lines：認得的編號行數
    （含 0 工的行）——lines > 0 而 items 為空，是「當天全部 0 工」（假日、停工），
    不是解析失敗。unparsed：有編號、但格式認不得的行數——這些行的人數沒算進去。

    棟別一律留空：同一行常同時寫 A棟、B棟（「A棟4F、B棟2F…」），硬挑一個會讓
    同一家廠商在更正版裡換鍵、變成兩筆。棟別資訊保留在施作項目原文裡。
    """
    out = {"report_date": parse_report_date(content, fallback_date),
           "items": [], "lines": 0, "unparsed": 0}
    if not out["report_date"]:
        return out
    items = {}
    for ln in content.splitlines():
        m = DAILY_LINE.match(ln)
        if not m:
            if NUMBERED_LINE.match(ln):
                out["unparsed"] += 1
            continue
        out["lines"] += 1
        name = m.group(1).strip()[:64]
        raw_n = float(m.group(2))
        # 0.5 工＝半天，仍是一個人到場：有出工就至少算 1 人，其餘四捨五入
        n = max(1, int(raw_n + 0.5)) if raw_n > 0 else 0
        if not name or not HAS_CJK.search(name) or n <= 0 or n > MAX_COUNT:
            continue
        task = m.group(3).strip(" -－—–") or None
        old = items.get(name)
        if old:                          # 同一則裡同名重複列：人數相加
            old["headcount"] += n
            old["tasks"] = "、".join(t for t in (old["tasks"], task) if t)[:400] or None
            continue
        items[name] = {"report_date": out["report_date"], "building": None, "vendor": name,
                       "trade": None, "headcount": n, "supervisor": None,
                       "tasks": task[:400] if task else None, "trades": []}
    out["items"] = list(items.values())
    return out


def fetch_rows(url: Optional[str] = None) -> list:
    r = requests.get(_csv_url(url) if url else sheet_csv_url(), timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    reader = csv.DictReader(io.StringIO(r.text))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        # 只印回應的第一行（「forbidden」、HTML 開頭或欄位名稱），不印內容與網址
        head = (r.text.strip().splitlines() or [""])[0][:40]
        raise RuntimeError(
            f"來源回應不是出工回報試算表（缺欄位 {'、'.join(missing)}；回應開頭 {head!r}）。"
            "常見原因：Apps Script 通行碼與設定不一致、存取權不是「所有人」、部署已封存")
    return list(reader)


def collect_reports(rows: Optional[list] = None, site_code: Optional[str] = None):
    """解析整張試算表的出工回報（兩種寫法：每家廠商一則的「出工回報」、
    一天一則清單的「施工回報」）。每筆回報帶上 site_code（呼叫端指定）。

    回傳 (讀到的列數, {(日期, 棟別, 廠商): 回報}, 解析失敗的訊息清單)。

    同一天同一棟同一廠商有好幾則「出工回報」時（2026-10-30 檢討會：同廠商分 7F、
    B1 各自回報，只取到一則）：
      - 標題寫「更正／修正」→ 取代這家當天之前所有回報
      - 施作內容相同（重發、或只改人數的更正）→ 以較新的一則為準
      - 施作內容不同（不同樓層、不同工班）→ 人數相加、施作項目合併
    合併或被取代的訊息都記在該筆的 superseded_ids——雲端要靠它刪掉這些訊息
    改判前留在別的鍵上的舊列，否則解析規則一改，同一批人會在兩個鍵各算一次。
    同一班人上午在 B1、下午到 7F 又回報一次的情形，從訊息上分辨不出來，會被相加；
    要靠工地的回報原則（換區不重報）避免。

    施工回報是「整天的清單」：同一天發了第二則就是更正版（實例：「施工回報修正」），
    以最新一則為準取代整天。舊版有、新版沒有或改成 0 工的廠商，產生一筆 0 人的回報
    蓋掉舊數字——不這樣做，舊版的人數會因為「新版沒提到它」而一直留著。
    只有「同一天內發的」或標題寫「修正」的才整天取代：實例中有週一早上發、日期卻
    還是上週六的一則（多半是沿用範本忘了改日期），整天取代會把週六真實的出工刪掉。
    這種隔天才發的維持逐家「最新一則為準」，並另列提醒。

    解析失敗清單裡有 notice 欄位的不是失敗，呼叫端只拿來提醒、不要當成失敗：
      notice="partial"：施工回報有幾行格式認不得，其餘照常計入，這幾行沒算到
      notice="late_date"：施工回報在寫的日期之後才發，又跟那天已有的回報同日期
    本機看板收集（poll_once）與雲端出工資料庫（cloud-runner/push_worklog.py）
    共用這一份解析，兩邊的數字才會一致。
    """
    labels = building_labels()
    if rows is None:
        rows = fetch_rows()

    parsed = {}
    rejects = []
    daily = {}              # 施工回報：日期 → [(接收時間, 列序, 訊息ID, 該則的廠商名單)]
    pending = []            # (item, 列序)：施工回報的回報，等整天替換算完再進 parsed

    per_vendor = {}         # 出工回報：鍵 → [(接收時間, 列序, 解析結果, 是否標明更正)]

    def take(item):
        key = (item["report_date"], item["building"] or "", item["vendor"])
        own = list(item.pop("absorbed_ids", []))     # 已併入這筆的其他訊息
        old = parsed.get(key)
        if old is None:
            item["superseded_ids"] = own
            parsed[key] = item
        elif (item["reported_at"] or datetime.min) >= (old["reported_at"] or datetime.min):
            item["superseded_ids"] = old["superseded_ids"] + [old["message_id"]] + own
            parsed[key] = item
        else:
            old["superseded_ids"] += [item["message_id"]] + own

    for idx, row in enumerate(rows):
        content = (row.get("訊息內容") or "").strip()
        head = content[:16]
        if "出工回報" in head:
            kind = "per_vendor"
        elif "施工回報" in head:
            kind = "daily_list"
        else:
            continue
        reported_at = None
        try:
            reported_at = datetime.strptime(
                (row.get("接收時間") or "").strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
        reporter = (row.get("使用者名稱") or "")[:64] or None
        message_id = (row.get("LINE訊息ID") or "")[:64] or None
        fallback = reported_at.date() if reported_at else None
        reject = {"message_id": message_id, "reported_at": reported_at,
                  "reporter": reporter, "raw": content[:2000], "site_code": site_code}
        meta = {"reporter": reporter, "reported_at": reported_at,
                "message_id": message_id, "raw": content[:2000], "site_code": site_code}
        if kind == "per_vendor":
            ones = parse_report(content, labels, fallback)
            if not ones:
                rejects.append(reject)
                continue
            corrected = bool(CORRECTION.search("\n".join(content.splitlines()[:2])))
            for one in ones:
                one.update(meta)
                key = (one["report_date"], one["building"] or "", one["vendor"])
                per_vendor.setdefault(key, []).append(
                    (reported_at or datetime.min, idx, one, corrected))
            continue

        res = parse_daily_list(content, fallback)
        if not res["lines"]:
            rejects.append(reject)          # 一行都認不得：整則失敗
            continue
        if res["unparsed"]:
            rejects.append(dict(reject, notice="partial", unparsed=res["unparsed"]))
        for item in res["items"]:
            item.update(meta)
            pending.append(item)
        daily.setdefault(res["report_date"], []).append(
            (reported_at or datetime.min, idx, message_id, {i["vendor"] for i in res["items"]}, meta,
             "修正" in head, reject))

    # 施工回報：同一天以最新一則為準。舊版有、最新版沒有（或改成 0 工）的廠商，
    # 以最新那則的名義補一筆 0 人，讓它在「最新一則為準」的比較中勝出、蓋掉舊數字。
    # 只拿「同一天內發的」舊版比（或最新一則標明修正）——隔天才發、日期卻是舊日子
    # 的，多半是範本沒改日期，不整天取代，只提醒
    for day, msgs in daily.items():
        msgs.sort(key=lambda m: (m[0], m[1]))
        latest = msgs[-1]
        latest_vendors, latest_meta, corrected = latest[3], latest[4], latest[5]
        same_day = [m for m in msgs[:-1]
                    if corrected or m[0].date() == latest[0].date()]
        if len(same_day) < len(msgs) - 1 and latest[0].date() > day:
            rejects.append(dict(latest[6], notice="late_date", report_date=day))
        dropped = set().union(*(m[3] for m in same_day)) - latest_vendors if same_day else set()
        for vendor in sorted(dropped):
            pending.append(dict(latest_meta, report_date=day, building=None, vendor=vendor,
                                trade=None, headcount=0, supervisor=None, tasks=None, trades=[]))
    for msgs in per_vendor.values():
        take(_merge_vendor_reports(msgs))
    for item in pending:
        take(item)

    for item in parsed.values():
        item["superseded_ids"] = [m for m in dict.fromkeys(item["superseded_ids"])
                                  if m and m != item["message_id"]]
    return len(rows), parsed, rejects


CORRECTION = re.compile(r"更正|修正")
_TASK_PREFIX = re.compile(r"^\s*(工作內容|作業項目|施作項目|工作項目)\s*[：:]\s*")
_TASK_NOISE = re.compile(r"[\s、，,。．.；;()（）]")


def _task_key(text: Optional[str]) -> str:
    """施作內容的比對鍵：去掉「工作內容：」這類前綴、空白與標點、全形 F 轉半形。
    兩則的比對鍵相同，視為同一工班（重發或改人數的更正）。"""
    t = _TASK_PREFIX.sub("", (text or "").strip())
    return _TASK_NOISE.sub("", t).replace("Ｆ", "F").upper()


def _same_task(a: str, b: str) -> bool:
    """同一工班：內容相同，或其中一則只是另一則改寫、增減幾項（「5F鋪版」與
    「5F鋪版、B1F吊料」）。太短的不算，「7F」會包含在一堆不相干的內容裡。"""
    return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a))


def _trade_names(item: dict) -> set:
    return {t["trade"] for t in item.get("trades") or [] if t.get("trade")}


def _same_crew(a: dict, b: dict) -> bool:
    """同一工班的另一則回報。沒寫施作內容的不能靠內容分：工種不同才是別班
    （工務*1 與鋼筋工*9 都歸「未標示廠商」），否則是同一班補寫內容或改人數。"""
    ka, kb = _task_key(a.get("tasks")), _task_key(b.get("tasks"))
    if ka and kb:
        return _same_task(ka, kb)
    ta, tb = _trade_names(a), _trade_names(b)
    if a["vendor"] == UNNAMED_VENDOR and not (ta or tb):
        return a.get("raw") == b.get("raw")   # 廠商與工種都不知道：內容一樣才敢當重發
    return not (ta and tb) or bool(ta & tb)


def _merge_vendor_reports(msgs: list) -> dict:
    """同一天同一棟同一廠商的多則出工回報 → 一筆（規則見 collect_reports 說明）。"""
    msgs = sorted(msgs, key=lambda m: (m[0], m[1]))
    parts, absorbed = [], []
    for _, _, item, corrected in msgs:
        if corrected:
            absorbed += [p["message_id"] for p in parts]
            parts = [item]
            continue
        hits = [p for p in parts if _same_crew(p, item)]
        if not hits:
            parts.append(item)
            continue
        k = _task_key(item.get("tasks"))
        # 彙整之後又發其中一項（內容是舊則的一部分、人數沒有更多）：彙整那則才是全部
        if k and all(_task_key(p.get("tasks")) != k and k in _task_key(p.get("tasks"))
                     and (item["headcount"] or 0) <= (p["headcount"] or 0) for p in hits):
            absorbed.append(item["message_id"])
            continue
        # 同一班重發、改寫，或彙整好幾則：取代命中的全部，留在原位置
        if not k:                        # 這則沒寫內容：前面寫過的不能跟著消失
            item = dict(item, tasks="；".join(dict.fromkeys(
                p["tasks"] for p in hits if p.get("tasks"))) or None)
        at = parts.index(hits[0])
        absorbed += [p["message_id"] for p in hits]
        parts = [p for p in parts if p not in hits]
        parts.insert(at, item)
    latest = max(parts, key=lambda p: p["reported_at"] or datetime.min)
    out = dict(latest)
    if len(parts) > 1:
        counts = [p["headcount"] for p in parts if p["headcount"] is not None]
        out["headcount"] = sum(counts) if counts else None
        out["tasks"] = "；".join(dict.fromkeys(
            _TASK_PREFIX.sub("", p["tasks"]) for p in parts if p.get("tasks")))[:400] or None
        out["supervisor"] = next(
            (p["supervisor"] for p in reversed(parts) if p.get("supervisor")), None)
        trades: dict = {}
        for p in parts:
            for t in p.get("trades") or []:
                trades[t["trade"]] = trades.get(t["trade"], 0) + (t["headcount"] or 0)
        out["trades"] = [{"trade": k, "headcount": v} for k, v in trades.items()]
        # 同工種分兩則：摘要寫合計（鋼筋工8），不是「鋼筋工5、鋼筋工3」
        out["trade"] = "、".join(dict.fromkeys(
            [f"{k}{v}" for k, v in trades.items() if v]
            + [p["trade"] for p in parts if p.get("trade") and not p.get("trades")]))[:128] or None
        out["raw"] = "\n——\n".join(p["raw"] for p in parts if p.get("raw"))[:2000]
    out["absorbed_ids"] = absorbed + [p["message_id"] for p in parts if p is not latest]
    return out


def poll_once() -> str:
    row_count, parsed, rejects = collect_reports()
    skipped = sum(1 for x in rejects if not x.get("notice"))

    db = SessionLocal()
    created = updated = removed = 0
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
        # 解析規則改版後，同一則訊息換了鍵：舊鍵那列要刪，否則同一批人算兩次。
        # 只刪「訊息還在、但這個鍵已不屬於它」的列；來源消失的列不動（試算表曾進垃圾桶）
        keys = {(rd, bld or None, vendor) for rd, bld, vendor in parsed}
        ids = {m for item in parsed.values()
               for m in [item["message_id"], *(item.get("superseded_ids") or [])] if m}
        db.flush()
        rows = db.query(WorkLog.id, WorkLog.message_id, WorkLog.report_date,
                        WorkLog.building, WorkLog.vendor).all()   # 不載入原文：每 15 分鐘掃一次全表
        for oid, mid, rd, bld, vendor in rows:
            if mid in ids and (rd, bld or None, vendor) not in keys:
                db.query(WorkLog).filter(WorkLog.id == oid).delete()
                removed += 1
        db.commit()
    finally:
        db.close()

    msg = (f"讀到 {row_count} 列、出工回報 {len(parsed)} 組"
           f"（新增 {created}、更新 {updated}、改判移除 {removed}）")
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
